"""Integration: ``bh backfill`` over a REAL ``bd`` store (embedded Dolt in ``tmp_path``).

Proves on real ``bd list`` / ``bd export`` rows what ``tests/test_backfill_cli.py`` proves over
the in-memory fake: the flat ``backfill-complexity`` and the nested ``backfill complexity`` plan
identically, the complexity apply (now through the engine) converges real labels and leaves
``model:*`` alone, and ``export`` then ``inspect`` / ``verify`` round-trips the hive.

Marked ``integration``; self-skips without ``bd`` on PATH. Nothing here touches a real hive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import beadlog
from beadhive import backfill_bd, backfill_cli, bd, cli, complexity_backfill
from harness.beads import _SHARED_SERVER_ENV, bd_json, create, init_embedded, skip_if_no_bd
from harness.beads import bd as run_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

IDENT = ("github", "agentguides", "infra")
ENTRY = {"provider": "github", "org": "agentguides", "repo": "infra", "prefix": "ag-infra"}
ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}


@pytest.fixture
def hive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in _SHARED_SERVER_ENV:
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "config.yaml"
    config.write_text((Path(__file__).parent / "fixture_config.yaml").read_text())
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setenv("BH_CONFIG", str(config))
    monkeypatch.setenv("BH_DEV", "dev/backfill-int")
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: IDENT)
    monkeypatch.setattr(backfill_bd, "workspace_identity", lambda cwd=None: IDENT)
    root = tmp_path / "hive"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    init_embedded(root, "ag-infra")
    monkeypatch.setattr(complexity_backfill.registry, "hive_dir_for", lambda cfg, hive: root)
    monkeypatch.setattr(
        backfill_cli, "_target", lambda hive: (root, backfill_cli.hive_group(ENTRY, root))
    )
    return root


def _invoke(*args: str) -> str:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == 0, result.output
    return result.stdout


def _labels(root: Path, bead: str) -> list[str]:
    return sorted((bd_json("show", bead, cwd=root) or [{}])[0].get("labels") or [])


def test_complexity_plans_identically_and_converges_real_labels(hive: Path, tmp_path) -> None:
    fresh = create(hive, "Implement a Python API and unit test")
    repaired = create(hive, "Fix a typo in the README")
    run_bd("label", "add", repaired, "complexity:bogus", cwd=hive, capture=True)
    run_bd("label", "add", repaired, "model:sonnet", cwd=hive, capture=True)

    flat = _invoke("backfill-complexity", "--json", "--plan", str(tmp_path / "flat.json"))
    nested = _invoke("backfill", "complexity", "--json", "--plan", str(tmp_path / "plan.json"))
    assert flat == nested
    assert (tmp_path / "flat.json").read_text() == (tmp_path / "plan.json").read_text()
    plan = json.loads(nested)
    assert plan["totals"]["changes"] == 2

    audit = json.loads(
        _invoke(
            "backfill", "complexity", "--apply", "--plan", str(tmp_path / "plan.json"), "--json"
        )
    )
    assert audit["state"] == "applied"
    assert audit["completed"] == sorted([fresh, repaired])
    assert audit["second_dry_run_changes"] == 0
    assert (tmp_path / "plan.pre-state.jsonl").is_file()
    targets = {entry["id"]: entry["target_label"] for entry in plan["entries"]}
    assert _labels(hive, fresh) == [targets[fresh]]
    assert _labels(hive, repaired) == sorted([targets[repaired], "model:sonnet"])
    again = json.loads(_invoke("backfill-complexity", "--json"))
    assert again["totals"]["changes"] == 0


def test_export_then_inspect_and_verify_round_trip_a_real_hive(hive: Path, tmp_path) -> None:
    first = create(hive, "Old work")
    create(hive, "Other work")
    run_bd("label", "add", first, "area:cli", cwd=hive, capture=True)
    archive = tmp_path / "hive.bdlg"

    exported = json.loads(_invoke("backfill", "export", "--out", str(archive), "--json"))
    summary = json.loads(_invoke("backfill", "inspect", str(archive), "--json"))
    verified = json.loads(_invoke("backfill", "verify", str(archive), "--json"))

    group = backfill_cli.hive_group(ENTRY, hive)
    assert summary["groups"] == [group.to_json()] == [exported["group"]]
    assert summary["counts"]["beads"] == exported["beads"] == 2
    assert summary["counts"]["events"] == exported["events"] == verified["events"]
    _manifest, events = beadlog.read_archive(archive)
    states = beadlog.fold(events)
    assert "area:cli" in states[beadlog.BeadRef(group.id, first)].labels
    # Re-exporting an unchanged hive reproduces the same event ids.
    again = tmp_path / "again.bdlg"
    _invoke("backfill", "export", "--out", str(again))
    assert {e.id for e in beadlog.read_archive(again)[1]} == {e.id for e in events}
