"""Versioned managed-worktree inventory contract — pure fold, no envelope (bh-qdezo.7).

Moved from ``tests/test_worktree_inventory_json.py`` (root): these cases exercised
``worktree.inventory_payload`` end to end, but every assertion here is about
``build_inventory_payload``'s own fold — paging, coverage, cursors, counts — none of it about
the ``schema_version``/``command`` envelope or CLI wiring root adds on top. Root keeps exactly
one envelope-shape test and one CLI-wiring test; see that file's remaining cases.
"""

from __future__ import annotations

import pytest

from beadhive_worktrees import WtClassification, WtStatus, build_inventory_payload


def _status(
    bead: str,
    state: WtClassification = WtClassification.ACTIVE,
    *,
    safe: bool = False,
) -> WtStatus:
    return WtStatus(
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
    *statuses: WtStatus,
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
    payload = build_inventory_payload([_observation()], generated_at=1000)

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
    reclaimable = _status("bh-2", WtClassification.SAFE, safe=True)

    payload = build_inventory_payload([_observation(active, reclaimable)], generated_at=1000)

    assert [item["state"] for item in payload["worktrees"]] == ["active", "safe"]
    assert [item["retention"] for item in payload["worktrees"]] == ["retained", "reclaimable"]
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

    first = build_inventory_payload([observation], limit=1, generated_at=1000)
    second = build_inventory_payload(
        [observation], limit=1, cursor=first["next_cursor"], generated_at=1001
    )

    assert first["returned"] == 1
    assert first["truncated"] is True
    assert first["total"] == 3
    assert first["counts"][0]["total"] == 3
    assert first["next_cursor"] and "/" not in first["next_cursor"]
    assert second["worktrees"][0]["bead_id"] == "bh-2"


def test_state_filter_is_cursor_scoped_and_has_exact_matched_total() -> None:
    observation = _observation(_status("bh-1"), _status("bh-2", WtClassification.SAFE, safe=True))
    filtered = build_inventory_payload([observation], states=("safe",), generated_at=1000)

    assert [item["bead_id"] for item in filtered["worktrees"]] == ["bh-2"]
    assert filtered["total"] == 1
    assert filtered["counts"][0]["total"] == 2

    paged = build_inventory_payload([observation], limit=1, generated_at=1000)
    with pytest.raises(ValueError, match="different filters"):
        build_inventory_payload(
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
    payload = build_inventory_payload(
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
    payload = build_inventory_payload(
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
    unknown = _status("bh-1", WtClassification.UNKNOWN)
    payload = build_inventory_payload([_observation(unknown)], generated_at=1000)

    assert payload["coverage"]["state"] == "partial"
    assert payload["total"] is None
    assert payload["counts"] is None


def test_cursor_is_invalidated_when_source_revision_changes() -> None:
    first_observation = _observation(_status("bh-1"), _status("bh-2"), revision="git:r1")
    first = build_inventory_payload([first_observation], limit=1, generated_at=1000)
    changed = _observation(_status("bh-1"), _status("bh-2"), revision="git:r2")

    with pytest.raises(ValueError, match="changed"):
        build_inventory_payload([changed], limit=1, cursor=first["next_cursor"], generated_at=1001)


def test_limit_and_state_validation_fails_closed() -> None:
    with pytest.raises(ValueError, match="limit must be"):
        build_inventory_payload([], limit=0)
    with pytest.raises(ValueError, match="limit must be"):
        build_inventory_payload([], limit=201)
    with pytest.raises(ValueError, match="unknown worktree state"):
        build_inventory_payload([], states=("bogus",))
