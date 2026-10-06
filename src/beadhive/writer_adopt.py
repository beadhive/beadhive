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

* :class:`FenceData` — the hive's own data (the product adapter is M1's, ``bh-uz46l``; until it
  lands no adapter is wired and every hive takes the legacy path — dormant until a hive's data
  switches it on);
* :class:`PlacementAuthority` — the HQ placement record;
* :class:`EpochRef` — the legacy ``refs/bh/epoch`` fence.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from . import log

__all__ = [
    "ADOPT_COMMIT_PREFIX",
    "ADOPT_SENTINEL_PREFIX",
    "DEFAULT_STEP2_ATTEMPTS",
    "FENCE_TRIGGER_COUNT",
    "STEP2_ATTEMPTS_ENV",
    "AdoptError",
    "AdoptIncomplete",
    "AdoptLost",
    "AdoptReport",
    "CoexistenceOutcome",
    "DataUnreachable",
    "EpochRef",
    "EpochRefLost",
    "FenceData",
    "NotCutOver",
    "PlacementAuthority",
    "PlacementLost",
    "PlacementView",
    "RefView",
    "Step2Outcome",
    "Step2Result",
    "WriterRow",
    "adopt_report",
    "bump_statements",
    "coexistence_adopt",
    "fresh_revision",
    "next_epoch",
    "recovery_command",
    "run_step2",
    "step2_attempts",
]

#: Commit subject of every adopt bump; ``fence_audit`` / the invariant checker key on it.
ADOPT_COMMIT_PREFIX = "bh: adopt "
#: Every bump inserts a ``bh_write_mark`` row with this id prefix (condition 1).
ADOPT_SENTINEL_PREFIX = "adopt-"
#: The guard's 42 triggers plus the two monotonic fence triggers (condition 9, ``bh-jbb6r`` E6).
#: A node short of them refuses to act as writer. M1 owns the install; this is the bar.
FENCE_TRIGGER_COUNT = 44
#: How many push rounds step 2 makes before it reports "adopt incomplete". Configurable per
#: call and by :data:`STEP2_ATTEMPTS_ENV`; a lost round is cheap and loops back to the data
#: check, so the cap only bounds a pathological push race.
DEFAULT_STEP2_ATTEMPTS = 4
STEP2_ATTEMPTS_ENV = "BH_ADOPT_STEP2_ATTEMPTS"


# =============================================================================================
# Errors
# =============================================================================================


class AdoptError(RuntimeError):
    """Adopt could not be completed. Typer-free; the CLI maps it to exit 1."""


class NotCutOver(AdoptError):
    """The hive's data has no ``bh_writer``: it is on the legacy model (Φ1), so the caller
    takes the legacy fence-first adopt instead. Raised before anything is written."""


class DataUnreachable(AdoptError):
    """The hive's remote could not be fetched or read. Adopt needs the remote head."""


class PlacementLost(AdoptError):
    """The placement CAS lost: another principal moved placement since it was read. Nothing
    was written. Never retried with the same expectation (condition 5)."""


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


@dataclass(frozen=True)
class WriterRow:
    """The ``bh_writer`` singleton (``id = 1``)."""

    frame: str
    epoch: int
    revision: str = ""


@dataclass(frozen=True)
class PlacementView:
    """Placement as read from HQ, with the CAS token the next write must present."""

    frame: str
    epoch: int
    token: str = ""


@dataclass(frozen=True)
class RefView:
    """``refs/bh/epoch`` as read from the hive remote, with its sha (the CAS expectation)."""

    frame: str
    epoch: int
    sha: str


class FenceData(Protocol):
    """The hive's versioned data, as one node sees it (the M1 seam)."""

    def sync_to_remote(self) -> None:
        """Make local ``main`` exactly the remote head (fetch, then hard reset). Raises
        :class:`DataUnreachable` when the remote cannot be reached."""

    def writer(self) -> WriterRow | None:
        """Local ``bh_writer`` (``None`` when the table does not exist: not cut over)."""

    def remote_writer(self) -> WriterRow | None:
        """``bh_writer`` on the remote head WITHOUT moving local ``main`` (doctor's read)."""

    def history_max_epoch(self) -> int:
        """``max(dolt_history_bh_writer.epoch)`` on local ``main`` (0 when none)."""

    def trigger_count(self) -> int:
        """How many ``bh_*`` triggers this node carries."""

    def commit_bump(self, statements: Sequence[str], message: str) -> None:
        """Run ``statements`` and commit them as ONE commit on local ``main``."""

    def push(self) -> bool:
        """Push ``main``. ``False`` on a non-fast-forward rejection; raises
        :class:`DataUnreachable` on any other failure."""


class PlacementAuthority(Protocol):
    """The single HQ CAS point for placement (ADR §1)."""

    def read(self) -> PlacementView | None:
        """Current placement, or ``None`` when the hive was never placed."""

    def cas(self, frame: str, epoch: int, *, expected: PlacementView | None) -> PlacementView:
        """Move placement to ``(frame, epoch)`` iff it is still ``expected``, rewriting the CAS
        token. Raises :class:`PlacementLost` (or the authority's own rejection) on a loss."""


