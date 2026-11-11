"""``bh backfill commits|docs|planning`` over the in-memory fake bd and a throwaway git repo.

The three passes apply DRIFT (``git.commits`` appends, ``external_ref`` stamps, a later close) and
NEW through the bd StoreLoader. Every bd read and write goes through :class:`FakeBd` behind the
real loader (the scratch real-bd proof is ``test_backfill_source_passes_int``); git is a
``git init`` repository per test. Nothing touches a live hive and nothing reads a wall clock.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from tests.test_backfill_bd import FakeBd, Ticks
from tests.test_backfill_bd import cfg_path as cfg_path  # noqa: F401 — fixture resolved by name
from typer.testing import CliRunner

from beadhive import backfill_cli, cli, validate
from beadhive.backfill_bd import BdStoreLoader
from beadhive_backfill import Plan
from beadlog_sources import ChoiceQuery, JudgeSettings, Judgment

ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
ENTRY = {"provider": "github", "org": "agentguides", "repo": "infra", "prefix": "ag-infra"}
TRIPLET = ("provider:github", "org:agentguides", "repo:infra")
P1 = ".planning/phases/01-foundation"
DOC_A, DOC_B = "docs/design/a-stamped.md", "docs/design/b-unknown.md"


def _row(iid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [*TRIPLET],
        "created_at": "2026-08-10T09:00:00Z",
        "updated_at": "2026-08-20T00:00:00Z",
        **extra,
    }


def _git(repo: Path, *args: str) -> str:
    when = "2026-09-01T10:00:00+00:00"
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    )
    return done.stdout.strip()


def _commit(repo: Path, message: str, files: Mapping[str, str] | None = None) -> str:
    for name, text in (files or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class OneJudge:
    """Judges every doc a new bead; stands in for the configured ``backfill.judge`` command."""

    engine = "scripted"
    engine_version = "1"
    model = None

    def __init__(self) -> None:
        self.calls = 0

    def choose(self, query: ChoiceQuery) -> Judgment:
        self.calls += 1
        return Judgment("new", {"new": 1.0}, 0.9, len(query.options), "scripted", "scripted", "1")

    def score(self, query: Any) -> Judgment:  # pragma: no cover - never asked
        raise AssertionError("docs asks choices only")


@pytest.fixture
def hive(cfg_path, monkeypatch, tmp_path):  # noqa: F811 — the imported fixture
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        _git(repo, *command)
    store = FakeBd(
        [_row("ag-infra-1"), _row("ag-infra-2", title="Entirely different matter")],
        identity_labels=TRIPLET,
    )
    group = backfill_cli.hive_group(ENTRY, repo)
    refreshed: list[str] = []
    monkeypatch.setattr(backfill_cli, "_target", lambda hive: (repo, group))
    monkeypatch.setattr(backfill_cli, "hive_prefix", lambda hive: ENTRY["prefix"])

    def loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
        return BdStoreLoader(store, group=group, clock=Ticks(), pre_state_path=pre_state_path)

    monkeypatch.setattr(backfill_cli, "store_loader", loader)
    monkeypatch.setattr(
        backfill_cli,
        "refresh_coverage_after_apply",
        lambda hive, quiet=False: refreshed.append(hive),
    )
    return store, repo, refreshed


def _invoke(*args: str, code: int = 0) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == code, result.output
    return result


# -- the leaves --------------------------------------------------------------------------------


def test_the_three_leaves_exist_with_plan_apply_audit_options() -> None:
    for name in ("commits", "docs", "planning"):
        text = _invoke("backfill", name, "--help").stdout
        for flag in ("--apply", "--dry-run", "--plan", "--pre-state", "--audit", "--option"):
            assert flag in text, (name, flag)


# -- commits -----------------------------------------------------------------------------------


def test_commits_previews_by_default_and_writes_nothing(hive, tmp_path) -> None:
    store, repo, refreshed = hive
    first = _commit(repo, "feat(x): start (ag-infra-1)")
    plan_path = tmp_path / "commits.bdlg"

    text = _invoke("backfill", "commits", "--plan", str(plan_path)).stdout
    assert store.writes == [] and refreshed == []
    assert "commits: 1 beads would change" in text and "DRIFT=1" in text
    assert f"saved plan: {plan_path}" in text
    plan = Plan.read(plan_path)
    assert plan.pass_name == "commits" and plan.report["options"]["rev"] == first

    summary = json.loads(_invoke("backfill", "commits", "--dry-run", "--json").stdout)
    assert summary["pass"] == "commits" and summary["changes"] == 1
    assert summary["totals"]["drift_reported"] == 0
    assert store.writes == []

    both = _invoke("backfill", "commits", "--apply", "--dry-run", code=2)
    assert "either --dry-run or --apply" in both.output


def test_commits_apply_appends_shas_as_drift_and_a_second_run_plans_nothing(hive, tmp_path) -> None:
    store, repo, refreshed = hive
    first = _commit(repo, "feat(x): start (ag-infra-1)")
    store.rows["ag-infra-1"]["metadata"] = {"git.commits": json.dumps(["f" * 40])}
    second = _commit(repo, "fix(x): more\n\nRefs: ag-infra-1, ag-infra-2")
    plan_path = tmp_path / "commits.bdlg"
    _invoke("backfill", "commits", "--plan", str(plan_path))

    audit = json.loads(
        _invoke("backfill", "commits", "--apply", "--plan", str(plan_path), "--json").stdout
    )

    assert audit["state"] == "applied" and audit["pass"] == "commits"
    assert audit["verification"]["second_dry_run_changes"] == 0
    assert audit["progress"]["completed"] == 2
    assert {op for op, _ in store.writes} == {"update"}
    assert json.loads(store.rows["ag-infra-1"]["metadata"]["git.commits"]) == [
        "f" * 40,
        first,
        second,
    ]
    assert json.loads(store.rows["ag-infra-2"]["metadata"]["git.commits"]) == [second]
    assert (tmp_path / "commits.audit.json").is_file()
    assert (tmp_path / "commits.pre-state.jsonl").is_file()
    assert refreshed == [""]  # the coverage seam ran once, after the applied pass

    writes = len(store.writes)
    text = _invoke("backfill", "commits").stdout
    assert "commits: 0 beads would change" in text and len(store.writes) == writes


def test_commits_apply_uses_the_planned_rev_when_the_branch_moves(hive, tmp_path) -> None:
    store, repo, _ = hive
    _commit(repo, "feat: one (ag-infra-1)")
    plan_path = tmp_path / "commits.bdlg"
    _invoke("backfill", "commits", "--plan", str(plan_path))
    _commit(repo, "feat: two (ag-infra-1)")  # lands between the dry run and the apply

    audit = json.loads(
        _invoke("backfill", "commits", "--apply", "--plan", str(plan_path), "--json").stdout
    )
    assert audit["state"] == "applied"
    assert len(json.loads(store.rows["ag-infra-1"]["metadata"]["git.commits"])) == 1


def test_a_failed_write_rolls_the_apply_back_and_audits_it(hive, tmp_path) -> None:
    store, repo, refreshed = hive
    _commit(repo, "feat: both (ag-infra-1, ag-infra-2)")
    plan_path = tmp_path / "commits.bdlg"
    _invoke("backfill", "commits", "--plan", str(plan_path))
    before = copy.deepcopy(store.rows)
    store.fail = lambda op, bead: op == "update" and bead == "ag-infra-2"  # the second write fails

    result = _invoke("backfill", "commits", "--apply", "--plan", str(plan_path), code=1)

    assert "apply failed and was audited" in result.output
    audit = json.loads((tmp_path / "commits.audit.json").read_text())
    assert audit["state"] == "rolled_back" and audit["recovery"]["rollback_complete"] is True
    store.fail = lambda op, bead: False
    assert "git.commits" not in store.rows["ag-infra-1"].get("metadata", {})
    assert {k: v for k, v in store.rows["ag-infra-2"].items() if k != "updated_at"} == {
        k: v for k, v in before["ag-infra-2"].items() if k != "updated_at"
    }
    assert refreshed == []  # a failed apply refreshes nothing


def test_apply_demands_its_own_plan_and_ignores_options(hive, tmp_path) -> None:
    store, repo, _ = hive
    _commit(repo, "feat: one (ag-infra-1)")
    missing = CliRunner().invoke(cli.app, ["backfill", "commits", "--apply"], env=ENV)
    assert missing.exit_code == 1 and "--apply requires --plan" in missing.output

    plan_path = tmp_path / "commits.bdlg"
    _invoke("backfill", "commits", "--plan", str(plan_path))
    wrong = CliRunner().invoke(
        cli.app, ["backfill", "planning", "--apply", "--plan", str(plan_path)], env=ENV
    )
    assert wrong.exit_code == 1 and "is a 'commits' plan, not a planning plan" in wrong.output
    extra = CliRunner().invoke(
        cli.app,
        ["backfill", "commits", "--apply", "--plan", str(plan_path), "--option", "rev=HEAD"],
        env=ENV,
    )
    assert extra.exit_code == 1 and "records its options" in extra.output
    assert store.writes == []


def test_an_unknown_option_is_refused(hive) -> None:
    result = _invoke("backfill", "docs", "--option", "rev=HEAD", code=1)
    assert "unknown option(s) rev" in result.output
    assert "not key=value" in _invoke("backfill", "docs", "--option", "labels", code=1).output


def test_the_neutral_apply_now_applies_a_commits_plan(hive, tmp_path) -> None:
    store, repo, _ = hive
    sha = _commit(repo, "feat: one (ag-infra-1)")
    plan_path = tmp_path / "commits.bdlg"
    _invoke("backfill", "commits", "--plan", str(plan_path))

    audit = json.loads(_invoke("backfill", "apply", str(plan_path), "--json").stdout)

    assert audit["state"] == "applied" and audit["pass"] == "commits"
    assert json.loads(store.rows["ag-infra-1"]["metadata"]["git.commits"]) == [sha]


# -- docs --------------------------------------------------------------------------------------


def _docs(repo: Path) -> None:
    _commit(
        repo,
        "docs: add design docs",
        {
            DOC_A: "# Stamped\n\nBeads: ag-infra-1\n",
            DOC_B: "# Something entirely unrelated\n\nNo bead here.\n",
        },
    )


def test_docs_stamps_external_ref_and_prints_the_residual_with_the_judge_off(
    hive, tmp_path
) -> None:
    store, repo, _ = hive
    _docs(repo)
    plan_path = tmp_path / "docs.bdlg"

    text = _invoke("backfill", "docs", "--plan", str(plan_path)).stdout
    assert f"RESIDUAL {DOC_B}: undecided; needs human confirm" in text
    assert "judge: off; judge calls: 0" in text and "docs: 1 beads would change" in text
    assert store.writes == []

    audit = json.loads(
        _invoke("backfill", "docs", "--apply", "--plan", str(plan_path), "--json").stdout
    )
    assert audit["state"] == "applied" and audit["verification"]["second_dry_run_changes"] == 0
    assert store.rows["ag-infra-1"]["external_ref"] == DOC_A
    assert "external_ref" not in store.rows["ag-infra-2"]
    assert len(store.rows) == 2  # the residual created nothing

    again = _invoke("backfill", "docs").stdout
    assert "docs: 0 beads would change" in again and f"RESIDUAL {DOC_B}" in again


def test_docs_with_backfill_judge_configured_creates_the_judged_new_bead(
    hive, tmp_path, monkeypatch
) -> None:
    store, repo, _ = hive
    _docs(repo)
    judge = OneJudge()
    monkeypatch.setattr(
        backfill_cli, "judge_settings", lambda cfg=None: JudgeSettings(enabled=True)
    )
    monkeypatch.setattr(backfill_cli, "build_judge", lambda settings: judge)
    plan_path = tmp_path / "docs.bdlg"

    text = _invoke(
        "backfill", "docs", "--option", "labels=complexity:MEDIUM", "--plan", str(plan_path)
    ).stdout
    assert "judge: on" in text and "RESIDUAL" not in text and judge.calls >= 1
    asked = judge.calls

    audit = json.loads(
        _invoke("backfill", "docs", "--apply", "--plan", str(plan_path), "--json").stdout
    )
    assert audit["state"] == "applied" and judge.calls == asked  # recorded verdicts only
    created = [row for iid, row in store.rows.items() if iid not in ("ag-infra-1", "ag-infra-2")]
    assert len(created) == 1 and created[0]["external_ref"] == DOC_B
    minted = [iid for iid in store.rows if iid not in ("ag-infra-1", "ag-infra-2")]
    assert all(iid.startswith("ag-infra-") for iid in minted)  # the hive's configured prefix
    assert {"origin:backfill", "source:docs", "complexity:MEDIUM", *TRIPLET} <= set(
        created[0]["labels"]
    )
    assert store.rows["ag-infra-1"]["external_ref"] == DOC_A

    assert "docs: 0 beads would change" in _invoke("backfill", "docs").stdout


# -- planning ----------------------------------------------------------------------------------

TREE = {
    f"{P1}/01-01-PLAN.md": "---\ndepends_on: []\n---\n# Foundation: set up\n",
    f"{P1}/01-02-PLAN.md": "---\ndepends_on:\n  - 01-01\n---\n# Foundation: wire CI\n",
}


def test_planning_creates_beads_with_a_tier_and_applies_a_later_close(hive, tmp_path) -> None:
    store, repo, _ = hive
    _commit(repo, "docs: add the planning tree", TREE)
    plan_path = tmp_path / "planning.bdlg"
    _invoke(
        "backfill", "planning", "--option", "labels=complexity:MEDIUM", "--plan", str(plan_path)
    )
    assert store.writes == []

    audit = json.loads(
        _invoke("backfill", "planning", "--apply", "--plan", str(plan_path), "--json").stdout
    )
    assert audit["state"] == "applied" and audit["verification"]["second_dry_run_changes"] == 0
    created = {k: v for k, v in store.rows.items() if k not in ("ag-infra-1", "ag-infra-2")}
    assert len(created) == 3
    for iid, row in created.items():
        assert {"origin:backfill", "source:planning", *TRIPLET} <= set(row["labels"])
        assert validate.bead_violations(None, iid, row["labels"], row["issue_type"]) == []
    assert "planning: 0 beads would change" in _invoke("backfill", "planning").stdout

    # a summary appears later: the close of an existing bead is DRIFT and is applied
    _commit(repo, "docs: summarize 01-01", {f"{P1}/01-01-SUMMARY.md": "# done\n"})
    closing = tmp_path / "closing.bdlg"
    text = _invoke("backfill", "planning", "--plan", str(closing)).stdout
    assert "planning: 1 beads would change" in text and "DRIFT=1" in text
    _invoke("backfill", "planning", "--apply", "--plan", str(closing))
    assert sorted(row["status"] for row in created_rows(store, created)) == [
        "closed",
        "open",
        "open",
    ]
    assert "planning: 0 beads would change" in _invoke("backfill", "planning").stdout


def created_rows(store: FakeBd, created: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [store.rows[iid] for iid in created]
