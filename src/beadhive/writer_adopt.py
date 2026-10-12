"""Placement-first coexistence adopt with an idempotent data step 2 (bh-4c7p4, P-M2).

Implements ``docs/design/hive-writer-partitioning-adr.md`` §2 "Adopt" and the §1 rules, binding
conditions 1, 2, 5 and 12. On a hive whose data carries ``bh_writer`` (it has been cut over to
the in-data epoch fence) becoming its writer is:

1. **Placement CAS** at HQ — ``refs/bh/lease/<prefix>`` in git HQ — at the new epoch. Placement
   is what makes an epoch unique to one frame (``bh-cvk70`` E10), so it goes first.
2. **``refs/bh/epoch`` CAS in lockstep** while that ref exists (dual fence, Φ2), so an older bh
   that still reserves the legacy ref before every push is fenced too. A lost ref CAS is a lost
   adopt: it is reported, never retried with the same expectation.
3. **Step 2 in data**, idempotent (:func:`run_step2`): start from the remote head; stop if
   ``bh_writer.epoch >= mine``; re-check that placement still names ``(me, epoch)``; then ONE
   commit names the writer, rewrites ``revision`` with a fresh value, deletes marks below the
   new epoch, moves the singleton live epoch and inserts the ``adopt-<epoch>`` sentinel; push,
   and on a non-fast-forward loop back to the data check.

The epoch is ``max(refs/bh/epoch, placement, bh_writer, max(dolt_history_bh_writer)) + 1``
(:func:`next_epoch`). History is in the max so a dropped-and-recreated ``bh_writer`` cannot
regress it.

The half-done state — placement (and the ref) ahead of ``bh_writer`` — is **adopt incomplete**
(``placement_ahead``). It "fails old": the previous writer may finish publishing until the bump
fences it, and nobody else can adopt at that epoch. It is recovered by re-running the adopt,
which resumes step 2 at the placed epoch without a second bump (:func:`coexistence_adopt`), or
by re-placing at a higher epoch. It is never recovered by rolling placement back or by
resolving a ``bh_writer`` conflict.

Everything here is Typer-free and talks to the outside only through three ports, so the order
and the idempotence can be proven with fakes and against the composed Dolt prototype alike:

* :class:`~beadhive_hq_storage.HiveFence` — the hive's own data, through the HQ storage port
  (``docs/design/hq-storage-port-adr.md``, bh-jhgum). The bump is a typed
  :class:`~beadhive_hq_storage.BumpRequest`, never SQL; the product adapter is
  :class:`beadhive.fence_data.FenceNode`, the root composition of the Dolt adapter, and the
  in-memory :class:`~beadhive_hq_storage.InMemoryHiveFence` carries the protocol tests;
* :class:`~beadhive_hq_storage.PlacementStore` — the HQ placement record, through the same
  port (bh-xxkf7.4): ``Applied`` / ``Conflict`` / ``Unknown``, an ``Unknown`` resolved by
  ``lookup`` under the CAS's own operation id and never re-issued. The product adapters are
  :mod:`beadhive.host_adopt`'s lease and director placements;
* :class:`EpochRef` — the legacy ``refs/bh/epoch`` fence.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from beadhive_hq_storage import (
    ABSENT,
    ADOPT_SENTINEL_PREFIX,
    Applied,
    BumpRequest,
    Confirmed,
    Conflict,
    HiveFence,
    OperationId,
    PlacementStore,
    PlacementView,
    RefView,
    StorageError,
    StorageUnavailable,
    Unknown,
    WriterRow,
    new_operation_id,
)

from . import log

if TYPE_CHECKING:
    from .failover_reclaim import ReclaimPlan

__all__ = [
    "ADOPT_COMMIT_PREFIX",
    "ADOPT_SENTINEL_PREFIX",
    "DEFAULT_STEP2_ATTEMPTS",
    "FENCE_TRIGGER_COUNT",
    "PLACEMENT_CAUSE_FAILOVER",
    "PLACEMENT_CAUSE_PLANNED",
    "STEP2_ATTEMPTS_ENV",
    "AdoptError",
    "AdoptIncomplete",
    "AdoptLost",
    "AdoptReport",
    "BumpReclaim",
    "CoexistenceOutcome",
    "DataUnreachable",
    "EpochRef",
    "EpochRefLost",
    "HiveFence",
    "NotCutOver",
    "PlacementLost",
    "PlacementStore",
    "PlacementUnknown",
    "PlacementView",
    "RefView",
    "Step2Outcome",
    "Step2Result",
    "WriterRow",
    "adopt_report",
    "coexistence_adopt",
    "failover_kind",
    "fresh_revision",
    "next_epoch",
    "place",
    "recovery_command",
    "run_step2",
    "step2_attempts",
    "unavailable_detail",
]

#: Commit subject of every adopt bump; ``fence_audit`` / the invariant checker key on it.
ADOPT_COMMIT_PREFIX = "bh: adopt "
#: ``ADOPT_SENTINEL_PREFIX`` (re-exported from the port): every bump inserts a ``bh_write_mark``
#: row with this id prefix (condition 1).
#: The guard's 42 triggers plus the two monotonic fence triggers (condition 9, ``bh-jbb6r`` E6).
#: A node short of them refuses to act as writer (the fence's
#: :meth:`~beadhive_hq_storage.HiveFence.guard_conformance`). M1 owns the install.
FENCE_TRIGGER_COUNT = 44
#: How many push rounds step 2 makes before it reports "adopt incomplete". Configurable per
#: call and by :data:`STEP2_ATTEMPTS_ENV`; a lost round is cheap and loops back to the data
#: check, so the cap only bounds a pathological push race.
DEFAULT_STEP2_ATTEMPTS = 4
STEP2_ATTEMPTS_ENV = "BH_ADOPT_STEP2_ATTEMPTS"
#: The cause a placement CAS records: the same values the director writes
#: (:data:`beadhive.hq_sql_placement.CAUSE_FAILOVER` / ``CAUSE_PLANNED``).
PLACEMENT_CAUSE_FAILOVER = "failover"
PLACEMENT_CAUSE_PLANNED = "planned"


# =============================================================================================
# Errors
# =============================================================================================


class AdoptError(RuntimeError):
    """Adopt could not be completed. Typer-free; the CLI maps it to exit 1."""


class NotCutOver(AdoptError):
    """The hive's data has no ``bh_writer``: it is on the legacy model (Φ1), so the caller
    takes the legacy fence-first adopt instead. Raised before anything is written."""


class DataUnreachable(AdoptError, StorageUnavailable):
    """The hive's remote could not be fetched or read. Adopt needs the remote head.

    Also the port's :class:`~beadhive_hq_storage.StorageUnavailable`, so a root fence node that
    raises it still honours the :class:`~beadhive_hq_storage.HiveFence` contract."""


class PlacementLost(AdoptError):
    """The placement CAS lost: another principal moved placement since it was read (the
    store's ``Conflict``), or the placement authority refused the move. Nothing was written.
    Never retried with the same expectation (condition 5)."""


class PlacementUnknown(AdoptError):
    """The placement CAS's outcome is unknown (the store's ``Unknown``) and its ``lookup``
    under the same operation id did not confirm it. The placement may or may not have moved.

    Never re-issued and never re-minted (hq-storage-port-adr section 2, rule 3): re-running the
    adopt reads placement back and decides again from that read — it resumes if placement
    names this frame, and otherwise places higher."""

    def __init__(self, message: str, *, operation: OperationId, reason: str):
        super().__init__(message)
        self.operation = operation
        self.reason = reason


class AdoptLost(AdoptError):
    """Somebody else's adopt superseded this one (the data or placement moved past it).
    Do not recover it: the other adopt is the live one."""


class AdoptIncomplete(AdoptError):
    """Placement (and the ref) name this frame at an epoch the data does not yet carry —
    ``placement_ahead``. Fails old, not open: recover by re-running the adopt (it resumes step
    2 at the placed epoch) or by re-placing higher; never by rolling placement back and never
    by resolving a ``bh_writer`` conflict."""

    def __init__(self, message: str, *, report: AdoptReport | None = None):
        super().__init__(message)
        self.report = report


class EpochRefLost(AdoptIncomplete):
    """The ``refs/bh/epoch`` CAS lost after placement was already moved. A lost ref CAS is a
    lost adopt and is never retried with the same expectation; placement is left ahead (adopt
    incomplete), recovered by re-placing higher."""


# =============================================================================================
# Records and ports
# =============================================================================================


# ``WriterRow``, ``RefView`` and ``PlacementView`` are the port's records (hq-storage-port-adr
# section 6, "Move"), re-exported here. The placement authority is the port's
# ``PlacementStore`` (bh-xxkf7.4); ``PlacementView``'s token keeps an empty default there, for
# the views ``doctor`` and ``fence_audit`` build without a CAS token.


class EpochRef(Protocol):
    """The legacy ``refs/bh/epoch`` fence on the hive remote."""

    def read(self) -> RefView | None:
        """The ref, or ``None`` when it does not exist (then it is not maintained)."""

    def cas(self, frame: str, epoch: int, *, expected: RefView) -> RefView:
        """Move the ref to ``(frame, epoch, seq 0)`` iff it is still at ``expected.sha``.
        Raises :class:`EpochRefLost` (or the fence's own rejection) on a loss."""


class BumpReclaim(Protocol):
    """The failover reclaim hook (M3, :mod:`beadhive.failover_reclaim`): one plan per step-2
    round, computed from the head that round just synced, whose typed
    :class:`~beadhive_hq_storage.ReclaimSet` (``plan.reclaim``) rides in the bump."""

    def plan(self, *, dead_frame: str, frame: str, epoch: int) -> ReclaimPlan: ...

    def after_landed(self, plan: ReclaimPlan) -> None: ...

    def with_kind(self, failover: bool | None) -> BumpReclaim:
        """The same hook for an adopt of this kind (``True`` failover, ``False`` planned
        handoff, ``None`` unknown)."""
        ...


# =============================================================================================
# Pure pieces
# =============================================================================================


def next_epoch(
    *,
    ref: int | None = None,
    placement: int | None = None,
    writer: int | None = None,
    history_max: int | None = None,
) -> int:
    """``max(refs/bh/epoch, placement, bh_writer, max(dolt_history_bh_writer)) + 1``.

    Every carrier that ever named an epoch is in the max, so no adopt can mint an epoch some
    carrier already saw: not after a crash that left placement ahead of the data, and not after
    ``bh_writer`` was dropped and recreated lower (its history still holds the old maximum)."""
    return max(v or 0 for v in (ref, placement, writer, history_max)) + 1


def fresh_revision(frame: str, epoch: int) -> str:
    """A new ``bh_writer.revision``: 64 hex, ``sha256(record ‖ uuid4)``.

    Never a content hash of the record alone — that would repeat for the same ``(frame,
    epoch)``, and a write that leaves the CAS cell unchanged can silently merge past a
    competing adopt (``bh-cvk70`` E6, condition 2)."""
    material = f"{frame}\x00{epoch}\x00{uuid.uuid4().hex}".encode()
    return hashlib.sha256(material).hexdigest()


def step2_attempts(attempts: int | None = None) -> int:
    """The step-2 round cap: explicit argument, else :data:`STEP2_ATTEMPTS_ENV`, else
    :data:`DEFAULT_STEP2_ATTEMPTS`. Refused (never clamped) below 1."""
    if attempts is None:
        raw = os.environ.get(STEP2_ATTEMPTS_ENV, "").strip()
        attempts = int(raw) if raw else DEFAULT_STEP2_ATTEMPTS
    if attempts < 1:
        raise ValueError(f"step-2 attempts must be >= 1 (got {attempts})")
    return attempts


def recovery_command(prefix: str) -> str:
    """The one command that recovers "adopt incomplete" for this host."""
    return f"bh host lease adopt {prefix}"


# =============================================================================================
# Step 2
# =============================================================================================


class Step2Outcome(StrEnum):
    LANDED = "landed"
    SUPERSEDED = "superseded in data"
    PLACEMENT_MOVED = "placement moved"
    GUARD_INCOMPLETE = "guard incomplete"
    UNREACHABLE = "unreachable"
    RETRIES_EXHAUSTED = "retries exhausted"


@dataclass(frozen=True)
class Step2Result:
    outcome: Step2Outcome
    epoch: int
    attempts: int
    #: The revision this run committed, when this run's own bump landed.
    revision: str | None = None
    detail: str = ""
    #: The failover reclaim plan of the round whose bump landed (``None`` when no hook ran, or
    #: when the bump had already landed in an earlier run: reclaim happens exactly once).
    reclaim: ReclaimPlan | None = None

    @property
    def landed(self) -> bool:
        return self.outcome is Step2Outcome.LANDED


def run_step2(
    data: HiveFence,
    placement: PlacementStore,
    *,
    prefix: str,
    frame: str,
    epoch: int,
    attempts: int | None = None,
    before_push: Callable[[], None] | None = None,
    reclaim: BumpReclaim | None = None,
) -> Step2Result:
    """Adopt step 2: idempotent, safe to re-run after a crash at any point.

    Each round starts from the remote head and stops when the data already holds ``>= epoch``
    (``landed`` iff it names ``(frame, epoch)`` — the crash-after-push case, no second bump),
    when placement no longer names ``(frame, epoch)``, or when this node's guard install is
    incomplete (:meth:`~beadhive_hq_storage.HiveFence.guard_conformance`). Otherwise it commits
    the typed bump and pushes; a non-fast-forward (``Conflict``) loops back to the data check.
    An ``Unknown`` push, or an ``Unknown`` bump commit (whose push then publishes the bump if
    it exists), is resolved by :meth:`~beadhive_hq_storage.HiveFence.lookup_bump` with the
    bump's own operation id: ``Confirmed`` landed, anything else is unreachable (the re-run
    resumes; it never re-issues the bump). ``before_push`` is a test seam (one-shot).

    The bump's epoch floor is the store's: a bump at or below ``max(history, bh_writer)``
    raises :class:`~beadhive_hq_storage.IntegrityViolation`, which fails closed and is never
    retried (it propagates).

    ``reclaim`` (failover adopts only, M3): each round asks it for a plan against the head the
    round synced — the dead frame is the ``bh_writer`` the bump replaces — and carries the
    plan's :class:`~beadhive_hq_storage.ReclaimSet` in the SAME bump commit. A non-fast-forward
    recomputes; a landed re-run stops at the data check above before asking, so the reclaim is
    applied exactly once."""
    rounds = step2_attempts(attempts)
    for attempt in range(1, rounds + 1):
        try:
            data.sync_to_remote()
            current = data.writer()
        except StorageUnavailable as exc:
            return Step2Result(
                Step2Outcome.UNREACHABLE, epoch, attempt, detail=unavailable_detail(exc)
            )
        if current is None:
            return Step2Result(
                Step2Outcome.SUPERSEDED,
                epoch,
                attempt,
                detail="bh_writer is gone from the remote head (rolled back?)",
            )
        if (current.frame, current.epoch) == (frame, epoch):
            return Step2Result(Step2Outcome.LANDED, epoch, attempt)
        if current.epoch >= epoch:
            return Step2Result(
                Step2Outcome.SUPERSEDED,
                epoch,
                attempt,
                detail=f"bh_writer already names {current.frame}@{current.epoch}",
            )
        try:
            placed = placement.read(prefix)
        except Exception as exc:  # noqa: BLE001 — any HQ read failure means "cannot re-check"
            return Step2Result(Step2Outcome.UNREACHABLE, epoch, attempt, detail=f"HQ: {exc}")
        if placed is None or (placed.frame, placed.epoch) != (frame, epoch):
            seen = "nothing" if placed is None else f"{placed.frame}@{placed.epoch}"
            return Step2Result(
                Step2Outcome.PLACEMENT_MOVED, epoch, attempt, detail=f"placement names {seen}"
            )
        guard = data.guard_conformance()
        if guard.missing or guard.present < guard.expected:
            return Step2Result(
                Step2Outcome.GUARD_INCOMPLETE,
                epoch,
                attempt,
                detail=f"{guard.present} of {guard.expected} bh_* triggers installed on this node",
            )
        revision = fresh_revision(frame, epoch)
        plan = None
        if reclaim is not None:
            plan = reclaim.plan(dead_frame=current.frame, frame=frame, epoch=epoch)
        bump = BumpRequest(
            frame=frame,
            epoch=epoch,
            revision=revision,
            operation=new_operation_id(),
            reclaim=None if plan is None else plan.reclaim,
        )
        # IntegrityViolation (floor) propagates: fail closed. An Unknown commit is never
        # re-issued (bh-xxkf7.6): the push publishes it if it exists, and lookup_bump decides.
        committed = data.commit_bump(bump)
        if before_push is not None:
            hook, before_push = before_push, None
            hook()
        try:
            pushed = data.push()
            unsure = [
                f"{what} outcome unknown ({out.reason})"
                for what, out in (("bump commit", committed), ("push", pushed))
                if isinstance(out, Unknown)
            ]
            if unsure and not isinstance(pushed, Conflict):
                found = data.lookup_bump(frame, epoch, operation=bump.operation)
                if not isinstance(found, Confirmed):
                    return Step2Result(
                        Step2Outcome.UNREACHABLE,
                        epoch,
                        attempt,
                        detail=f"{'; '.join(unsure)}; the bump is not confirmed on the remote head",
                    )
        except StorageError as exc:
            return Step2Result(
                Step2Outcome.UNREACHABLE, epoch, attempt, detail=unavailable_detail(exc)
            )
        if isinstance(pushed, Conflict):
            continue  # non-fast-forward: back to the data check from the new remote head
        assert isinstance(pushed, Applied | Unknown)  # an Unknown was Confirmed above
        if reclaim is not None and plan is not None:
            reclaim.after_landed(plan)
        return Step2Result(Step2Outcome.LANDED, epoch, attempt, revision=revision, reclaim=plan)
    return Step2Result(Step2Outcome.RETRIES_EXHAUSTED, epoch, rounds)


def unavailable_detail(exc: BaseException) -> str:
    """An unreachable-data detail: the exception, plus the cause the port adapter hid (it never
    puts driver text in its own message, but keeps the original as ``__context__``)."""
    text = str(exc)
    cause = exc.__cause__ or exc.__context__
    if cause is not None and str(cause) and str(cause) not in text:
        text = f"{text}: {cause}" if text else str(cause)
    return text


# =============================================================================================
# Adopt-incomplete reporting
# =============================================================================================


@dataclass(frozen=True)
class AdoptReport:
    """``placement_ahead``: HQ placement names a higher epoch than the hive's ``bh_writer``."""

    prefix: str
    placement: PlacementView
    writer: WriterRow
    ref: RefView | None = None

    def describe(self, *, host_id: str | None = None) -> str:
        ref = "" if self.ref is None else f", refs/bh/epoch {self.ref.frame}@{self.ref.epoch}"
        if host_id is not None and self.placement.frame == host_id:
            how = f"re-run `{recovery_command(self.prefix)}` here (resumes step 2 at that epoch)"
        else:
            how = (
                f"the placed frame re-runs `{recovery_command(self.prefix)}` (resumes step 2), "
                "or a director/operator re-places higher"
            )
        return (
            f"hive '{self.prefix}': adopt incomplete (placement_ahead) — placement names "
            f"{self.placement.frame}@{self.placement.epoch}{ref} but bh_writer names "
            f"{self.writer.frame}@{self.writer.epoch}. Dispatch grants no new claims until it "
            f"converges. Recover: {how}. Never roll placement back and never resolve a "
            "bh_writer conflict."
        )


def adopt_report(
    prefix: str,
    placement: PlacementView | None,
    writer: WriterRow | None,
    ref: RefView | None = None,
) -> AdoptReport | None:
    """An :class:`AdoptReport` when placement is ahead of the data, else ``None`` (also when
    the hive is not cut over — a missing ``bh_writer`` is the legacy model, not a half-state)."""
    if placement is None or writer is None or placement.epoch <= writer.epoch:
        return None
    return AdoptReport(prefix=prefix, placement=placement, writer=writer, ref=ref)


# =============================================================================================
# The coexistence adopt
# =============================================================================================


@dataclass(frozen=True)
class CoexistenceOutcome:
    """A converged coexistence adopt."""

    epoch: int
    placement: PlacementView
    ref: RefView | None
    step2: Step2Result
    #: True when this run resumed an adopt already placed at ``epoch`` (no new epoch minted).
    resumed: bool = False


def _resumable(
    frame: str,
    placed: PlacementView | None,
    writer: WriterRow,
    history_max: int,
    ref: RefView | None,
) -> bool:
    """Placement already names ``(frame, E)`` and step 2 can still converge AT ``E``.

    That needs the data at or below ``E`` (equal only if it names us: already landed), the
    history below ``E`` unless the current row is ours at ``E``, and the ref not past ``E``
    nor naming someone else at ``E``. Anything else is re-placed higher."""
    if placed is None or placed.frame != frame:
        return False
    e = placed.epoch
    landed = (writer.frame, writer.epoch) == (frame, e)
    if not landed and not (writer.epoch < e and history_max < e):
        return False
    return ref is None or ref.epoch < e or (ref.epoch == e and ref.frame == frame)


def failover_kind(frame: str, displaced: PlacementView | None) -> bool | None:
    """Is the adopt that displaces ``displaced`` a failover (D5a) or a planned handoff (D5c)?

    ``True`` when the displaced placement names another live frame (expired, evicted or
    forced: the primary is lost), ``False`` when it was released (a tombstone, empty frame) or
    absent, ``None`` when it already named ``frame`` (a resumed adopt: the record it displaced
    is gone, so the kind cannot be read back)."""
    if displaced is None or not displaced.frame:
        return False
    if displaced.frame == frame:
        return None
    return True


def place(
    placement: PlacementStore,
    prefix: str,
    frame: str,
    epoch: int,
    *,
    expected: PlacementView | None,
    cause: str,
) -> PlacementView:
    """The placement CAS (adopt step 1): move placement to ``(frame, epoch)`` iff it is still
    ``expected`` (``None``: never placed), under one fresh operation id.

    ``Applied`` returns the new view. ``Conflict`` raises :class:`PlacementLost`. ``Unknown`` is
    looked up under the SAME operation id: ``Confirmed`` is placed (at the confirmed revision,
    which may already be superseded; step 2 re-checks placement), and ``NotConfirmed`` or a
    failed lookup raises :class:`PlacementUnknown`. Never re-issued, never re-minted."""
    operation = new_operation_id()
    out = placement.compare_and_set(
        prefix,
        frame,
        epoch,
        expected=ABSENT if expected is None else expected,
        cause=cause,
        operation=operation,
    )
    if isinstance(out, Applied):
        return PlacementView(frame=frame, epoch=epoch, token=out.revision)
    if isinstance(out, Conflict):
        now = "absent" if out.current is None else f"at revision {out.current.revision}"
        raise PlacementLost(
            f"{prefix}: placement CAS for {frame}@{epoch} lost — placement moved since it was "
            f"read (now {now}). Nothing was written; re-read and decide again, this is NOT "
            "retried with the same expectation."
        )
    try:
        found = placement.lookup(prefix, operation)
    except StorageError:
        found = None
    if isinstance(found, Confirmed):
        return PlacementView(frame=frame, epoch=epoch, token=found.revision)
    raise PlacementUnknown(
        f"{prefix}: placement CAS for {frame}@{epoch} outcome unknown ({out.reason}) and not "
        f"confirmed by operation {operation}. It is never re-issued: re-run "
        f"`{recovery_command(prefix)}`, which reads placement back and decides again.",
        operation=operation,
        reason=out.reason,
    )


def coexistence_adopt(
    data: HiveFence,
    placement: PlacementStore,
    ref: EpochRef,
    *,
    prefix: str,
    frame: str,
    attempts: int | None = None,
    on_placed: Callable[[PlacementView], None] | None = None,
    before_push: Callable[[], None] | None = None,
    reclaim: BumpReclaim | None = None,
    failover: bool | None = None,
) -> CoexistenceOutcome:
    """Placement first, then the legacy ref in lockstep, then the idempotent data step 2.

    Re-running after a crash at any point converges without a second epoch bump: when
    placement already names ``(frame, E)`` and the data can still reach ``E``, this resumes —
    it CASes the ref to ``E`` only if it is still behind, then runs step 2 at ``E``.

    ``on_placed`` runs right after a won placement CAS (the caller caches the lease there, so
    the host itself sees its own half-state).

    ``reclaim`` is the failover reclaim hook (M3). It runs only when the adopt is a failover:
    ``failover`` when given, else :func:`failover_kind` of the placement this run displaced. A
    planned handoff applies nothing; an unknown kind (resumed adopt) writes nothing.

    Raises :class:`NotCutOver` (legacy hive, nothing written), :class:`PlacementLost`,
    :class:`PlacementUnknown`, :class:`AdoptLost`, :class:`EpochRefLost` or
    :class:`AdoptIncomplete`."""
    logger = log.get_logger(__name__)
    # Decide the path from the remote head WITHOUT moving local main: a legacy hive is left
    # exactly as it was. Only a cut-over hive is reset to the remote head (step 2's rule).
    if data.remote_writer() is None:
        raise NotCutOver(f"{prefix}: no bh_writer on the remote head — legacy adopt applies")
    data.sync_to_remote()
    writer = data.writer()
    if writer is None:  # rolled back between the two reads
        raise NotCutOver(f"{prefix}: no bh_writer on the remote head — legacy adopt applies")
    history_max = data.epoch_floor().history_max
    placed = placement.read(prefix)
    held = ref.read()

    resumed = _resumable(frame, placed, writer, history_max, held)
    kind = failover if failover is not None else failover_kind(frame, placed)
    if reclaim is not None:
        reclaim = reclaim.with_kind(kind)  # a non-failover kind plans "skipped", writes nothing
    if resumed:
        assert placed is not None
        epoch = placed.epoch
        now_placed = placed
    else:
        epoch = next_epoch(
            ref=held.epoch if held else None,
            placement=placed.epoch if placed else None,
            writer=writer.epoch,
            history_max=history_max,
        )
        # A loss raises (nothing written); an unconfirmed Unknown raises, never re-issued.
        now_placed = place(
            placement,
            prefix,
            frame,
            epoch,
            expected=placed,
            cause=PLACEMENT_CAUSE_FAILOVER if kind else PLACEMENT_CAUSE_PLANNED,
        )
        if on_placed is not None:
            on_placed(now_placed)

    def incomplete(cls, why: str, *, now_ref: RefView | None) -> AdoptIncomplete:
        report = AdoptReport(prefix=prefix, placement=now_placed, writer=writer, ref=now_ref)
        logger.warning(
            "writer_adopt_incomplete",
            hive_prefix=prefix,
            frame=frame,
            epoch=epoch,
            reason=why,
        )
        return cls(f"{why}\n  {report.describe(host_id=frame)}", report=report)

    # ---- the legacy ref, in lockstep, while it exists (dual fence, Φ2) --------------------
    now_ref = held
    if held is not None and (held.frame, held.epoch) != (frame, epoch):
        try:
            now_ref = ref.cas(frame, epoch, expected=held)
        except Exception as exc:  # noqa: BLE001 — every ref-CAS loss is the same lost adopt
            raise incomplete(
                EpochRefLost,
                f"{prefix}: refs/bh/epoch CAS lost after placement moved to {frame}@{epoch} — "
                f"this adopt is lost and is not retried with the same expectation ({exc})",
                now_ref=held,
            ) from exc

    # ---- step 2 in data ----------------------------------------------------------------
    result = run_step2(
        data,
        placement,
        prefix=prefix,
        frame=frame,
        epoch=epoch,
        attempts=attempts,
        before_push=before_push,
        reclaim=reclaim,
    )
    if result.landed:
        return CoexistenceOutcome(
            epoch=epoch, placement=now_placed, ref=now_ref, step2=result, resumed=resumed
        )
    if result.outcome in (Step2Outcome.SUPERSEDED, Step2Outcome.PLACEMENT_MOVED):
        raise AdoptLost(
            f"{prefix}: adopt at {frame}@{epoch} was superseded ({result.outcome}: "
            f"{result.detail}) — another adopt is live; do not recover this one"
        )
    raise incomplete(
        AdoptIncomplete,
        f"{prefix}: placed {frame}@{epoch} but step 2 did not land "
        f"({result.outcome}{': ' + result.detail if result.detail else ''})",
        now_ref=now_ref,
    )