class EpochRef(Protocol):
    """The legacy ``refs/bh/epoch`` fence on the hive remote."""

    def read(self) -> RefView | None:
        """The ref, or ``None`` when it does not exist (then it is not maintained)."""

    def cas(self, frame: str, epoch: int, *, expected: RefView) -> RefView:
        """Move the ref to ``(frame, epoch, seq 0)`` iff it is still at ``expected.sha``.
        Raises :class:`EpochRefLost` (or the fence's own rejection) on a loss."""


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


def _sql_text(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def bump_statements(frame: str, epoch: int, revision: str) -> list[str]:
    """Step 2's single commit (conditions 1–2): name the writer with a fresh revision, retire
    every older epoch's marks, move the singleton live epoch (the FK forbids moving it while
    marks point at it), and insert the ``adopt-<epoch>`` sentinel so EVERY bump changes
    ``bh_write_mark``."""
    epoch = int(epoch)
    return [
        f"UPDATE bh_writer SET frame = {_sql_text(frame)}, epoch = {epoch}, "
        f"revision = {_sql_text(revision)} WHERE id = 1",
        f"DELETE FROM bh_write_mark WHERE epoch < {epoch}",
        f"UPDATE bh_epoch_live SET epoch = {epoch} WHERE id = 1",
        f"INSERT INTO bh_write_mark (id, epoch, tbl) VALUES "
        f"({_sql_text(ADOPT_SENTINEL_PREFIX + str(epoch))}, {epoch}, 'bh_writer')",
    ]


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

    @property
    def landed(self) -> bool:
        return self.outcome is Step2Outcome.LANDED


def run_step2(
    data: FenceData,
    placement: PlacementAuthority,
    *,
    frame: str,
    epoch: int,
    attempts: int | None = None,
    required_triggers: int = FENCE_TRIGGER_COUNT,
    before_push: Callable[[], None] | None = None,
) -> Step2Result:
    """Adopt step 2: idempotent, safe to re-run after a crash at any point.

    Each round starts from the remote head and stops when the data already holds ``>= epoch``
    (``landed`` iff it names ``(frame, epoch)`` — the crash-after-push case, no second bump),
    when placement no longer names ``(frame, epoch)``, or when this node's guard is short of
    ``required_triggers``. Otherwise it commits the bump and pushes; a non-fast-forward loops
    back to the data check. ``before_push`` is a test seam (one-shot)."""
    rounds = step2_attempts(attempts)
    for attempt in range(1, rounds + 1):
        try:
            data.sync_to_remote()
            current = data.writer()
        except DataUnreachable as exc:
            return Step2Result(Step2Outcome.UNREACHABLE, epoch, attempt, detail=str(exc))
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
            placed = placement.read()
        except Exception as exc:  # noqa: BLE001 — any HQ read failure means "cannot re-check"
            return Step2Result(Step2Outcome.UNREACHABLE, epoch, attempt, detail=f"HQ: {exc}")
        if placed is None or (placed.frame, placed.epoch) != (frame, epoch):
            seen = "nothing" if placed is None else f"{placed.frame}@{placed.epoch}"
            return Step2Result(
                Step2Outcome.PLACEMENT_MOVED, epoch, attempt, detail=f"placement names {seen}"
            )
        have = data.trigger_count()
        if have < required_triggers:
            return Step2Result(
                Step2Outcome.GUARD_INCOMPLETE,
                epoch,
                attempt,
                detail=f"{have} of {required_triggers} bh_* triggers installed on this node",
            )
        revision = fresh_revision(frame, epoch)
        data.commit_bump(
            bump_statements(frame, epoch, revision), f"{ADOPT_COMMIT_PREFIX}{frame}@{epoch}"
        )
        if before_push is not None:
            hook, before_push = before_push, None
            hook()
        try:
            pushed = data.push()
        except DataUnreachable as exc:
            return Step2Result(Step2Outcome.UNREACHABLE, epoch, attempt, detail=str(exc))
        if pushed:
            return Step2Result(Step2Outcome.LANDED, epoch, attempt, revision=revision)
    return Step2Result(Step2Outcome.RETRIES_EXHAUSTED, epoch, rounds)


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


def coexistence_adopt(
    data: FenceData,
    placement: PlacementAuthority,
    ref: EpochRef,
    *,
    prefix: str,
    frame: str,
    attempts: int | None = None,
    required_triggers: int = FENCE_TRIGGER_COUNT,
    on_placed: Callable[[PlacementView], None] | None = None,
    before_push: Callable[[], None] | None = None,
) -> CoexistenceOutcome:
    """Placement first, then the legacy ref in lockstep, then the idempotent data step 2.

    Re-running after a crash at any point converges without a second epoch bump: when
    placement already names ``(frame, E)`` and the data can still reach ``E``, this resumes —
    it CASes the ref to ``E`` only if it is still behind, then runs step 2 at ``E``.

    ``on_placed`` runs right after a won placement CAS (the caller caches the lease there, so
    the host itself sees its own half-state). Raises :class:`NotCutOver` (legacy hive, nothing
    written), :class:`PlacementLost`, :class:`AdoptLost`, :class:`EpochRefLost` or
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
    history_max = data.history_max_epoch()
    placed = placement.read()
    held = ref.read()

    resumed = _resumable(frame, placed, writer, history_max, held)
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
        now_placed = placement.cas(frame, epoch, expected=placed)  # loss raises: nothing written
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
        frame=frame,
        epoch=epoch,
        attempts=attempts,
        required_triggers=required_triggers,
        before_push=before_push,
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
