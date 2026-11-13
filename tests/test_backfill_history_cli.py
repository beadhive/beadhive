"""``bh backfill history | collate | view``: the history pipeline over a fake bd and a tiny repo.

Each ``--until`` stage stops and leaves its output in the run directory; a rerun resumes from it.
Every bd read and write goes through :class:`FakeBd` behind the real loader; git is a ``git init``
repository per test; the ledger directory is in memory. No network, no wall clock.
"""

from __future__ import annotations

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

from beadhive import backfill_cli, backfill_history, cli
from beadhive.backfill_bd import BdStoreLoader
from beadlog_sources import CachedLedgerDirectory


def InMemoryLedgerDirectory(ledgers):  # noqa: N802 - reads like the class it stands in for
    """A fixed-id ledger directory (the library's own fake is not a public import)."""
    return CachedLedgerDirectory({n: (lambda ids=tuple(i): ids) for n, i in ledgers.items()})


ENV = {"COLUMNS": "200", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
ENTRY = {"provider": "github", "org": "agentguides", "repo": "infra", "prefix": "ag-infra"}
TRIPLET = ("provider:github", "org:agentguides", "repo:infra")


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
    _commit(repo, "feat(core): first change for ag-infra-1", {"core/a.txt": "a"})
    _commit(repo, "feat(web): an entirely unrelated page", {"web/b.txt": "b"})
    store = FakeBd(
        [_row("ag-infra-1"), _row("ag-infra-2", title="Other matter")], identity_labels=TRIPLET
    )
    group = backfill_cli.hive_group(ENTRY, repo)
    monkeypatch.setattr(backfill_cli, "_target", lambda hive: (repo, group))
    monkeypatch.setattr(backfill_cli, "hive_prefix", lambda hive: ENTRY["prefix"])
    monkeypatch.setattr(
        backfill_cli,
        "history_directory",
        lambda hive, cfg, backfill: InMemoryLedgerDirectory(
            {"ag-infra": ["ag-infra-1", "ag-infra-2"], "other": ["oth-1"]}
        ),
    )

    def loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
        return BdStoreLoader(store, group=group, clock=Ticks(), pre_state_path=pre_state_path)

    monkeypatch.setattr(backfill_cli, "store_loader", loader)
    monkeypatch.setattr(
        backfill_cli, "refresh_coverage_after_apply", lambda hive, quiet=False: None
    )
    return store, repo


def _config(cfg_path: Path, **backfill: Any) -> None:
    """Add a ``backfill`` section to the test config file (no patching of the config module)."""
    import yaml

    base = yaml.safe_load(cfg_path.read_text()) or {}
    base["backfill"] = backfill
    cfg_path.write_text(yaml.safe_dump(base))


def _invoke(*args: str, code: int = 0) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == code, result.output
    return result


def _run(tmp_path: Path, *args: str, code: int = 0) -> Any:
    return _invoke("backfill", "history", "--run-dir", str(tmp_path / "run"), *args, code=code)


# -- the stages --------------------------------------------------------------------------------


def test_until_parse_leaves_only_the_evidence(hive, tmp_path) -> None:
    store, _ = hive
    result = _run(tmp_path, "--sources", "git", "--until", "parse", "--json")
    summary = json.loads(result.stdout)
    run = tmp_path / "run"
    assert summary["stages"]["parse"]["artifacts"] > 0
    assert (run / "evidence.jsonl").is_file()
    assert not (run / "collation.json").exists()
    assert not (run / "plan.bdlg").exists()
    assert store.writes == []


def test_until_collate_adds_the_collation(hive, tmp_path) -> None:
    store, _ = hive
    _run(tmp_path, "--sources", "git", "--until", "collate")
    run = tmp_path / "run"
    document = json.loads((run / "collation.json").read_text())
    assert document["schema"].startswith("bdlg.collation/")
    assert document["units"]
    assert not (run / "plan.bdlg").exists()
    assert store.writes == []


def test_collate_verb_is_history_until_collate(hive, tmp_path) -> None:
    _invoke("backfill", "collate", "--run-dir", str(tmp_path / "run"), "--sources", "git")
    assert (tmp_path / "run" / "collation.json").is_file()
    assert not (tmp_path / "run" / "plan.bdlg").exists()


def test_until_plan_seals_a_verifiable_plan_and_writes_nothing(hive, tmp_path) -> None:
    store, _ = hive
    result = _run(tmp_path, "--sources", "git")  # the default stop is plan
    assert "plan hash:" in result.stdout
    run = tmp_path / "run"
    assert (run / "plan.bdlg").is_file()
    assert not (run / "audit.json").exists()
    assert store.writes == []
    _invoke("backfill", "verify", str(run / "plan.bdlg"))
    manifest = json.loads((run / "run.json").read_text())
    assert manifest["schema"] == backfill_history.RUN_SCHEMA
    assert set(manifest) >= {"parse", "collate", "plan"}


def test_create_off_proposes_no_new_bead(hive, tmp_path) -> None:
    store, _ = hive
    _run(tmp_path, "--sources", "git")
    events = _plan_events(tmp_path / "run" / "plan.bdlg")
    assert "create" not in {e["kind"] for e in events}
    assert {e["bead"]["id"] for e in events} <= set(store.rows)


def _plan_events(path: Path) -> list[dict[str, Any]]:
    import zipfile

    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith(".jsonl")]
        return [json.loads(line) for n in names for line in archive.read(n).splitlines() if line]


