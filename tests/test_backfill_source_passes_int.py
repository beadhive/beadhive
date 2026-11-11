"""Integration: ``bh backfill commits|docs|planning`` applied to a REAL scratch ``bd`` store.

An embedded bd store in ``tmp_path`` (never a live hive) is the target of the real
:class:`BdCliPort`; git is a ``git init`` repository beside it. Proves what ``FakeBd`` assumes of
bd 1.3.0 for these passes: ``metadata.git.commits`` and ``external_ref`` round-trip so the
post-apply projection matches and a second run plans nothing, created planning beads and a later
close land, and the whole envelope (pre-state, audit, verification) holds on the real store.

Marked ``integration``; self-skips without ``bd`` on PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import beadlog
from beadhive import backfill_bd, backfill_cli, bd, cli
from beadhive.backfill_bd import BdCliPort, BdStoreLoader, stamp_labels
from harness.beads import _SHARED_SERVER_ENV, bd_json, init_embedded, skip_if_no_bd
from harness.beads import bd as run_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
IDENT = ("github", "agentguides", "infra")
TRIPLET = ("provider:github", "org:agentguides", "repo:infra")
ENTRY = {"provider": "github", "org": "agentguides", "repo": "infra", "prefix": "ag-infra"}
P1 = ".planning/phases/01-foundation"
DOC = "docs/design/001-linked.md"
# Every commit carries this one date on purpose: a later commit in the same second as one
# already recorded must still be appended (bh-hxodo), whatever the shas hash to.
WHEN = "2026-09-01T10:00:00+00:00"


@pytest.fixture
def hive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in _SHARED_SERVER_ENV:
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "config.yaml"
    config.write_text((Path(__file__).parent / "fixture_config.yaml").read_text())
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setenv("BH_CONFIG", str(config))
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: IDENT)
    monkeypatch.setattr(backfill_bd, "workspace_identity", lambda cwd=None: IDENT)
    root = tmp_path / "hive"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "t@example.com", cwd=root)
    git("config", "user.name", "T", cwd=root)
    git("config", "commit.gpgsign", "false", cwd=root)
    init_embedded(root, "ag-infra")
    group = backfill_cli.hive_group(ENTRY, root)
    monkeypatch.setattr(backfill_cli, "_target", lambda hive: (root, group))
    monkeypatch.setattr(backfill_cli, "hive_prefix", lambda hive: ENTRY["prefix"])

    def loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
        port = BdCliPort(cwd, actor="dev/loader")
        return BdStoreLoader(port, group=group, pre_state_path=pre_state_path)

    monkeypatch.setattr(backfill_cli, "store_loader", loader)
    monkeypatch.setattr(
        backfill_cli, "refresh_coverage_after_apply", lambda hive, quiet=False: None
    )
    seed = [
        beadlog.mint_event(
            "create",
            beadlog.BeadRef(group.id, iid),
            {"type": "task", "fields": {"title": f"Seed {iid}", "priority": 2}},
            occurred_at="2026-08-01T00:00:00Z",
            recorded_at=None,
            extracted_at="2026-10-31T00:00:00Z",
            source=("seed", iid),
            provenance="inferred",
        )
        for iid in ("ag-infra-1", "ag-infra-2")
    ]
    loader(root, group=group.id).load(stamp_labels(seed, [*TRIPLET]), dry_run=False)
    return root


def _commit(root: Path, message: str, files: dict[str, str] | None = None) -> str:
    for name, text in (files or {}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_DATE": WHEN, "GIT_COMMITTER_DATE": WHEN}
    for args in (["add", "-A"], ["commit", "-q", "--allow-empty", "-m", message]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _invoke(*args: str) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == 0, result.output
    return result


def _show(root: Path, iid: str) -> dict[str, Any]:
    row = bd_json("show", iid, cwd=root)
    return row[0] if isinstance(row, list) else row


def test_all_three_passes_apply_drift_and_new_to_a_real_bd_store_and_converge(
    hive: Path, tmp_path: Path
) -> None:
    # commits: an append to an existing bead is DRIFT
    first = _commit(hive, "feat(x): start (ag-infra-1)")
    plan = tmp_path / "commits.bdlg"
    _invoke("backfill", "commits", "--plan", str(plan))
    audit = json.loads(
        _invoke("backfill", "commits", "--apply", "--plan", str(plan), "--json").stdout
    )
    assert audit["state"] == "applied" and audit["verification"]["second_dry_run_changes"] == 0
    assert json.loads(_show(hive, "ag-infra-1")["metadata"]["git.commits"]) == [first]
    assert (tmp_path / "commits.pre-state.jsonl").is_file()
    assert "commits: 0 beads would change" in _invoke("backfill", "commits").stdout

    second = _commit(hive, "fix(x): more (ag-infra-1)")
    _invoke("backfill", "commits", "--plan", str(plan))
    _invoke("backfill", "commits", "--apply", "--plan", str(plan))
    assert json.loads(_show(hive, "ag-infra-1")["metadata"]["git.commits"]) == [first, second]

    # docs: an external_ref stamp on an existing bead is DRIFT
    _commit(hive, "docs: add a design doc", {DOC: "# Linked\n\nBeads: ag-infra-2\n"})
    plan = tmp_path / "docs.bdlg"
    _invoke("backfill", "docs", "--plan", str(plan))
    audit = json.loads(_invoke("backfill", "docs", "--apply", "--plan", str(plan), "--json").stdout)
    assert audit["state"] == "applied" and audit["verification"]["second_dry_run_changes"] == 0
    assert _show(hive, "ag-infra-2")["external_ref"] == DOC
    assert "docs: 0 beads would change" in _invoke("backfill", "docs").stdout

    # planning: NEW creates and edges, then a later close (DRIFT)
    tree = {
        f"{P1}/01-01-PLAN.md": "---\ndepends_on: []\n---\n# Foundation: set up\n",
        f"{P1}/01-02-PLAN.md": "---\ndepends_on:\n  - 01-01\n---\n# Foundation: wire CI\n",
    }
    _commit(hive, "docs: add the planning tree", tree)
    plan = tmp_path / "planning.bdlg"
    _invoke("backfill", "planning", "--option", "labels=complexity:MEDIUM", "--plan", str(plan))
    # `bd import` streams its own progress to stdout, so the audit is read from its file
    _invoke("backfill", "planning", "--apply", "--plan", str(plan))
    audit = json.loads((tmp_path / "planning.audit.json").read_text())
    assert audit["state"] == "applied" and audit["verification"]["second_dry_run_changes"] == 0
    assert "planning: 0 beads would change" in _invoke("backfill", "planning").stdout

    _commit(hive, "docs: summarize 01-01", {f"{P1}/01-01-SUMMARY.md": "# done\n"})
    plan = tmp_path / "closing.bdlg"
    assert (
        "planning: 1 beads would change"
        in _invoke("backfill", "planning", "--plan", str(plan)).stdout
    )
    _invoke("backfill", "planning", "--apply", "--plan", str(plan))
    assert "planning: 0 beads would change" in _invoke("backfill", "planning").stdout
    export = run_bd("export", cwd=hive, capture=True).stdout
    assert export.count('"_type":"issue"') == 5  # two seeds, a phase epic and two plans
    assert export.count('"status":"closed"') == 1  # the plan whose summary appeared later
    assert f'"external_ref":"{P1}"' in export
