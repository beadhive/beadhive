"""The Protocols of the intake module (external-intake-module-adr.md section 1).

:class:`IntakePort` is the one consumer-facing port. The outbound ports are intake's own narrow
ones: :class:`ReportStore` (the bead store holding report beads), :class:`CheckpointStore` (opaque
resume tokens), :class:`LabelPolicy` (the closed label vocabulary), :class:`IntakeLock` (the one
per-binding lock), :class:`OutboxView` (what own-message recognition reads of the outbox),
:class:`Sanitizer` (the egress secret scan), :class:`IdSource` (the outbox's opaque ids),
:class:`ReleaseVerifier` (what a tag contains) and :class:`RepoFacts` (what the proposal checks
read of the repository).
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
    InvestigateOutcome,
    InvestigateRequest,
    IssueBinding,
    LabelBatch,
    LabelBatchApprove,
    LabelBatchOutcome,
    LabelDefineOutcome,
    LabelDefineRequest,
    LabelSyncOutcome,
    LabelSyncRequest,
    LinkOutcome,
    LinkRequest,
    MergeFact,
    MilestoneOutcome,
    NeedsInfoOutcome,
    NeedsInfoRequest,
    NoticeFact,
    OpKind,
    OutboxApprove,
    OutboxDocument,
    OutboxListing,
    OutboxQuery,
    OutboxResult,
    PresenceOutcome,
    PresenceRequest,
    Proposal,
    QueueQuery,
    QueueView,
    ReleaseFact,
    RenderedRecord,
    ReplyDraft,
    ReportDraft,
    ReportRelations,
    ReviewMetrics,
    ReviewRequest,
    ReviewRun,
    SanitizerFinding,
    SourceSnapshot,
    StartFact,
    StoreScope,
    Thread,
    ThreadActor,
    ThreadOutcome,
    ThreadRequest,
    ThreadView,
    Verdict,
    WakeOutcome,
    WakeRequest,
    WakeSweep,
    WorkDraft,
    WorkProgress,
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

    def work_progress(self, report: str) -> WorkProgress: ...  # derived from the tracked work

    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome: ...  # scheduled discovery

    # the conversation model (bh-fptob): one thread per bound report, in ``intake.thread``

    def ingest_thread(self, request: ThreadRequest) -> ThreadOutcome: ...  # incremental refresh

    def reconcile_thread(self, request: ThreadRequest) -> ThreadOutcome: ...  # deletions

    def reconcile_presence(self, request: PresenceRequest) -> PresenceOutcome: ...

    def thread_of(self, report: str) -> ThreadView | None: ...  # triage's read

    def mark_considered(self, request: ConsiderRequest) -> ThreadOutcome: ...  # triage watermark

    # the lifetime intake-hold on a bound report (invariant H, external-intake-module-adr.md 7)

    def sweep_holds(self, request: HoldSweepRequest) -> HoldSweep: ...  # bh doctor, hive repair

    # the supervised outbox (bh-6wj9y R6, R11; D2): comment.create and labels.change, each its own
    # gate. Nothing here runs from a timer: every write needs an operator approval minting a
    # single-use grant.

    def draft_reply(self, request: ReplyDraft) -> OutboxResult: ...  # O1 for a reply

    def outbox(self, query: OutboxQuery) -> OutboxListing: ...  # list; read-only

    def render(self, outbox_id: str) -> RenderedRecord: ...  # the dry run; read-only

    def approve(self, request: OutboxApprove) -> OutboxResult: ...  # O5 (and O4 publishes)

    def send(
        self, outbox_id: str, *, actor: str, kind: OpKind = OpKind.COMMENT
    ) -> OutboxResult: ...  # O9 to O13

    def reconcile(
        self, outbox_id: str | None = None, *, actor: str
    ) -> tuple[OutboxResult, ...]: ...  # O13 to O16, O19

    def discard(
        self,
        outbox_id: str,
        *,
        reason: str,
        actor: str,
        accept_unknown: bool = False,
        kind: OpKind | None = None,
    ) -> OutboxResult: ...  # O8, O18

    def retry(
        self, outbox_id: str, *, actor: str, kind: OpKind = OpKind.COMMENT
    ) -> OutboxResult: ...  # O17

    # the milestone triggers (bh-6wj9y R9; I10). They DRAFT and never send: no grant, no writer, no
    # tracker request. Free under ``NullIntake``; the work layer wraps both so a failure here is
    # reported and never fails a merge or a release.

    def on_work_merged(self, fact: MergeFact) -> MilestoneOutcome: ...  # work.merge, work.finish

    def on_release(self, fact: ReleaseFact) -> MilestoneOutcome: ...  # bh work delivery release

    # waiting for the reporter (bh-61xkc): a bound report asks, waits on ``intake:needs-info`` and
    # wakes back to ``intake:untriaged`` on a reply. The question is a drafted reply (supervised:
    # an operator approves and sends it); waking reads the stored thread only.

    def needs_info(self, request: NeedsInfoRequest) -> NeedsInfoOutcome: ...  # bh work needs-info

    def wake(self, request: WakeRequest) -> WakeOutcome: ...  # bh work wake <report>

    def wake_all(self, *, actor: str) -> WakeSweep: ...  # bh work wake --all (opt-in)

    # the progress milestones (bh-dev42). Drafts only, like the merge and release triggers:
    # ``started`` from the claim hook (the work layer wraps it so it never fails a claim),
    # ``blocked`` / ``deprioritised`` from ``bh work delivery blocked|deprioritised``.

    def on_work_started(self, fact: StartFact) -> MilestoneOutcome: ...  # work.claim (first claim)

    def delivery_notice(self, fact: NoticeFact) -> MilestoneOutcome: ...  # bh work delivery <event>

    # reproduction work (bh-u3a53): a bead that INVESTIGATES a report, a relation distinct from
    # ``tracks``. It writes no ``delivery`` value, drafts nothing, and no milestone trigger, the
    # claim hook or the link catch-up ever reads it; ``relations`` lists both kinds.

    def investigate(self, request: InvestigateRequest) -> InvestigateOutcome: ...

    def uninvestigate(self, report: str, work: str, *, actor: str) -> InvestigateOutcome: ...

    def relations(self, report: str) -> ReportRelations: ...  # tracks + investigated by

    # review-gated triage (bh-hrx2r): cluster-aware review packets over the pending reports, each
    # with its full thread (ingested first; refused, naming both switches, while comment ingestion
    # is off), its proposal and review history and the global index; and the triage-quality
    # metrics, computed from the reviews stored on the proposals.

    def review_packets(self, request: ReviewRequest) -> ReviewRun: ...  # triage review

    def review_metrics(self) -> ReviewMetrics: ...  # read-only

    # the label projection (bh-uim9i): bead state onto the issue's OWNED labels. ``sync`` reads
    # live labels fresh (one listing per run) and DRAFTS at most one ``labels`` record per issue;
    # ``define`` converges the repository's label definitions, approved in the command by digest;
    # the batch renders every drafted labels operation and approving its digest mints one
    # single-use grant per item. Nothing here sends: ``send`` does, per record, as for comments.

    def sync_labels(self, request: LabelSyncRequest) -> LabelSyncOutcome: ...  # labels sync

    def define_labels(self, request: LabelDefineRequest) -> LabelDefineOutcome: ...  # define

    def review_label_batch(self) -> LabelBatch: ...  # labels review; read-only

    def approve_label_batch(self, request: LabelBatchApprove) -> LabelBatchOutcome: ...


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

    def is_held(self, report: str) -> bool:
        """True when ``report`` has at least one OPEN ``intake-hold:`` gate blocking it.

        The targeted form of :meth:`holds` for ONE report: a constant number of store reads however
        many reports and gates exist, where :meth:`holds` reads every gate. Same answer as
        ``any(report in hold.blocks for hold in holds())``."""
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

    def create_work(self, draft: WorkDraft) -> str:
        """One create of a plain work bead (``accept --new-work``): the hive triplet labels, the
        type, priority and description, and nothing of a report: no ``external_ref``, no
        ``origin``, no ``intake`` state and no hold, so it is ordinary dispatchable work."""
        ...

    def tracked_work(self, report: str) -> str | None: ...

    def track(self, report: str, work: str, *, actor: str) -> None: ...

    def untrack(self, report: str, work: str, *, actor: str) -> None: ...

    def trackers_of(self, work: str) -> tuple[str, ...]: ...

    def investigate(self, report: str, work: str, *, actor: str) -> None:
        """Record that ``work`` investigates ``report`` (bh-u3a53): one native edge, distinct from
        ``tracks`` (which this never reads, writes or removes), non-blocking in both directions,
        surviving either bead's closure. Idempotent."""
        ...

    def uninvestigate(self, report: str, work: str, *, actor: str) -> None:
        """Remove that one investigation edge; the ``tracks`` edge and every other investigation
        of ``report`` stay. A no-op when there is none."""
        ...

    def investigations_of(self, report: str) -> tuple[str, ...]:
        """Every bead that investigates ``report`` (sorted), of every status."""
        ...

    def read_thread(self, bead: str) -> Thread | None: ...  # None: no ``intake.thread`` yet

    def write_thread(self, bead: str, thread: Thread, *, expect_seq: int, actor: str) -> None:
        """Rewrite ``bead``'s whole ``intake.thread`` document with ``thread``, leaving every
        other document on the bead untouched, in ONE store write.

        bd has no compare-and-swap, so the store re-reads the stored ``seq`` first and refuses
        with ``IntakeRefused(THREAD_MOVED)`` (writing nothing) unless it still equals
        ``expect_seq`` (0 for a bead without a thread). ``thread.seq`` must exceed it."""
        ...

    def read_outbox(self, bead: str) -> OutboxDocument | None: ...  # None: no ``intake.outbox``

    def write_outbox(
        self, bead: str, document: OutboxDocument, *, expect_seq: int, actor: str
    ) -> None:
        """Rewrite ``bead``'s whole ``intake.outbox`` document, leaving every other document on
        the bead untouched, in ONE store write.

        As :meth:`write_thread`: the store re-reads the stored ``seq`` and refuses with
        ``IntakeRefused(OUTBOX_MOVED)`` (writing nothing) unless it still equals ``expect_seq``
        (0 for a bead without an outbox); ``document.seq`` must exceed it. Only a bound report
        carries an outbox."""
        ...

    def reports_with_outbox(self) -> tuple[BeadView, ...]:
        """Every report carrying an ``intake.outbox`` document, of every status."""
        ...

    # review-gated triage (bh-hrx2r): the ``intake.proposal`` document, on ANY report (native or
    # bound), private operational data that is never published

    def read_proposal(self, bead: str) -> Proposal | None: ...  # None: no ``intake.proposal``

    def write_proposal(self, bead: str, proposal: Proposal, *, expect_seq: int, actor: str) -> None:
        """Rewrite ``bead``'s whole ``intake.proposal`` document, leaving every other document on
        the bead untouched, in ONE store write.

        As :meth:`write_outbox`: the store re-reads the stored ``seq`` and refuses with
        ``IntakeRefused(PROPOSAL_MOVED)`` (writing nothing) unless it still equals ``expect_seq``
        (0 for a bead without a proposal); ``proposal.seq`` must exceed it. A stored proposal that
        is already ``applied`` is immutable: any write over it is refused with
        ``IntakeRefused(PROPOSAL_IMMUTABLE)``. Unlike the outbox, a born-native report may carry
        one."""
        ...

    def reports_with_proposal(self) -> tuple[BeadView, ...]:
        """Every report carrying an ``intake.proposal`` document, of every status."""
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
class ReleaseVerifier(Protocol):
    """The release seam (``bh-6wj9y`` R9): what a tag is and whether it contains a merge.

    Read-only against git. ``resolve`` is the commit ``tag`` names (``None``: no such tag, so
    nothing is verified or recorded); ``contains`` answers whether ``release_commit`` has
    ``merge_commit`` in its history. Only ``Verdict.YES`` is proof; ``NO`` and ``UNKNOWN`` are
    reported as not verified and never assumed. The root implements it with ``git``; tests inject
    a fake."""

    def resolve(self, tag: str) -> str | None: ...

    def contains(self, release_commit: str, merge_commit: str) -> Verdict: ...