def test_until_apply_goes_through_the_envelope(hive, tmp_path) -> None:
    store, _ = hive
    result = _run(tmp_path, "--sources", "git", "--until", "apply", "--yes")
    assert "second dry-run empty" in result.stdout
    run = tmp_path / "run"
    assert (run / "audit.json").is_file()
    assert (run / "pre-state.jsonl").exists()
    assert store.writes, "the applied plan wrote to the store"
    commits = store.rows["ag-infra-1"].get("metadata", {}).get("git.commits")
    assert commits, store.rows["ag-infra-1"]


def test_apply_refuses_unreviewed_verdicts_without_yes(hive, tmp_path, monkeypatch) -> None:
    from beadhive_backfill import Plan

    real = Plan.totals.fget  # type: ignore[attr-defined]
    monkeypatch.setattr(Plan, "totals", property(lambda self: {**real(self), "review_required": 2}))
    store, _ = hive
    result = _run(tmp_path, "--sources", "git", "--until", "apply", code=1)
    assert "2 verdict(s) need review" in result.output
    assert store.writes == []
    assert not (tmp_path / "run" / "audit.json").exists()
    _run(tmp_path, "--sources", "git", "--until", "apply", "--yes")
    assert store.writes


def test_standalone_apply_takes_the_history_plan(hive, tmp_path) -> None:
    store, _ = hive
    _run(tmp_path, "--sources", "git")
    _invoke("backfill", "apply", str(tmp_path / "run" / "plan.bdlg"))
    assert store.writes


# -- resume ------------------------------------------------------------------------------------


def test_rerun_resumes_from_the_run_directory(hive, tmp_path) -> None:
    _run(tmp_path, "--sources", "git", "--until", "collate")
    again = json.loads(_run(tmp_path, "--sources", "git", "--until", "plan", "--json").stdout)
    assert again["stages"]["parse"]["reused"] is True
    assert again["stages"]["collate"]["prior"] is True
    refreshed = json.loads(
        _run(tmp_path, "--sources", "git", "--until", "parse", "--refresh", "--json").stdout
    )
    assert refreshed["stages"]["parse"]["reused"] is False


def test_a_new_commit_invalidates_the_evidence(hive, tmp_path) -> None:
    _, repo = hive
    _run(tmp_path, "--sources", "git", "--until", "parse")
    _commit(repo, "feat(core): another change", {"core/c.txt": "c"})
    again = json.loads(_run(tmp_path, "--sources", "git", "--until", "parse", "--json").stdout)
    assert again["stages"]["parse"]["reused"] is False


def test_judge_resume_off_does_not_feed_the_prior(hive, tmp_path, cfg_path) -> None:
    _run(tmp_path, "--sources", "git", "--until", "collate")
    _config(cfg_path, judge={"resume": False})
    again = json.loads(_run(tmp_path, "--sources", "git", "--until", "collate", "--json").stdout)
    assert again["stages"]["collate"]["prior"] is False


# -- sources and config ------------------------------------------------------------------------


