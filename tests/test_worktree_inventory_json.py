"""Versioned managed-worktree inventory envelope and CLI wiring.

The pure payload fold (paging, coverage, cursors, counts) is
``beadhive_worktrees.build_inventory_payload``, proven on fakes in
``packages/beadhive-worktrees/tests/test_inventory_payload.py`` (bh-qdezo.7) — this file keeps
only what is uniquely root's: the `schema_version`/`command` envelope `impl_inventory_payload`
adds on top, and the one CLI-wiring proof that `bh worktree list --json` threads its `--hive`
filter through to `_inventory_observations` and that the human path still short-circuits before
ever touching the payload builder.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import worktree, wt_status
from beadhive.cli import app

runner = CliRunner()


def _status(
    bead: str,
    state: wt_status.WtClassification = wt_status.WtClassification.ACTIVE,
    *,
    safe: bool = False,
) -> wt_status.WtStatus:
    return wt_status.WtStatus(
        hive="bh",
        leaf=bead,
        branch=f"wt/bead/issue/{bead}",
        path=f"/managed/github/beadhive/beadhive/{bead}",
        bead_id=bead,
        classification=state,
        merged=safe,
        dirty=False,
        safe=safe,
    )


def _observation(
    *statuses: wt_status.WtStatus,
    hive_id: str = "github/beadhive/beadhive",
    prefix: str = "bh",
) -> dict:
    return {
        "hive_id": hive_id,
        "hive_prefix": prefix,
        "state": "complete",
        "reason": None,
        "revision": "git:r1",
        "statuses": list(statuses),
    }


def test_impl_inventory_payload_merges_the_schema_version_envelope() -> None:
    payload = worktree.inventory_payload([_observation(_status("bh-1"))], generated_at=1000)

    assert payload["schema_version"] == 1
    assert payload["command"] == "worktree list"
    # The rest of the shape is the package's own contract — just confirm it rode along flat.
    assert payload["worktrees"][0]["bead_id"] == "bh-1"


def test_cli_json_passes_exact_hive_filter_and_human_list_is_unchanged(monkeypatch, capsys) -> None:
    seen: list[str] = []
    observation = _observation(_status("bh-1"))
    monkeypatch.setattr(
        "beadhive.worktree_inventory._inventory_observations",
        lambda hive="": seen.append(hive) or [observation],
    )

    result = runner.invoke(
        app,
        ["worktree", "list", "--json", "--hive", "github/beadhive/beadhive", "--limit", "1"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert seen == ["github/beadhive/beadhive"]
    assert payload["filters"]["hive"] == "github/beadhive/beadhive"

    monkeypatch.setattr(worktree.config, "load", lambda: {})
    monkeypatch.setattr(worktree, "managed", lambda _cfg: [])
    monkeypatch.setattr(worktree, "unregistered_worktrees", lambda _cfg: [])
    worktree.list_cmd()
    assert capsys.readouterr().out == "no managed worktrees\n"
