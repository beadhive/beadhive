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

    LANDED_REBASED = "landed-rebased"
    """Closed bead whose branch is **not** a git ancestor of its parent, but whose content
    is effectively present in the parent under different SHAs (rebase/squash-integrated
    molecule).  The AGF lifecycle ``close_reason`` event or ``git cherry`` patch-id
    equivalence confirms the work has landed.  Auto-prune eligible (``safe=True``): the
    content is confirmed in the parent even though the original per-bead tip is not an
    ancestor."""

    UNMERGED = "unmerged"
    """Bead is closed but the branch is NOT a git ancestor of its parent branch AND content
    equivalence cannot be confirmed (no merge-event, no patch-id match).  A genuine
    work-loss signal — not safe to remove."""

    ACTIVE = "active"
    """Bead is open / in-progress — work is actively in progress."""

    DETACHED = "detached"
    """No branch is checked out in this worktree (detached HEAD state)."""

    MERGED_ORPHAN = "merged-orphan"
    """Branch is a git ancestor of its parent branch and the worktree is clean, but the bead id
    is unresolvable (legacy / non-conforming branch name).  Not auto-pruned by default: the
    merged+clean signal is weaker than closed+merged+clean=SAFE because there is no closed-bead
    confirmation.  Surface in ``status`` for operator review; batch worktrees are never
    MERGED_ORPHAN (they keep their own no-bead treatment)."""

    ABANDONED = "abandoned"
    """Worktree has no corresponding bead id AND is not a merged+clean legacy worktree.
    Covers session worktrees, batch worktrees, and any unresolvable branch that is not
    yet merged into its parent."""


@dataclass(frozen=True)
class WtStatus:
    """Classification record for one managed worktree."""

    hive: str
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
    """True iff auto-prune should reclaim this worktree.  Set for ``SAFE``
    (closed+merged+clean via ancestry) and ``LANDED_REBASED`` (closed+clean+content
    confirmed in parent via merge-event or patch-id equivalence)."""

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
    hive_prefix: str,
    managed_rows: list[tuple[str, str, str]],
    meta_branches: list[dict],
    bead_statuses: dict[str, str],
    dirty_by_path: dict[str, bool],
    is_merged_fn,
    parent_fn,
    integration: str,
    is_landed_fn=None,
    bead_close_reasons: dict[str, str] | None = None,
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
        Callable ``(entry, path, integration, branch) -> (bead_id|None, parent_branch)`` —
        ``worktree.bead_and_parent``.  The ``branch`` argument is the real git branch ref
        from the managed row (e.g. wt/bead/) so id-resolution
        can strip the ``wt/bead/`` prefix directly instead of reconstructing from the
        sanitized directory leaf.
    integration:
        The rig's integration branch name (e.g. ``main``).
    is_landed_fn:
        Optional callable ``(entry, branch, base, close_reason) -> bool`` — the second-stage
        check for closed+non-ancestor rows (today's UNMERGED set).  Combines bead merge-event
        and ``git cherry`` patch-id equivalence.  When ``None`` the second stage is skipped and
        closed+non-ancestor branches stay UNMERGED.
    bead_close_reasons:
        Optional mapping ``bead_id -> close_reason`` string (e.g. ``"merged"``,
        ``"molecule landed"``).  Passed to ``is_landed_fn`` so the merge-event check does not
        require a git call.  Ignored when ``is_landed_fn`` is ``None``.

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
        # Thread the real branch ref through parent_fn so bead_and_parent can
        # strip the wt/bead/ prefix from the actual ref instead of reconstructing
        # the branch from the sanitized directory leaf (Fix 1).
        entry_stub = {"prefix": prefix}
        bead_id, parent = parent_fn(entry_stub, path, integration, branch)

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
        #   1. DETACHED        — no branch; cannot determine anything else
        #   2. DIRTY           — uncommitted changes override merge/bead status
        #   3. ABANDONED       — no bead id AND (not merged OR is a batch worktree)
        #   3a.MERGED_ORPHAN   — no bead id but branch IS merged+clean and not batch;
        #                        conservative: not auto-pruned (weaker signal than SAFE)
        #   4. SAFE            — closed + merged + clean (ancestry fast-path)
        #   5. REVIEW          — merged + clean but bead is not yet closed
        #   6a.LANDED_REBASED  — closed + clean + content confirmed in parent via
        #                        merge-event or patch-id (rebase/squash-landed molecule)
        #   6b.UNMERGED        — closed + not ancestor + content NOT confirmed → real signal
        #   7. ACTIVE          — open/in-progress bead (default)
        if is_detached:
            cls = WtClassification.DETACHED
        elif dirty:
            cls = WtClassification.DIRTY
        elif bead_id is None:
            # No resolvable bead: use merge ancestry to distinguish reclaimable
            # orphans (merged+clean, non-batch) from genuinely abandoned worktrees.
            # Batch branches (wt/batch/<epic>) keep their own no-bead treatment
            # and are always ABANDONED regardless of merge state (Fix 2).
            is_batch = branch.startswith("wt/batch/")
            if merged and not is_batch:
                cls = WtClassification.MERGED_ORPHAN
            else:
                cls = WtClassification.ABANDONED
        elif bead_closed and merged:
            cls = WtClassification.SAFE
        elif merged and not bead_closed:
            cls = WtClassification.REVIEW
        elif bead_closed and not merged:
            # Second-stage check: run only for closed+non-ancestor rows (current UNMERGED
            # set).  Cheap: is_landed_fn tries the merge-event first, then patch-id.
            if is_landed_fn is not None:
                close_reason = (bead_close_reasons or {}).get(bead_id or "", "")
                cls = (
                    WtClassification.LANDED_REBASED
                    if is_landed_fn(entry_stub, branch, parent, close_reason)
                    else WtClassification.UNMERGED
                )
            else:
                cls = WtClassification.UNMERGED
        else:
            # open/in-progress/unknown bead, not merged
            cls = WtClassification.ACTIVE

        safe = cls in (WtClassification.SAFE, WtClassification.LANDED_REBASED)

        results.append(
            WtStatus(
                hive=hive_prefix,
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
