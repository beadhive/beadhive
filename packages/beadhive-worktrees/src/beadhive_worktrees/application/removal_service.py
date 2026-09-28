"""Sequencing immediately around the ONE removal effect for `rm` and SAFE prune (bh-qdezo.7).

Every Beadhive worktree removal routes through exactly one release-then-remove effect
(``WorktreeLifecycleService.remove``, composed at root as ``_remove_worktree``, bh-cb4jo.2). This
module does not re-implement or wrap that effect a second time — it only sequences what a caller
does immediately around it: resolve the private claim-authority record BEFORE the effect runs
(git deletes a linked worktree's own admin metadata as part of removal, so the record path has
to be captured first) and retire it only once the effect has actually succeeded, then reclaim
now-empty parent directories.

Deliberately narrow: telemetry recording, CLI echo, and metadata-cache invalidation stay root
concerns (this package never imports telemetry, the CLI framework, or the metadata cache).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..contracts.state_ports import ClaimRecords
from ..domain import WorktreeManagerError, WorktreeRemoved


@dataclass(frozen=True, slots=True)
class RemovalOutcome:
    """Result of one ``execute_removal`` call.

    ``error`` carries the manager's captured stderr (or a fallback message) when ``ok`` is
    False — never raised, so a caller decides for itself how to report or exit. ``returncode``
    preserves the manager's own exit code (``WorktreeManagerError.returncode``) so a CLI caller
    can exit with the identical code the raised exception used to carry.
    """

    ok: bool
    removed: WorktreeRemoved | None = None
    error: str = ""
    returncode: int = 0


def execute_removal(
    remove_fn: Callable[[], WorktreeRemoved],
    *,
    claim_records: ClaimRecords,
    target: Path,
) -> RemovalOutcome:
    """Resolve the claim record, run the ONE removal effect, and retire the record on success.

    ``target``'s claim-record path is resolved before ``remove_fn`` runs, unconditionally —
    exactly the ordering ``worktree_cleanup.py``'s ``impl_remove`` and ``impl__prune_remove_one``
    both used. A ``WorktreeManagerError`` from ``remove_fn`` is reported, not raised, and the
    record is left in place (the worktree was not actually removed).
    """
    claim_path = claim_records.record_path(target)
    try:
        removed = remove_fn()
    except WorktreeManagerError as exc:
        return RemovalOutcome(ok=False, error=exc.error or str(exc), returncode=exc.returncode)
    claim_records.remove_record_path(claim_path)
    return RemovalOutcome(ok=True, removed=removed)


def reclaim_empty_parents(leaf_path: Path, root: Path, *, enabled: bool = True) -> None:
    """Climb from a removed worktree's parent toward the shadow root, removing now-empty
    triplet dirs.

    ``Path.rmdir`` only deletes EMPTY dirs (raises otherwise) — that is the safety: a non-empty
    dir (another live worktree) stops the climb, and the root itself is never removed. Disabled
    by the caller when ``worktrees.rmdir_empty: false`` is configured (absent => enabled; that
    config read stays root's job).
    """
    if not enabled:
        return
    resolved_root = root.resolve()
    current = Path(leaf_path).parent.resolve()
    while resolved_root in current.parents and current != resolved_root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


__all__ = ["RemovalOutcome", "execute_removal", "reclaim_empty_parents"]
