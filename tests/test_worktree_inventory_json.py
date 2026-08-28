"""Versioned managed-worktree inventory and exact-count contract."""

from __future__ import annotations

import json

import pytest
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
    state: str = "complete",
    reason: str | None = None,
    revision: str = "git:r1",
) -> dict:
    return {
        "hive_id": hive_id,
        "hive_prefix": prefix,
        "state": state,
        "reason": reason,
        "revision": revision,
        "statuses": list(statuses),
    }


def test_complete_empty_inventory_reports_true_zero() -> None:
    payload = worktree.inventory_payload([_observation()], generated_at=1000)

    assert payload["schema_version"] == 1
    assert payload["command"] == "worktree list"
    assert payload["coverage"]["state"] == "complete"
    assert payload["freshness"] == {"state": "fresh", "as_of": 1000}
    assert payload["worktrees"] == []
    assert payload["total"] == 0
    assert payload["counts"] == [
        {
            "hive_id": "github/beadhive/beadhive",
            "hive_prefix": "bh",
            "total": 0,
            "by_state": {},
        }
    ]


def test_items_have_exact_identity_and_stable_active_and_retention_states() -> None:
    active = _status("bh-1")
    reclaimable = _status("bh-2", wt_status.WtClassification.SAFE, safe=True)

    payload = worktree.inventory_payload([_observation(active, reclaimable)], generated_at=1000)

    assert [item["state"] for item in payload["worktrees"]] == ["active", "safe"]
    assert [item["retention"] for item in payload["worktrees"]] == [
        "retained",
        "reclaimable",
    ]
    assert payload["worktrees"][0] == {
        "hive_id": "github/beadhive/beadhive",
        "hive_prefix": "bh",
        "bead_id": "bh-1",
        "worktree_id": "github/beadhive/beadhive:bh-1",
        "leaf": "bh-1",
        "branch": "wt/bead/issue/bh-1",
        "path": "/managed/github/beadhive/beadhive/bh-1",
        "state": "active",
        "retention": "retained",
        "merged": False,
        "dirty": False,
        "safe": False,
        "underlying_state": None,
        "unknown_reason": None,
    }
    assert payload["counts"][0]["by_state"] == {"active": 1, "safe": 1}


def test_partial_page_keeps_exact_full_snapshot_totals_and_opaque_cursor() -> None:
    observation = _observation(_status("bh-1"), _status("bh-2"), _status("bh-3"))

    first = worktree.inventory_payload([observation], limit=1, generated_at=1000)
    second = worktree.inventory_payload(
        [observation], limit=1, cursor=first["next_cursor"], generated_at=1001
    )

    assert first["returned"] == 1
    assert first["truncated"] is True
    assert first["total"] == 3
    assert first["counts"][0]["total"] == 3
    assert first["next_cursor"] and "/" not in first["next_cursor"]
    assert second["worktrees"][0]["bead_id"] == "bh-2"


def test_state_filter_is_cursor_scoped_and_has_exact_matched_total() -> None:
    observation = _observation(
        _status("bh-1"), _status("bh-2", wt_status.WtClassification.SAFE, safe=True)
    )
    filtered = worktree.inventory_payload([observation], states=("safe",), generated_at=1000)

    assert [item["bead_id"] for item in filtered["worktrees"]] == ["bh-2"]
    assert filtered["total"] == 1
    assert filtered["counts"][0]["total"] == 2

    paged = worktree.inventory_payload([observation], limit=1, generated_at=1000)
    with pytest.raises(ValueError, match="different filters"):
        worktree.inventory_payload(
            [observation], states=("safe",), cursor=paged["next_cursor"], generated_at=1000
        )


@pytest.mark.parametrize(
    ("source_state", "expected_coverage", "expected_freshness"),
    [
        ("partial", "partial", "fresh"),
        ("stale", "stale", "stale"),
        ("unavailable", "unavailable", "unknown"),
    ],
)
def test_incomplete_coverage_never_publishes_counts(
    source_state: str, expected_coverage: str, expected_freshness: str
) -> None:
    statuses = () if source_state == "unavailable" else (_status("bh-1"),)
    payload = worktree.inventory_payload(
        [
            _observation(
                *statuses,
                state=source_state,
                reason=f"source is {source_state}",
                revision="" if source_state == "unavailable" else "git:r1",
            )
        ],
        generated_at=1000,
    )

    assert payload["coverage"]["state"] == expected_coverage
    assert payload["freshness"]["state"] == expected_freshness
    assert payload["total"] is None
    assert payload["counts"] is None
    assert payload["warnings"][0]["code"] == f"worktree_source_{source_state}"
    if source_state == "unavailable":
        assert payload["source_revision"] is None


def test_one_failed_hive_makes_a_mixed_inventory_partial_without_hiding_items() -> None:
    payload = worktree.inventory_payload(
        [
            _observation(_status("bh-1")),
            _observation(
                hive_id="github/acme/widgets",
                prefix="wdg",
                state="unavailable",
                reason="git failed",
                revision="",
            ),
        ],
        generated_at=1000,
    )

    assert payload["coverage"]["state"] == "partial"
    assert [item["bead_id"] for item in payload["worktrees"]] == ["bh-1"]
    assert payload["total"] is None
    assert payload["counts"] is None


def test_unknown_classification_downgrades_an_asserted_complete_source() -> None:
    unknown = _status("bh-1", wt_status.WtClassification.UNKNOWN)
    payload = worktree.inventory_payload([_observation(unknown)], generated_at=1000)

    assert payload["coverage"]["state"] == "partial"
    assert payload["total"] is None
    assert payload["counts"] is None


def test_cursor_is_invalidated_when_source_revision_changes() -> None:
    first_observation = _observation(_status("bh-1"), _status("bh-2"), revision="git:r1")
    first = worktree.inventory_payload([first_observation], limit=1, generated_at=1000)
    changed = _observation(_status("bh-1"), _status("bh-2"), revision="git:r2")

    with pytest.raises(ValueError, match="changed"):
        worktree.inventory_payload(
            [changed], limit=1, cursor=first["next_cursor"], generated_at=1001
        )


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
