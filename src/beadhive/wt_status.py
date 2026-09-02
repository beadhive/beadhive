"""ws.wt_status — pure worktree status classifier.

Classifies each managed worktree into one mutually exclusive lifecycle state from
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
    """Closed bead + branch merged into parent + no uncommitted changes. Auto-prune eligible."""

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

    RETAINED = "retained"
    """A deliberately preserved terminal branch with a queryable consumer relation.
    Never auto-pruned, even when its commits are otherwise present in the parent."""

    SUPERSEDED = "superseded"
    """A terminal branch explicitly replaced by another bead and proven content-equivalent
    to its parent through the same ``is_landed`` predicate used by ``LANDED_REBASED``.
    Auto-prune eligible."""

    STALE = "stale"
    """A terminal branch whose disposition is unknown or cannot be proven.  This is the
    conservative successor to the ordinary closed-but-unmerged result; never auto-pruned."""

    UNMERGED = "unmerged"
    """Legacy vocabulary retained for machine-consumer compatibility. New ordinary
    closed-but-unproven rows classify ``STALE``; this member remains never safe."""

    ACTIVE = "active"
    """Bead is open / in-progress — work is actively in progress.

    This is a POSITIVE statement, and it used to be a guess.  Until bh-167s0 the fallback
    bucketed "the bead says open" together with "the bead could not be resolved at all", so a
    hive whose store returned nothing rendered every row ACTIVE — see :attr:`UNKNOWN`."""

    UNKNOWN = "unknown"
    """The bead could not be RESOLVED, so this worktree's state is not known (bh-167s0).

    Distinct from ACTIVE on purpose, and the distinction is the whole bead.  ``worktree status``
    is the pre-flight for destructive operations, and the two answers it must never conflate are
    exactly the two it used to:

      * "this worktree holds open work"  -> do not remove
      * "I could not read the bead"      -> I cannot tell you; do not decide on this

    Measured on ``github/agentguides/runtime``: the hive's store returned zero issues (bd's
    schema-fork guard), every lookup came back empty, and 16 worktrees were reported ACTIVE to
    an operator asking whether the clone could be archived.  Reconciling the schema moved exactly
    one row — the rest were a retired bead PREFIX, a second, unrelated cause reaching the same
    silent fallback.  Two causes, one lie, which is why the fix is an explicit classification
    rather than a repair for either cause.

    Never ``safe``, and :attr:`WtStatus.unknown_reason` says WHY it could not be resolved so the
    operator is not left to re-derive it."""

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
    """Hive prefix (e.g. ``workspace``)."""

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
    """True iff auto-prune should reclaim this worktree. Set for ``SAFE``,
    ``LANDED_REBASED``, and equivalence-proven ``SUPERSEDED`` rows."""

    underlying: WtClassification | None = None
    """What this row would classify as if it were not ``DIRTY`` — ``None`` for every other row.

    ``DIRTY`` preempts every rule except ``DETACHED``, which masks the bead state underneath it:
    a dirty-but-SAFE worktree and a dirty-and-open one render identically, and a dirty worktree
    whose bead is UNRESOLVABLE renders as neither (bh-167s0).  Carrying the masked answer costs
    one field and keeps ``DIRTY``'s precedence — the renderer shows it alongside, and the
    UNKNOWN-taint check reads it, so a dirty row cannot hide an unresolvable bead from the
    "these classifications are not trustworthy" warning."""

    unknown_reason: str = ""
    """Why the bead could not be resolved, on an ``UNKNOWN`` row (else "").

    "Unresolvable" is not one condition — it is at least three, and they call for different
    operator responses: the store could not be READ at all (bd absent, schema-forked, server
    down), the store answered but holds NO issues, or the store is fine and this particular bead
    is missing (a retired prefix leaves every worktree branch naming an id that no longer
    exists).  ``worktree status`` silently defaulted for all three; saying which is bh-167s0's
    second acceptance criterion."""

    disposition_reason: str = ""
    """Machine-recorded terminal reason (``pivot``, ``superseded``, or ``obsolete``)."""

    citing_bead: str = ""
    """Queryable bead that retains or supersedes this branch, when one was recorded."""

    def as_dict(self) -> dict:
        """JSON-serializable dict with ``classification`` / ``underlying`` as strings and
        ``safe`` as a bool — suitable for ``--json`` emission."""
        d = asdict(self)
        d["classification"] = str(self.classification)
        d["underlying"] = str(self.underlying) if self.underlying else None
        return d


@dataclass(frozen=True)
class BatchEvidence:
    """Resolved lifecycle evidence for one exact ``wt/batch/<group>`` branch.

    The inventory adapter owns label lookup and parent resolution.  The pure classifier owns
    the policy decision: every named member must be closed before a merged, clean batch branch
    may become :attr:`WtClassification.SAFE`.
    """

    member_statuses: tuple[tuple[str, str], ...]
    """Deterministically ordered ``(bead_id, status)`` pairs from the batch-label snapshot."""

    parent: str
    """The one integration target shared by every member in this batch."""

    @property
    def all_closed(self) -> bool:
        """True only for a non-empty batch with a parent whose complete member set is closed."""
        return (
            bool(self.parent)
            and bool(self.member_statuses)
            and all(status == "closed" for _bead_id, status in self.member_statuses)
        )


@dataclass(frozen=True)
class WtDisposition:
    """Parsed, fail-closed terminal-disposition record stored in ``close_reason``."""

    state: str
    reason: str
    citing_bead: str = ""


_DISPOSITION_PREFIX = "bh:worktree-disposition:v1"
_DISPOSITION_STATES = frozenset({"retained", "superseded", "stale"})
_DISPOSITION_REASONS = frozenset({"pivot", "superseded", "obsolete"})


def format_disposition(state: str, reason: str, citing_bead: str = "") -> str:
    """Return the canonical close_reason codec; reject values that cannot round-trip."""
    if state not in _DISPOSITION_STATES:
        raise ValueError(f"unsupported worktree disposition state: {state}")
    if reason not in _DISPOSITION_REASONS:
        raise ValueError(f"unsupported worktree disposition reason: {reason}")
    if state in {"retained", "superseded"} and not citing_bead:
        raise ValueError(f"{state} disposition requires a citing bead")
    if state == "stale" and citing_bead:
        raise ValueError("stale disposition cannot carry a citing bead")
    if any(char in citing_bead for char in ";=\n\r"):
        raise ValueError("citing bead contains a reserved disposition-codec character")
    return f"{_DISPOSITION_PREFIX};state={state};reason={reason};citing={citing_bead}"


def parse_disposition(close_reason: str) -> WtDisposition | None:
    """Parse only the exact v1 codec; malformed or future records fail closed as ``None``."""
    parts = close_reason.split(";")
    if len(parts) != 4 or parts[0] != _DISPOSITION_PREFIX:
        return None
    expected = ("state", "reason", "citing")
    values: dict[str, str] = {}
    for part, key in zip(parts[1:], expected, strict=True):
        actual, separator, value = part.partition("=")
        if separator != "=" or actual != key:
            return None
        values[key] = value
    state = values["state"]
    reason = values["reason"]
    citing = values["citing"]
    if state not in _DISPOSITION_STATES or reason not in _DISPOSITION_REASONS:
        return None
    if state in {"retained", "superseded"} and not citing:
        return None
    if state == "stale" and citing:
        return None
    return WtDisposition(state=state, reason=reason, citing_bead=citing)


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
    bead_unknown_reasons: dict[str, str] | None = None,
    store_unreadable_reason: str = "",
    bead_disposition_relations: dict[str, frozenset[tuple[str, str]]] | None = None,
    batch_evidence: dict[str, BatchEvidence] | None = None,
) -> list[WtStatus]:
    """Classify every managed worktree row for one hive.

    Parameters
    ----------
    hive_prefix:
        The hive's prefix string (e.g. ``workspace``).
    managed_rows:
        Rows from ``worktree.managed()`` — a flat list of ``(prefix, path, branch)`` tuples
        for this hive (callers should pre-filter to the target hive).
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
        The hive's integration branch name (e.g. ``main``).
    is_landed_fn:
        Optional callable ``(entry, branch, base, close_reason) -> bool`` — the second-stage
        check for closed+non-ancestor rows (the historical UNMERGED set). Combines bead merge-event
        and ``git cherry`` patch-id equivalence.  When ``None`` the second stage is skipped and
        closed+non-ancestor branches classify conservatively as STALE.
    bead_close_reasons:
        Optional mapping ``bead_id -> close_reason`` string (e.g. ``"merged"``,
        ``"molecule landed"``).  Passed to ``is_landed_fn`` so the merge-event check does not
        require a git call.  Ignored when ``is_landed_fn`` is ``None``.
    bead_unknown_reasons:
        Optional mapping ``bead_id -> reason`` for ids the caller could not resolve (bh-167s0).
        The caller knows WHY a lookup came back empty — it made the call — and the classifier
        does not, so the reason is threaded rather than re-derived.  A bead id that appears here,
        or that is simply absent from ``bead_statuses``, classifies ``UNKNOWN`` instead of
        falling through to ``ACTIVE``.
    store_unreadable_reason:
        Optional hive-wide reason, used for any unresolved bead with no per-bead entry — the
        common case, because when a store cannot be read NOTHING resolves and repeating the same
        sentence per bead says nothing extra.
    bead_disposition_relations:
        Normalized queryable terminal edges per bead: ``("retained", consumer)`` or
        ``("superseded", replacement)``. A close_reason assertion without its matching edge is
        STALE.
    batch_evidence:
        Exact ``wt/batch/<group>`` branch to resolved member statuses and their one shared parent.
        Missing evidence is deliberately indistinguishable from an unresolved batch and cannot
        make a row safe.

    Returns
    -------
    list[WtStatus]
        One entry per managed row in the same order as ``managed_rows``.
    """
    results: list[WtStatus] = []
    unknown_reasons = bead_unknown_reasons or {}
    disposition_relations = bead_disposition_relations or {}
    batches = batch_evidence or {}

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
        batch = batches.get(branch) if branch.startswith("wt/batch/") else None
        if batch is not None:
            parent = batch.parent

        # -- merge ancestry ----------------------------------------------
        if is_detached or not branch or branch == "(detached)":
            merged = False
        else:
            merged = is_merged_fn(entry_stub, branch, parent)

        # -- bead status -------------------------------------------------
        bead_status = bead_statuses.get(bead_id or "", "") if bead_id else ""
        bead_closed = bead_status == "closed"
        close_reason = (bead_close_reasons or {}).get(bead_id or "", "")
        disposition = parse_disposition(close_reason) if bead_closed else None
        malformed_disposition = (
            bead_closed
            and close_reason.startswith("bh:worktree-disposition:")
            and disposition is None
        )
        relation = (disposition.state, disposition.citing_bead) if disposition is not None else None
        relation_matches = relation in disposition_relations.get(bead_id or "", frozenset())
        disposition_reason = disposition.reason if disposition is not None else ""
        citing_bead = disposition.citing_bead if disposition is not None else ""
        # A row with a bead id whose STATUS never came back is unresolved.  Before bh-167s0 this
        # was indistinguishable from "open", because both reached the same `else` — so a store
        # that answered nothing at all reported every worktree as live work.
        unresolved = bool(bead_id) and not bead_status
        unknown_reason = ""
        if unresolved:
            unknown_reason = (
                unknown_reasons.get(bead_id or "")
                or store_unreadable_reason
                or f"bead {bead_id} did not resolve, and the caller gave no reason"
            )

        # -- classification (priority order) -----------------------------
        # Priority:
        #   1. DETACHED        — no branch; cannot determine anything else
        #   2. DIRTY           — uncommitted changes override merge/bead status, but the answer
        #                        underneath is kept in `underlying` rather than thrown away
        #   3. ABANDONED       — no bead id AND no complete closed+merged batch proof
        #   3a.MERGED_ORPHAN   — no bead id but branch IS merged+clean and not batch;
        #                        conservative: not auto-pruned (weaker signal than SAFE)
        #   3b.UNKNOWN         — a bead id that did NOT resolve.  Ahead of every bead-derived
        #                        class below, including REVIEW: an unresolved bead cannot be
        #                        called merged-but-not-closed either, and the opposite error is
        #                        worse — the same silent path could be masking UNMERGED, the
        #                        classifier's own real work-loss signal (bh-167s0).
        #   4. SAFE            — closed + merged + clean (ancestry fast-path)
        #   5. REVIEW          — merged + clean but bead is not yet closed
        #   6a.LANDED_REBASED  — closed + clean + content confirmed in parent via
        #                        merge-event or patch-id (rebase/squash-landed molecule)
        #   6b.UNMERGED        — closed + not ancestor + content NOT confirmed → real signal
        #   7. ACTIVE          — open/in-progress bead.  A POSITIVE answer now: everything that
        #                        could not be resolved left at 3b.
        if is_detached:
            cls = WtClassification.DETACHED
        elif bead_id is None:
            # A batch has no single bead id, so its label-defined member set is the equivalent
            # lifecycle authority.  Promote only the complete all-closed + merged proof.  Missing
            # members, an unresolved status, a mixed parent, or an unmerged branch remains the
            # existing conservative ABANDONED result.  Non-batch orphans retain their separate
            # MERGED_ORPHAN treatment and are never widened by this rule.
            is_batch = branch.startswith("wt/batch/")
            if is_batch and batch is not None and batch.all_closed and merged:
                cls = WtClassification.SAFE
            elif merged and not is_batch:
                cls = WtClassification.MERGED_ORPHAN
            else:
                cls = WtClassification.ABANDONED
        elif unresolved:
            cls = WtClassification.UNKNOWN
        elif malformed_disposition:
            cls = WtClassification.STALE
        elif bead_closed and disposition is not None and not relation_matches:
            # A terminal assertion without its promised queryable graph edge is not authority.
            cls = WtClassification.STALE
        elif bead_closed and disposition is not None and disposition.state == "retained":
            cls = WtClassification.RETAINED
        elif bead_closed and disposition is not None and disposition.state == "stale":
            cls = WtClassification.STALE
        elif bead_closed and disposition is not None and disposition.state == "superseded":
            landed = (
                is_landed_fn(entry_stub, branch, parent, close_reason)
                if is_landed_fn is not None
                else False
            )
            cls = WtClassification.SUPERSEDED if landed else WtClassification.STALE
        elif bead_closed and merged:
            cls = WtClassification.SAFE
        elif merged and not bead_closed:
            cls = WtClassification.REVIEW
        elif bead_closed and not merged:
            # Second-stage check: run only for closed+non-ancestor rows (historical UNMERGED
            # set).  Cheap: is_landed_fn tries the merge-event first, then patch-id.
            if is_landed_fn is not None:
                cls = (
                    WtClassification.LANDED_REBASED
                    if is_landed_fn(entry_stub, branch, parent, close_reason)
                    else WtClassification.STALE
                )
            else:
                cls = WtClassification.STALE
        else:
            # open/in-progress bead, not merged.  "unknown" left this branch in bh-167s0.
            cls = WtClassification.ACTIVE

        # DIRTY still preempts everything but DETACHED — an uncommitted change is a hard stop
        # whatever the bead says — but the masked answer is carried rather than discarded, so a
        # dirty-but-SAFE seat is distinguishable from a dirty-and-open one and a dirty row whose
        # bead is unresolvable still taints the hive (bh-167s0's last acceptance criterion).
        underlying = None
        if dirty and not is_detached:
            underlying, cls = cls, WtClassification.DIRTY

        safe = cls in (
            WtClassification.SAFE,
            WtClassification.LANDED_REBASED,
            WtClassification.SUPERSEDED,
        )

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
                underlying=underlying,
                unknown_reason=unknown_reason,
                disposition_reason=disposition_reason,
                citing_bead=citing_bead,
            )
        )

    return results


def untrustworthy(statuses: list[WtStatus]) -> list[WtStatus]:
    """The rows whose bead could not be resolved — including those masked by ``DIRTY``.

    THE ONE PLACE that answers "can these classifications be acted on", so ``status``, ``prune``
    and ``rm`` cannot drift apart on it (they disagreeing is how the classifier's own guarantees
    get lost — see the ``prune``/``status`` shared-classifier note in this module's docstring).
    A non-empty result means the hive's rows are not a basis for a removal decision: not that
    THESE rows are unsafe, but that the hive's answers are unreliable, so nothing measured
    against them is either.
    """
    return [
        s
        for s in statuses
        if s.classification is WtClassification.UNKNOWN or s.underlying is WtClassification.UNKNOWN
    ]
