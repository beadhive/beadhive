"""ws.wt_status — pure worktree status classifier.

Classifies each managed worktree into one of seven mutually exclusive states from
freshly-fetched data, keeping all I/O out of the classifier itself so the function is
trivially unit-testable.  Callers are responsible for:

  1. Repopulating a fresh RepoMetadata (metadata.invalidate + read_fleet ttl=0 or refresh).
  2. Resolving bead statuses (bd show) for every bead id that appears in the managed rows.
  3. Computing per-worktree dirty flags via worktree_dirty_flags (run git status --porcelain
     per linked worktree path) — RepoMetadata.branches only tracks the main clone's HEAD
     dirty state; linked worktrees require a separate check.

Both the ``ws worktree status`` renderer and the merge-aware ``ws worktree prune`` share the
same classifier so they never disagree on which worktrees are SAFE.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


class WtClassification(StrEnum):
    """Mutually exclusive classifications for a managed worktree."""

    SAFE = "safe"
    """Closed bead + branch merged into parent + no uncommitted changes.  The only class
    that ``ws worktree prune`` will remove."""

    REVIEW = "review"
    """Branch is merged and worktree is clean but the bead is not yet closed.  Waiting on
    a human to close / approve — do not auto-prune."""

    DIRTY = "dirty"
    """Uncommitted working-tree changes detected.  Never auto-pruned regardless of bead
    status or merge state."""

    UNMERGED = "unmerged"
    """Bead is closed but the branch is NOT a git ancestor of its parent branch.  Unusual
    (a bead may have been closed without landing) — not safe to remove."""

    ACTIVE = "active"
    """Bead is open / in-progress — work is actively in progress."""

    DETACHED = "detached"
    """No branch is checked out in this worktree (detached HEAD state)."""

    ABANDONED = "abandoned"
    """Worktree has no corresponding bead id (session or batch worktree with no bead)."""


@dataclass(frozen=True)
class WtStatus:
    """Classification record for one managed worktree."""

    rig: str
    """Rig prefix (e.g. ``workspace``)."""

    leaf: str
    """Last segment of the worktree shadow path — the worktree's directory name."""

    branch: str
    """Current branch name (``(detached)`` when HEAD is not attached to a branch)."""

    path: str
    """Absolute path to the worktree on disk."""

    bead_id: str | None
    """Bead id parsed from the ``wt/bead/<id>`` branch name, or ``None`` for non-bead
    worktrees (session, batch, …)."""

    classification: WtClassification
    """The determined classification for this worktree."""

    merged: bool
    """True iff the branch is a git ancestor of its parent (result of is_merged)."""

    dirty: bool
    """True iff the worktree has uncommitted changes."""

    safe: bool
    """True iff ``classification == SAFE`` — the only invariant that enables auto-prune."""

    def as_dict(self) -> dict:
        """JSON-serializable dict with ``classification`` and ``safe`` as their string/bool
        values — suitable for ``--json`` emission."""
        d = asdict(self)
        d["classification"] = str(self.classification)
        return d


def _branch_dirty(branch: str, meta_branches: list[dict]) -> bool:
    """Look up ``branch`` in the serialized ``RepoMetadata.branches`` list.

    Returns the ``dirty`` flag for the matching entry, or ``False`` when the branch is not
    found.  Note: this only reflects the main clone's checked-out branch dirty state — callers
    should use ``worktree_dirty_flags`` for linked worktrees.
    """
    for b in meta_branches:
        if b.get("name") == branch:
            return bool(b.get("dirty", False))
    return False


def classify(
    rig_prefix: str,
    managed_rows: list[tuple[str, str, str]],
    meta_branches: list[dict],
    bead_statuses: dict[str, str],
    dirty_by_path: dict[str, bool],
    is_merged_fn,
    parent_fn,
    integration: str,
) -> list[WtStatus]:
    """Classify every managed worktree row for one rig.

    Parameters
    ----------
    rig_prefix:
        The rig's prefix string (e.g. ``workspace``).
    managed_rows:
        Rows from ``worktree.managed()`` — a flat list of ``(prefix, path, branch)`` tuples
        for this rig (callers should pre-filter to the target rig).
    meta_branches:
        ``RepoMetadata.branches`` (list of dicts with ``name`` / ``dirty`` / ...) — used as
        a fallback dirty check for the main clone's HEAD branch.
    bead_statuses:
        Mapping ``bead_id -> status`` string (e.g. ``"open"``, ``"in_progress"``,
        ``"closed"``).  Only bead ids that appear in the managed rows need to be present.
    dirty_by_path:
        Pre-computed dirty flags per worktree path.  The caller runs ``git status --porcelain``
        per linked worktree path (the linked working-tree approach; cannot be derived from
        ``meta_branches`` which only tracks the main clone's HEAD).
    is_merged_fn:
        Callable ``(entry, branch, base) -> bool`` — the ``worktree.is_merged`` primitive.
    parent_fn:
        Callable ``(entry, path, integration) -> (bead_id|None, parent_branch)`` —
        ``worktree.bead_and_parent``.
    integration:
        The rig's integration branch name (e.g. ``main``).

    Returns
    -------
    list[WtStatus]
        One entry per managed row in the same order as ``managed_rows``.
    """
    results: list[WtStatus] = []

    for prefix, path, branch in managed_rows:
        leaf = Path(path).name
        is_detached = branch == "(detached)"

        # -- dirty -------------------------------------------------------
        # Prefer the per-path pre-computed flag (accurate for linked worktrees);
        # fall back to the main-clone branch metadata for the main repo's HEAD.
        dirty = dirty_by_path.get(path, _branch_dirty(branch, meta_branches))

        # -- bead id + parent branch -------------------------------------
        # Use a dummy entry (just the prefix key); callers supply is_merged_fn /
        # parent_fn so the entry shape is opaque here.
        entry_stub = {"prefix": prefix}
        bead_id, parent = parent_fn(entry_stub, path, integration)

        # -- merge ancestry ----------------------------------------------
        if is_detached or not branch or branch == "(detached)":
            merged = False
        else:
            merged = is_merged_fn(entry_stub, branch, parent)

        # -- bead status -------------------------------------------------
        bead_status = bead_statuses.get(bead_id or "", "") if bead_id else ""
        bead_closed = bead_status == "closed"

        # -- classification (priority order) -----------------------------
        # Priority:
        #   1. DETACHED  — no branch; cannot determine anything else
        #   2. DIRTY     — uncommitted changes override merge/bead status
        #   3. ABANDONED — no bead id (session/batch worktree, not a bead seat)
        #   4. SAFE      — closed + merged + clean (the prune-eligible state)
        #   5. REVIEW    — merged + clean but bead is not yet closed
        #   6. UNMERGED  — closed but branch not an ancestor of its parent
        #   7. ACTIVE    — open/in-progress bead (default)
        if is_detached:
            cls = WtClassification.DETACHED
        elif dirty:
            cls = WtClassification.DIRTY
        elif bead_id is None:
            cls = WtClassification.ABANDONED
        elif bead_closed and merged:
            cls = WtClassification.SAFE
        elif merged and not bead_closed:
            cls = WtClassification.REVIEW
        elif bead_closed and not merged:
            cls = WtClassification.UNMERGED
        else:
            # open/in-progress/unknown bead, not merged
            cls = WtClassification.ACTIVE

        safe = cls == WtClassification.SAFE

        results.append(
            WtStatus(
                rig=rig_prefix,
                leaf=leaf,
                branch=branch,
                path=path,
                bead_id=bead_id,
                classification=cls,
                merged=merged,
                dirty=dirty,
                safe=safe,
            )
        )

    return results
