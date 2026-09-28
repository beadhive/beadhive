"""SAFE/skip partition and untrustworthy-hive withholding policy (bh-qdezo.7).

Moved from ``tests/test_worktree.py`` (root): ``test_retained_is_skipped_by_two_consecutive_
prune_classifications``, ``test_prune_withholds_a_hive_that_carries_an_unknown_row``, and
``test_prune_still_prunes_a_healthy_hive_in_the_same_run`` all exercised the pure ``.safe``
partition and the UNKNOWN-taint withholding rule directly, with no root-only I/O, rendering, or
grouping in the mix — exactly ``split_safe_skipped`` / ``withhold_untrustworthy`` moved here.
"""

from __future__ import annotations

from beadhive_worktrees import (
    WtClassification,
    WtStatus,
    split_safe_skipped,
    withhold_untrustworthy,
)


def _status(
    hive: str,
    leaf: str,
    classification: WtClassification,
    *,
    safe: bool = False,
) -> WtStatus:
    return WtStatus(
        hive=hive,
        leaf=leaf,
        branch=f"wt/bead/issue/{leaf}",
        path=f"/wts/{leaf}",
        bead_id=leaf,
        classification=classification,
        merged=safe,
        dirty=False,
        safe=safe,
    )


def _unknown_status(hive: str = "mr") -> WtStatus:
    return WtStatus(
        hive=hive,
        leaf="u-1",
        branch="wt/bead/issue/u-1",
        path="/wts/u-1",
        bead_id="u-1",
        classification=WtClassification.UNKNOWN,
        merged=False,
        dirty=False,
        safe=False,
        unknown_reason="the bead store could not be read",
    )


def test_split_safe_skipped_partitions_by_the_safe_flag() -> None:
    safe = _status("mr", "s-1", WtClassification.SAFE, safe=True)
    retained = _status("mr", "old", WtClassification.RETAINED)

    safe_set, skipped = split_safe_skipped([safe, retained])

    assert safe_set == [safe]
    assert skipped == [retained]


def test_split_safe_skipped_is_stable_across_repeated_calls() -> None:
    retained = _status("mr", "old", WtClassification.RETAINED)

    first = split_safe_skipped([retained])
    second = split_safe_skipped([retained])

    assert first == ([], [retained])
    assert second == ([], [retained])


def test_withhold_untrustworthy_drops_the_whole_tainted_hive() -> None:
    safe = _status("mr", "s-1", WtClassification.SAFE, safe=True)

    kept, skipped, tainted = withhold_untrustworthy([safe], [_unknown_status()])

    assert kept == []
    assert safe in skipped
    assert tainted == {"mr"}


def test_withhold_untrustworthy_still_prunes_a_healthy_hive_in_the_same_run() -> None:
    """Scoped to the affected HIVE, not the whole run — a guard that punishes unrelated hives
    is a guard someone disables."""
    healthy = _status("other", "s-2", WtClassification.SAFE, safe=True)

    kept, _skipped, tainted = withhold_untrustworthy([healthy], [_unknown_status(hive="mr")])

    assert kept == [healthy]
    assert tainted == {"mr"}