@runtime_checkable
class RepoFacts(Protocol):
    """The repository facts the pre-apply proposal checks read (bh-hrx2r), read-only.

    ``is_ancestor`` answers whether ``commit`` is in ``branch``'s history: only ``Verdict.YES`` is
    proof, and ``NO`` and ``UNKNOWN`` both fail a fix claim. ``first_tag_containing`` is the first
    tag, in version order, whose history contains ``commit`` (``None``: no tag yet, or no
    answer), so a fix version is COMPUTED, never claimed. ``changed_paths`` lists the paths
    ``until`` changed since it diverged from ``since`` (``None``: no answer), which tells a
    proposal analysed at ``since`` that a path its evidence cites has moved. The root implements it
    with ``git`` (copying :class:`ReleaseVerifier`'s pattern); tests inject a fake. The domain's
    ``RepoQueries`` is the same three questions, structurally."""

    def is_ancestor(self, commit: str, branch: str) -> Verdict: ...

    def first_tag_containing(self, commit: str) -> str | None: ...

    def changed_paths(self, since: str, until: str) -> tuple[str, ...] | None: ...


@runtime_checkable
class AuditLog(Protocol):
    """A local, append-only audit trail of an operator-approved repository-level write
    (``bh-uim9i``): ``labels define --approve`` notes what it applied, under which grants and by
    whom. The root keeps it locally; it never reaches the tracker. Tests record the notes."""

    def note(self, text: str, *, actor: str) -> None: ...


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
    "AuditLog",
    "CheckpointStore",
    "IdSource",
    "IntakeLock",
    "IntakePort",
    "LabelPolicy",
    "OutboxView",
    "ReleaseVerifier",
    "RepoFacts",
    "ReportStore",
    "Sanitizer",
]
