"""The Protocols of the intake module (external-intake-module-adr.md section 1).

:class:`IntakePort` is the one consumer-facing port. The outbound ports are intake's own narrow
ones: :class:`ReportStore` (the bead store holding report beads), :class:`CheckpointStore` (opaque
resume tokens), :class:`LabelPolicy` (the closed label vocabulary), :class:`IntakeLock` (the one
per-binding lock), :class:`OutboxView` (what own-message recognition reads of the outbox),
:class:`Sanitizer` (the egress secret scan) and :class:`IdSource` (the outbox's opaque ids).
No bd argv, label string or store path crosses :class:`IntakePort`. Standard library plus this
module's domain records.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from ..domain import (
    BeadView,
    Checkpoint,
    ConsiderRequest,
    DiscoveryOutcome,
    DiscoveryRequest,
    DispositionOutcome,
    DispositionRequest,
    DuplicatePair,
    HoldGate,
    HoldSweep,
    HoldSweepRequest,
    ImportOutcome,
    ImportRequest,
    IssueBinding,
    LinkOutcome,
    LinkRequest,
    PresenceOutcome,
    PresenceRequest,
    QueueQuery,
    QueueView,
    ReportDraft,
    SanitizerFinding,
    SourceSnapshot,
    StoreScope,
    Thread,
    ThreadActor,
    ThreadOutcome,
    ThreadRequest,
    ThreadView,
)


@runtime_checkable
class IntakePort(Protocol):
    """What consumers (CLI, MCP, the work layer's merge and release hooks) may ask of intake.

    Further operations are added to this port, not beside it."""

    def file_report(self, draft: ReportDraft) -> str: ...  # bh report, bh escalate

    def import_issue(self, request: ImportRequest) -> ImportOutcome: ...  # bh work import <url>

    def link(self, request: LinkRequest) -> LinkOutcome: ...  # bh work link

    def unlink(self, report: str, *, actor: str) -> LinkOutcome: ...  # bh work link --remove

    def queue(self, query: QueueQuery) -> QueueView: ...  # bh work intake, MCP

    def dispose(self, request: DispositionRequest) -> DispositionOutcome: ...  # accept/reject/...

    def binding_of(self, report: str) -> IssueBinding | None: ...

    def reports_for_work(self, work: str) -> tuple[IssueBinding, ...]: ...  # merge/release hooks

    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome: ...  # scheduled discovery

    # the conversation model (bh-fptob): one thread per bound report, in ``intake.thread``

    def ingest_thread(self, request: ThreadRequest) -> ThreadOutcome: ...  # incremental refresh

    def reconcile_thread(self, request: ThreadRequest) -> ThreadOutcome: ...  # deletions

    def reconcile_presence(self, request: PresenceRequest) -> PresenceOutcome: ...

    def thread_of(self, report: str) -> ThreadView | None: ...  # triage's read

    def mark_considered(self, request: ConsiderRequest) -> ThreadOutcome: ...  # triage watermark

    # the lifetime intake-hold on a bound report (invariant H, external-intake-module-adr.md 7)

    def sweep_holds(self, request: HoldSweepRequest) -> HoldSweep: ...  # bh doctor, hive repair


@runtime_checkable
class ReportStore(Protocol):
    """Intake's own narrow bead-store port, bound to ONE operational store.

    The composition root builds it only over a hive's operational store, never a public one."""

    def scope(self) -> StoreScope: ...

    def get(self, bead: str) -> BeadView | None: ...

    def find_by_external_ref(self, url: str) -> tuple[BeadView, ...]: ...  # every status

    def create(self, draft: ReportDraft) -> str:
        """One create, ``external_ref`` included. A bound report (``draft.binding``) is born with
        its ``intake-hold:`` gate, opened immediately after the create and before any state
        stamp; a born-native report never gets one."""
        ...

    def set_state(
        self, bead: str, dimension: str, value: str, *, reason: str, actor: str
    ) -> None: ...

    def record_snapshot(self, bead: str, snapshot: SourceSnapshot, *, actor: str) -> None: ...

    def update_fields(self, bead: str, *, issue_type: str, priority: str, actor: str) -> None: ...

    def close(self, bead: str, *, reason: str, actor: str) -> None:
        """Close ``bead``. The ONLY way a report closes.

        A bound report carries an open ``intake-hold:`` gate for its whole life, and a plain close
        of a blocked bead is refused. So for a report whose only live blockers are its own holds
        this is the held close: force-close first, then resolve each hold as bookkeeping, so
        there is no instant at which the report is open and unblocked (a crash between the two
        leaves a closed report with an open hold, which is harmless and swept by ``bh doctor``).
        A report with any other live blocker is refused with ``IntakeRefused(LIVE_BLOCKER)``,
        and a bead with no hold closes as it always did."""
        ...

    def assign(self, bead: str, seat: str, *, actor: str) -> None: ...

    def list_open_labeled(self, label: str) -> tuple[BeadView, ...]: ...

    def duplicate_pairs(self, *, threshold: float) -> tuple[DuplicatePair, ...]: ...

    def holds(self) -> tuple[HoldGate, ...]:
        """Every OPEN ``intake-hold:`` gate and the beads it blocks (none: an orphaned gate)."""
        ...

    def bound_reports(self) -> tuple[BeadView, ...]:
        """Every bound report, of every status (those carrying the ``intake.source`` document)."""
        ...

    def open_hold(self, report: str, *, actor: str) -> None:
        """Open one more ``intake-hold:`` gate on ``report`` (the repair for a missing hold)."""
        ...

    def resolve_hold(self, gate: str, *, actor: str) -> None:
        """Resolve one hold gate as bookkeeping (the repair for a hold left on a closed report).

        Never call this on an open report: resolving the hold first re-admits it to ready work."""
        ...

    def tracked_work(self, report: str) -> str | None: ...

    def track(self, report: str, work: str, *, actor: str) -> None: ...

    def untrack(self, report: str, work: str, *, actor: str) -> None: ...

    def trackers_of(self, work: str) -> tuple[str, ...]: ...

    def read_thread(self, bead: str) -> Thread | None: ...  # None: no ``intake.thread`` yet

    def write_thread(self, bead: str, thread: Thread, *, expect_seq: int, actor: str) -> None:
        """Rewrite ``bead``'s whole ``intake.thread`` document with ``thread``, leaving every
        other document on the bead untouched, in ONE store write.

        bd has no compare-and-swap, so the store re-reads the stored ``seq`` first and refuses
        with ``IntakeRefused(THREAD_MOVED)`` (writing nothing) unless it still equals
        ``expect_seq`` (0 for a bead without a thread). ``thread.seq`` must exceed it."""
        ...


@runtime_checkable
class IntakeLock(Protocol):
    """The one per-binding lock every mutating intake operation holds (ADR section 4).

    ``hold`` yields the holder's live epoch, or raises ``IntakeRefused(INTAKE_BUSY)`` when the
    binding is held elsewhere. No hold expires by time: a dead holder is detected by its released
    lock or a moved epoch. The root's implementation is host + process + record (``guard``, an
    ``fcntl.flock`` in ``.bh/``, the store's ``seq`` re-check); tests use an in-process lock."""

    def hold(self, binding_key: str) -> AbstractContextManager[int]: ...


@runtime_checkable
class OutboxView(Protocol):
    """The read-only view of the outbox own-message recognition needs (``bh-fptob`` R7, with
    ``bh-6wj9y``'s ``issued``). The outbox owns the records; the thread only reads them."""

    def receipt_ids(self) -> frozenset[str]: ...  # remote comment ids a WriteReceipt returned

    def pending(self, outbox_id: str) -> bool: ...  # an operation ``sending`` or ``unknown``

    def issued(self, outbox_id: str) -> bool: ...  # an operation that ever had an attempt

    def known_actors(self) -> frozenset[ThreadActor]: ...  # every actor on an outbox receipt


@runtime_checkable
class Sanitizer(Protocol):
    """The egress sanitizer seam (``bh-o2mjz``; external-intake-module-adr.md section 13).

    ``scan`` reports where secrets are in ``text`` (spans and kinds, never the secret); the leak
    check turns each hit into a never-overridable ``secret`` finding. ``scrub`` redacts them,
    for content fed to inference or logs, never to make a held payload sendable. Until
    ``bh-o2mjz`` lands the root binds the shared ``scrub()`` of ``beadhive_plugins.credentials``
    plus its token-shape pattern; with no sanitizer bound every outbox operation is held."""

    def scan(self, text: str) -> Sequence[SanitizerFinding]: ...

    def scrub(self, text: str) -> str: ...


@runtime_checkable
class IdSource(Protocol):
    """Mints the outbox's opaque ids (``ob1_``/``gr1_`` plus 26 base32 characters of random
    bytes, never derived from a bead id). Tests inject a deterministic one."""

    def outbox_id(self) -> str: ...

    def grant_id(self) -> str: ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Opaque, local, non-secret resume tokens keyed per binding (not configuration)."""

    def get(self, binding_key: str) -> Checkpoint | None: ...

    def put(self, binding_key: str, checkpoint: Checkpoint) -> None: ...


@runtime_checkable
class LabelPolicy(Protocol):
    """The closed label vocabulary a new bead's labels are checked against before a create."""

    def violations(self, labels: Sequence[str]) -> tuple[str, ...]: ...


__all__ = [
    "CheckpointStore",
    "IdSource",
    "IntakeLock",
    "IntakePort",
    "LabelPolicy",
    "OutboxView",
    "ReportStore",
    "Sanitizer",
]