def test_github_source_is_off_by_default(hive, tmp_path) -> None:
    result = _run(tmp_path, "--sources", "github", "--until", "parse", code=1)
    assert "backfill.sources.github.enabled" in result.output


def test_unknown_source_is_named(hive, tmp_path) -> None:
    result = _run(tmp_path, "--sources", "nope", "--until", "parse", code=1)
    assert "no reader named 'nope'" in result.output


def test_bad_until_is_refused(hive, tmp_path) -> None:
    result = _run(tmp_path, "--until", "later", code=1)
    assert "--until must be one of" in result.output


def test_docs_formats_reach_the_reader_and_html_is_opt_in(monkeypatch) -> None:
    from beadhive.modules.config.contracts import BackfillConfig

    default = BackfillConfig()
    assert backfill_history.source_options("docs", default, [])["formats"] == "markdown"
    opted = BackfillConfig.model_validate({"sources": {"docs": {"formats": ["markdown", "html"]}}})
    assert backfill_history.source_options("docs", opted, ["a", "b"]) == {
        "formats": "markdown,html",
        "namespaces": "a,b",
    }


def test_namespaces_come_from_the_ledgers_and_options_win() -> None:
    from beadhive.modules.config.contracts import BackfillConfig

    chosen = BackfillConfig.model_validate({"sources": {"git": {"options": {"namespaces": "x"}}}})
    assert backfill_history.source_options("git", chosen, ["a"]) == {"namespaces": "x"}


# -- cost and prior wiring ---------------------------------------------------------------------


def test_cost_per_call_and_prior_reach_the_b5_run(hive, tmp_path, monkeypatch, cfg_path) -> None:
    seen: dict[str, Any] = {}
    real = backfill_history.identify_run

    def spy(settings, **kwargs):
        seen.update(kwargs)
        return real(settings, **kwargs)

    monkeypatch.setattr(backfill_history, "identify_run", spy)
    _config(cfg_path, judge={"cost_per_call_usd": 0.02})
    _run(tmp_path, "--sources", "git", "--until", "collate")
    assert seen["cost_per_call_usd"] == 0.02
    assert seen["prior"] is None
    seen.clear()
    _run(tmp_path, "--sources", "git", "--until", "collate")
    assert seen["prior"] is not None  # the last collation fed back as the prior


def test_ledger_restriction_is_passed_to_the_directory(
    hive, tmp_path, monkeypatch, cfg_path
) -> None:
    got: list[Any] = []

    def directory(hive, cfg, backfill):
        got.append(list(backfill.history.ledgers))
        return InMemoryLedgerDirectory({"ag-infra": ["ag-infra-1"]})

    monkeypatch.setattr(backfill_cli, "history_directory", directory)
    _config(cfg_path, history={"ledgers": ["ag-infra"]})
    _run(tmp_path, "--sources", "git", "--until", "collate")
    assert got == [["ag-infra"]]


# -- views -------------------------------------------------------------------------------------


@pytest.fixture
def collated(hive, tmp_path) -> Path:
    _run(tmp_path, "--sources", "git", "--until", "collate")
    return tmp_path / "run"


@pytest.mark.parametrize("view", ["unit", "conflict", "xref", "cost", "verdicts"])
def test_views_read_a_run_directory_and_emit_json(collated, view) -> None:
    result = _invoke("backfill", "view", view, str(collated), "--json")
    document = json.loads(result.stdout)
    if view != "verdicts":  # verdicts is the existing view, with its own schema
        assert document["view"] == view
    text = _invoke("backfill", "view", view, str(collated)).stdout
    assert text.strip()


def test_unit_view_filters_by_decision_and_reads_the_collation_file(collated) -> None:
    everything = json.loads(
        _invoke("backfill", "view", "unit", str(collated / "collation.json"), "--json").stdout
    )
    assert everything["units"]
    only = json.loads(
        _invoke("backfill", "view", "unit", str(collated), "--decision", "NEW", "--json").stdout
    )
    assert {u["decision"] for u in only["units"]} <= {"NEW"}


