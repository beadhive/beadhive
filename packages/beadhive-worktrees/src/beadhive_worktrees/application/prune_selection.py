"""Pure SAFE/skip selection policy for ``bh worktree prune`` (bh-qdezo.7).

Moved verbatim (byte-identical behavior) from ``worktree_cleanup.py``'s
``impl__prune_classify``'s post-classification split and ``impl__prune_withhold_untrustworthy``.
The concurrent classification itself stays root-adjacent orchestration (it drives git/bd I/O
through the classifier); this module only partitions and re-partitions already-classified rows.
"""

from __future__ import annotations

from ..policy.classification import WtStatus, untrustworthy


def split_safe_skipped(statuses: list[WtStatus]) -> tuple[list[WtStatus], list[WtStatus]]:
    """Partition already-classified rows into the SAFE-to-remove and NOT-SAFE sets."""
    safe_set = [s for s in statuses if s.safe]
    skipped = [s for s in statuses if not s.safe]
    return safe_set, skipped


def withhold_untrustworthy(
    safe_set: list[WtStatus], skipped: list[WtStatus]
) -> tuple[list[WtStatus], list[WtStatus], set[str]]:
    """Drop every hive carrying an UNKNOWN row out of the removal set (bh-167s0).

    UNKNOWN is not ``safe``, so an unresolvable row was never going to be removed — but that is
    not enough, and this is the acceptance criterion that says so: prune must "refuse to run
    unattended over a hive containing UNKNOWN rows". Whatever stopped one bead resolving —
    a store bd will not open, a retired prefix — stopped every OTHER bead in that hive being
    confirmed too, so the SAFE verdicts from the same pass are not evidence either. They are
    withheld, not removed, and the caller says why and exits non-zero.

    Scoped to the affected HIVE rather than the whole run: a second, healthy hive in the same
    ``bh worktree prune`` still prunes, because its answers were never in doubt.
    """
    tainted = {s.hive for s in untrustworthy(safe_set + skipped)}
    if not tainted:
        return safe_set, skipped, tainted
    withheld = [s for s in safe_set if s.hive in tainted]
    return (
        [s for s in safe_set if s.hive not in tainted],
        skipped + withheld,
        tainted,
    )


__all__ = ["split_safe_skipped", "withhold_untrustworthy"]