def test_unit_view_reports_a_new_unit_with_no_evidence_moment_as_skipped(
    collated, tmp_path
) -> None:
    """bh-uysl1: a NEW unit with no add commit and no declared date is not created."""
    from dataclasses import replace

    from beadlog_sources import Lifecycle, read_collation, write_collation

    collation = read_collation(collated / "collation.json")
    new = {v["unit"] for v in collation.verdicts if v["class"] == "NEW"}
    assert new
    target = sorted(new)[0]
    bare = [
        Lifecycle(r["unit"], "open", "none") if r["unit"] == target else Lifecycle.from_json(r)
        for r in collation.lifecycle
    ]
    edited = tmp_path / "edited.json"
    write_collation(replace(collation, lifecycle=tuple(x.to_json() for x in bare)), edited)
    view = json.loads(_invoke("backfill", "view", "unit", str(edited), "--json").stdout)
    skipped = {u["key"]: u["skipped"] for u in view["units"] if u["skipped"]}
    assert skipped == {target: "no evidence moment"}
    text = _invoke("backfill", "view", "unit", str(edited)).stdout
    assert "skipped: no evidence moment" in text


def test_view_of_an_empty_directory_is_a_clear_error(tmp_path) -> None:
    result = _invoke("backfill", "view", "unit", str(tmp_path), code=1)
    assert "no collation" in result.output


def test_cost_view_uses_the_configured_per_call_cost(collated, cfg_path) -> None:
    _config(cfg_path, judge={"cost_per_call_usd": 0.5})
    view = json.loads(_invoke("backfill", "view", "cost", str(collated), "--json").stdout)
    assert view["cost_per_call_usd"] == 0.5
    assert view["estimated_usd"] == round(view["judge_calls"] * 0.5, 6)


# -- create mode (bh-iv61r seam) -----------------------------------------------------------------


def test_create_propose_proposes_new_beads_with_the_creation_labels(
    hive, tmp_path, monkeypatch, cfg_path
) -> None:
    from beadhive import backfill_create
    from beadlog_sources import ProjectOptions

    labels = ("origin:backfill", *TRIPLET)
    monkeypatch.setattr(
        backfill_create,
        "create_mode_options",
        lambda h, c, m: ProjectOptions(creation_labels=labels),
    )
    _config(cfg_path, collate={"create": "propose"})
    store, _ = hive
    result = _run(tmp_path, "--sources", "git", "--json")
    plan = json.loads(result.stdout)["stages"]["plan"]
    assert plan["create"] == "propose"
    events = _plan_events(tmp_path / "run" / "plan.bdlg")
    assert "create" in {e["kind"] for e in events}
    added = {e["data"].get("label") for e in events if e["kind"] == "label_add"}
    assert "origin:backfill" in added
    assert store.writes == []


def test_a_hive_overlay_decides_the_create_mode_of_its_own_hive(
    hive, tmp_path, monkeypatch, cfg_path
) -> None:
    """bh-g71fi: ``backfill.hives.<prefix>`` over the host block reaches the history run; an
    overlay of another hive, or none, leaves the host's ``create = off`` in force."""
    from beadhive import backfill_create, registry
    from beadlog_sources import ProjectOptions

    labels = ("origin:backfill", *TRIPLET)
    monkeypatch.setattr(
        backfill_create,
        "create_mode_options",
        lambda h, c, m: ProjectOptions(creation_labels=labels),
    )
    monkeypatch.setattr(registry, "current_hive", lambda cfg: ENTRY)
    _config(cfg_path, hives={"someone-else": {"collate": {"create": "propose"}}})
    plan = json.loads(_run(tmp_path, "--sources", "git", "--json").stdout)["stages"]["plan"]
    assert plan["create"] == "off"
    _config(cfg_path, hives={ENTRY["prefix"]: {"collate": {"create": "propose"}}})
    plan = json.loads(_run(tmp_path, "--sources", "git", "--refresh", "--json").stdout)["stages"][
        "plan"
    ]
    assert plan["create"] == "propose"


def test_the_plan_report_carries_unit_counts(hive, tmp_path) -> None:
    plan = json.loads(_run(tmp_path, "--sources", "git", "--json").stdout)["stages"]["plan"]
    assert set(plan["units"]) <= {"NEW", "PRESENT", "NOISE", "residual"}
    assert plan["create"] == "off" and plan["creates_skipped"] >= 0


# -- gap fill of the created beads (bh-79fwx) ----------------------------------------------------


@pytest.fixture
def created_run(hive, monkeypatch, cfg_path):
    """A create-mode ``--until apply`` over the fake store with a recording gap-fill reader."""
    import re

    from beadhive import backfill_create
    from beadlog_sources import GapfillReader, ProjectOptions

    labels = ("origin:backfill", *TRIPLET)
    monkeypatch.setattr(
        backfill_create,
        "create_mode_options",
        lambda h, c, m: ProjectOptions(creation_labels=labels),
    )
    asked: list[list[str]] = []

    def runner(argv):
        sql = argv[5]
        if argv[4] == "--csv":
            asked.append(re.findall(r"'([^']+)'", sql[sql.index(" IN (") :]))
        return "id,commit_hash,d,h\n" if argv[4] == "--csv" else "[]"

    def reader(cwd, settings):
        return GapfillReader(cwd, batch_size=settings.batch_size, runner=runner)

    monkeypatch.setattr(backfill_cli, "gapfill_reader", reader)
    return hive[0], asked


def test_on_created_off_runs_no_gapfill(created_run, tmp_path, cfg_path) -> None:
    _, asked = created_run
    _config(cfg_path, collate={"create": "propose"})
    summary = json.loads(
        _run(tmp_path, "--sources", "git", "--until", "apply", "--yes", "--json").stdout
    )
    assert summary["stages"]["apply"]["completed"] > 0
    assert "gapfill" not in summary["stages"] and asked == []


def test_on_created_gap_fills_only_the_created_beads(created_run, tmp_path, cfg_path) -> None:
    store, asked = created_run
    _config(
        cfg_path,
        collate={"create": "propose"},
        timeline={"gapfill": {"on_created": True, "batch_size": 50}},
    )
    summary = json.loads(
        _run(tmp_path, "--sources", "git", "--until", "apply", "--yes", "--json").stdout
    )
    plan = _plan_events(tmp_path / "run" / "plan.bdlg")
    created = {e["bead"]["id"] for e in plan if e["kind"] == "create"}
    assert created and summary["stages"]["gapfill"]["beads"] == len(created)
    assert [i for batch in asked for i in batch] == sorted(created)
    assert not created & {"ag-infra-1", "ag-infra-2"}  # the existing beads were never asked


# -- a failed create-mode apply (bh-12hka, #152) -------------------------------------------------


def test_a_failed_create_apply_exits_nonzero_retires_what_it_created_and_a_rerun_revives_it(
    created_run, hive, tmp_path, cfg_path
) -> None:
    store, _ = created_run
    repo = hive[1]
    _commit(repo, "feat(api): a third unrelated change", {"api/c.txt": "c"})
    _config(cfg_path, collate={"create": "propose"})
    imports: list[str] = []

    def fail(op: str, bead: str) -> bool:
        if op == "import" and bead not in store.rows:
            imports.append(bead)
            return len(imports) == 2  # the second created bead fails mid-apply
        return False

    store.fail = fail
    before = set(store.rows)
    result = _run(tmp_path, "--sources", "git", "--until", "apply", "--yes", code=1)
    assert "apply failed" in result.output

    created = set(store.rows) - before
    assert created, "the first create landed before the failure"
    assert {store.rows[i]["status"] for i in created} == {"closed"}
    assert {store.rows[i].get("close_reason") for i in created} == {"backfill-rollback"}
    audit = json.loads((tmp_path / "run" / "audit.json").read_text())
    assert audit["state"] == "rolled_back"
    assert set(audit["rollback_retired"]) == created
    assert audit["rollback_retired_count"] == len(created)

    store.fail = lambda op, bead: False
    _run(tmp_path / "again", "--sources", "git", "--until", "apply", "--yes")
    refs = [row.get("external_ref") for row in store.rows.values() if row.get("external_ref")]
    assert len(refs) == len(set(refs)), "no duplicate external_ref after the re-run"
    assert created <= set(store.rows)
    assert not [i for i in created if store.rows[i]["status"] == "open"]
    import beadlog

    folded = beadlog.fold(
        BdStoreLoader(store, group=backfill_cli.hive_group(ENTRY, repo).id).export()
    )
    assert {s.phase for r, s in folded.items() if r.id in created} == {"closed"}  # not retired
