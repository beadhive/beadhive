"""``IntakeService``, the one implementation of :class:`IntakePort`, and ``NullIntake``.

The service implements what only needs the :class:`ReportStore`: filing a report, the queue, the
dispositions, bindings and the work lookup (``bh report``, ``bh escalate`` and the triage verbs
run through it). The conversation operations delegate to an optional
:class:`~.threads.ThreadService`; without one (comment ingestion off, the default) they refuse
with ``INTAKE_DISABLED`` and ``thread_of`` reads nothing. The outbox operations delegate to an
optional :class:`~.sender.OutboxService`; without one (the outbox off, the default) every one of
them refuses with ``OUTBOX_DISABLED``, so nothing can be drafted, approved or sent. Import delegates
to an optional :class:`~.importer.IssueImporter`; without one (no tracker bound) it refuses with
``INTAKE_DISABLED``. Link, unlink, the derived work progress and ``accept --work`` /
``--new-work`` run over the store alone through :class:`~.linker.LinkService`. Discovery
delegates to an optional :class:`~.discovery.IssueDiscoverer` and, without one (no tracker bound),
refuses with ``INTAKE_DISABLED`` like import. The merge and release milestones delegate to an
optional :class:`~.milestones.MilestoneService` (they only draft; without one they refuse with
``INTAKE_DISABLED``, and ``NullIntake`` answers them for free). Waiting for the reporter
(bh-61xkc) composes the two: ``needs_info`` drafts the question through the outbox and moves the
report to ``intake:needs-info``, ``wake`` reads the thread and moves a report its reporter answered
back to ``intake:untriaged``; each refuses without the service it needs. With
``catch_up_on_link`` (and a milestone service) a link to work that already merged catches the
report up on the milestones it missed. Propose, review and apply-reviewed (bh-9aqmv) delegate to a
:class:`~.proposals.ProposalService` over this service's own verbs; without a ``triage`` binding
they refuse with ``INTAKE_DISABLED``.
Presentation, hive resolution, clone-on-demand and push stay with the legacy callers.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable

from ..contracts import IntakeLock, IntakePort, LabelPolicy, ReportStore
from ..domain import (
    INTAKE_DIM,
    INTAKE_NEEDS_INFO,
    INTAKE_UNTRIAGED,
    NEEDS_INFO,
    REPORT_TYPES,
    UNTRIAGED,
    ApplyOutcome,
    ApplyRequest,
    BeadView,
    ConsiderRequest,
    DiscoveryOutcome,
    DiscoveryRequest,
    Disposition,
    DispositionOutcome,
    DispositionRequest,
    HoldFinding,
    HoldFindingKind,
    HoldSweep,
    HoldSweepRequest,
    ImportOutcome,
    ImportRequest,
    IntakeRefused,
    IntakeStoreError,
    InvestigateOutcome,
    InvestigateRequest,
    IssueBinding,
    LabelBatch,
    LabelBatchApprove,
    LabelBatchOutcome,
    LabelDefineOutcome,
    LabelDefineRequest,
    LabelSyncItem,
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
    OpState,
    OutboxApprove,
    OutboxListing,
    OutboxQuery,
    OutboxResult,
    PresenceOutcome,
    PresenceRequest,
    ProposalReviewOutcome,
    ProposalReviewRequest,
    ProposeOutcome,
    ProposeRequest,
    QueueItem,
    QueueQuery,
    QueueView,
    RefusalReason,
    ReleaseFact,
    RenderedRecord,
    ReplyDraft,
    ReportDraft,
    ReportRelations,
    ReviewMetrics,
    ReviewPolicy,
    ReviewRequest,
    ReviewRun,
    StartFact,
    ThreadOutcome,
    ThreadRequest,
    ThreadView,
    WakeOutcome,
    WakeRequest,
    WakeSweep,
    WorkDraft,
    WorkProgress,
    channel_of,
    check_ask,
    check_wake,
    disposition_state,
    dupes_touching,
    hold_findings,
    is_needs_info,
    may_disposition,
    wakes,
)
from .discovery import IssueDiscoverer
from .importer import IssueImporter
from .labels import LabelProjection
from .linker import LinkService
from .milestones import MilestoneService
from .proposals import ProposalService, TriageBinding
from .review import ReviewService
from .sender import OutboxService
from .threads import ThreadService


def _state_value(label: str) -> tuple[str, str]:
    """A ``<dim>:<value>`` label-cache constant -> the ``(dim, value)`` ``set_state`` takes, so
    the vocabulary stays single-owned in the domain."""
    dimension, _, value = label.partition(":")
    return dimension, value


def _retag(exc: IntakeStoreError, operation: str, bead: str) -> IntakeStoreError:
    """The same store failure, re-labelled with the service step that hit it."""
    return IntakeStoreError(exc.detail, bead=bead, operation=operation, returncode=exc.returncode)


def _channel(view: BeadView) -> str:
    return channel_of(view.labels, view.source_system) or ""


class IntakeService:
    """Implements :class:`IntakePort` over one :class:`ReportStore` and a :class:`LabelPolicy`.

    ``binary`` is the CLI alias used in the event reason of a filed report (``filed via bh
    report``), so the recorded history reads as it does today. ``threads`` is bound only when
    comment ingestion is enabled (``intake.external.comments.enabled``, off by default),
    ``outbox`` only when the outbox is (``intake.external.outbox.enabled``, off by default) and
    ``importer`` and ``discoverer`` only when a tracker is (``bh work import`` and ``bh work
    discover`` refuse without it). ``lock`` is the
    per-binding lock a link or unlink holds while it writes (none: a single in-process writer).
    ``catch_up_on_link`` (``intake.external.milestones.catch_up_on_link``, off by default) lets a
    link to already merged work run the bound ``milestones`` service's catch-up. ``review`` is the
    host's :class:`ReviewPolicy` for review packets (the comment switches, read by the root; the
    default has both off, so ``review_packets`` refuses and names them). ``projection``
    (the label projection, bound only when ``intake.external.labels.enabled``) serves label sync
    and define; without it they refuse with ``INTAKE_DISABLED``. The label batch is the outbox's.
    ``triage`` binds propose, review and apply-reviewed (the repository facts, landed commits,
    branch, leak policy and re-filer); without it they refuse with ``INTAKE_DISABLED``. Apply
    refreshes the touched reports' labels through ``projection`` when it is bound.
    ``refresh_on_transition`` (``intake.external.labels.on_transition``, the host passes it; off
    by default) makes every transition below also draft or refresh the affected report's ONE
    ``labels`` record from stored state (:meth:`LabelProjection.refresh`; no tracker call, the
    sender reads live labels at render): dispose (accept, reject, reroute, promote), needs_info,
    wake, link, unlink and the milestone triggers (merge, release, started, delivery notices).
    It needs ``projection`` and never fails the transition; each public call refreshes a report
    once, and ``apply_reviewed`` defers to its own refresh of the touched reports."""

    def __init__(
        self,
        store: ReportStore,
        labels: LabelPolicy,
        *,
        binary: str = "bh",
        threads: ThreadService | None = None,
        outbox: OutboxService | None = None,
        importer: IssueImporter | None = None,
        lock: IntakeLock | None = None,
        discoverer: IssueDiscoverer | None = None,
        milestones: MilestoneService | None = None,
        catch_up_on_link: bool = False,
        review: ReviewPolicy | None = None,
        projection: LabelProjection | None = None,
        triage: TriageBinding | None = None,
        refresh_on_transition: bool = False,
    ) -> None:
        self._store = store
        self._catch_up = catch_up_on_link
        self._labels = labels
        self._binary = binary
        self._threads = threads
        self._outbox = outbox
        self._importer = importer
        self._discoverer = discoverer
        self._milestones = milestones
        self._linker = LinkService(store, lock)
        self._review = ReviewService(store, threads, review)
        self._projection = projection
        self._refresh_on_transition = refresh_on_transition
        self._deferred = 0  # > 0 while a caller that refreshes the touched reports itself runs
        self._refreshed: tuple[LabelSyncItem, ...] = ()
        self._proposals = ProposalService(store, self, triage, projection=projection)

    # -- the opt-in label refresh on a transition (bh-gjb5w) ----------------------------------

    @property
    def last_label_refresh(self) -> tuple[LabelSyncItem, ...]:
        """What the most recent transition's label refresh did, per report (empty: it did not
        run). A refresh that could not draft is its item's ``note`` or ``error``."""
        return self._refreshed

    def _refresh_labels(self, reports: Iterable[str], actor: str) -> None:
        """The transition hook: draft or refresh the ONE ``labels`` record of each distinct
        report. A no-op unless ``refresh_on_transition`` and ``projection`` are both bound."""
        if not self._refresh_on_transition or self._projection is None or self._deferred:
            return
        wanted = tuple(dict.fromkeys(reports))
        if wanted:
            self._refreshed = self._projection.refresh(wanted, actor)

    def _after_milestones(self, outcome: MilestoneOutcome, actor: str) -> MilestoneOutcome:
        self._refresh_labels(outcome.reports, actor)
        return outcome

    # -- filing ------------------------------------------------------------------------------

    def file_report(self, draft: ReportDraft) -> str:
        """Gate the type, check ONLY the new bead's own labels, create it, then stamp the
        orthogonal ``origin`` channel and ``intake=untriaged`` queue state (event-sourced)."""
        if draft.issue_type not in REPORT_TYPES:
            allowed = ", ".join(sorted(REPORT_TYPES))
            raise IntakeRefused(
                RefusalReason.INVALID_TYPE,
                f"--type must be one of {allowed} (got {draft.issue_type!r})",
            )
        new_labels = [*self._store.scope().hive.labels(), draft.origin, INTAKE_UNTRIAGED]
        bad = self._labels.violations(new_labels)
        if bad:
            raise IntakeRefused(
                RefusalReason.INVALID_LABELS,
                "report bead would carry invalid labels: " + "; ".join(bad),
            )
        report = self._store.create(draft)
        reason = f"filed via {self._binary} report"
        for label in (draft.origin, INTAKE_UNTRIAGED):
            dimension, value = _state_value(label)
            try:
                self._store.set_state(report, dimension, value, reason=reason, actor=draft.actor)
            except IntakeStoreError as exc:
                # The bead exists: carry its id so the caller can report a half-filed report.
                raise _retag(exc, f"stamp:{dimension}", report) from exc
        return report

    # -- queue -------------------------------------------------------------------------------

    def queue(self, query: QueueQuery) -> QueueView:
        """Untriaged intake beads, source-agnostic (keyed on ``intake:untriaged`` so any channel
        shares one queue); ``channel`` narrows to one resolved ``origin`` channel."""
        views = self._store.list_open_labeled(INTAKE_UNTRIAGED)
        if query.channel:
            views = tuple(v for v in views if _channel(v) == query.channel)
        items = tuple(
            QueueItem(v.id, v.title, v.issue_type, _channel(v), v.labels, v.binding, v.raw)
            for v in views
        )
        if not query.include_duplicates or not items:
            return QueueView(items)
        pairs = self._store.duplicate_pairs(threshold=query.threshold)
        return QueueView(items, dupes_touching(pairs, (i.id for i in items)))

    # -- dispositions ------------------------------------------------------------------------

    def dispose(self, request: DispositionRequest) -> DispositionOutcome:
        outcome = self._dispose(request)
        self._refresh_labels((request.report,), request.actor)
        return outcome

    def _dispose(self, request: DispositionRequest) -> DispositionOutcome:
        if request.disposition is Disposition.REJECT and not request.reason:
            raise IntakeRefused(
                RefusalReason.REASON_REQUIRED, "reject requires a --reason (reporter-visible)"
            )
        self._require_untriaged(request.report)
        handler = {
            Disposition.ACCEPT: self._accept,
            Disposition.REJECT: self._reject,
            Disposition.PROMOTE: self._promote,
            Disposition.REROUTE: self._reroute,
        }.get(request.disposition)
        if handler is None:  # pragma: no cover - the enum is closed; defensive
            raise IntakeRefused(RefusalReason.UNKNOWN_DISPOSITION, str(request.disposition))
        return handler(request)

    def reroute_elsewhere(
        self, request: DispositionRequest, refile: Callable[[BeadView], str]
    ) -> DispositionOutcome:
        """Reroute an untriaged report that belongs in ANOTHER store.

        A service is bound to one store and never reaches into another, so the re-filing is the
        caller's: ``refile`` receives the checked source bead (read once, before anything is
        written anywhere), files it where it belongs and returns the reason to record (it raises
        to abort, leaving the source untouched). This store then clears the source's intake state
        and closes it with that reason. Not an operation of :class:`IntakePort`: only the
        composition root, which can reach both stores, orchestrates it."""
        view = self._require_untriaged(request.report)
        reason = refile(view)
        outcome = self._reroute(
            dataclasses.replace(request, disposition=Disposition.REROUTE, reason=reason, seat="")
        )
        self._refresh_labels((request.report,), request.actor)
        return outcome

    def _require_untriaged(self, report: str) -> BeadView:
        """The bead, if it is an untriaged intake bead (a disposition never re-triages)."""
        view = self._store.get(report)
        if view is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report} not found")
        if not may_disposition(view.labels):
            raise IntakeRefused(
                RefusalReason.NOT_UNTRIAGED,
                f"{report} is not an untriaged intake bead — nothing to triage",
            )
        return view

    def _clear_intake(self, request: DispositionRequest, reason: str) -> str:
        value = disposition_state(request.disposition.value) or ""
        try:
            self._store.set_state(
                request.report, INTAKE_DIM, value, reason=reason, actor=request.actor
            )
        except IntakeStoreError as exc:
            raise _retag(exc, "clear_intake", request.report) from exc
        return value

    def _accept(self, request: DispositionRequest) -> DispositionOutcome:
        if request.work or request.new_work:
            return self._accept_to_work(request)
        if request.issue_type or request.priority:
            self._store.update_fields(
                request.report,
                issue_type=request.issue_type,
                priority=request.priority,
                actor=request.actor,
            )
        state = self._clear_intake(request, "accepted into backlog")
        return DispositionOutcome(
            request.report,
            request.disposition,
            state,
            issue_type=request.issue_type,
            priority=request.priority,
        )

    def _accept_to_work(self, request: DispositionRequest) -> DispositionOutcome:
        """Accept a bound report AND make it track work: an existing bead (``work``) or one triage
        creates now (``new_work``). Every refusal is decided before the first write, so a refused
        accept creates nothing; a failure after the link leaves the report tracked and still
        untriaged, and repeating the accept with ``--work <that bead>`` finishes it.

        ``--new-work``: the ``type``/``priority`` of the request go to the NEW bead (default: the
        report's type, no priority). ``--work``: they go to the report, as a plain accept's do."""
        if request.work and request.new_work:
            raise IntakeRefused(
                RefusalReason.INVALID_REQUEST, "--work and --new-work are mutually exclusive"
            )
        view = self._store.get(request.report)
        if view is None:  # pragma: no cover - dispose checked it
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{request.report} not found")
        created = False
        work = request.work
        if request.new_work:
            work = self._create_work_for(view, request)
            created = True
        try:
            self._linker.link(LinkRequest(request.report, work, request.actor))
        except (IntakeRefused, IntakeStoreError):
            if created:
                self._discard_work(work, request)
            raise
        if request.work and (request.issue_type or request.priority):
            self._store.update_fields(
                request.report,
                issue_type=request.issue_type,
                priority=request.priority,
                actor=request.actor,
            )
        state = self._clear_intake(request, f"accepted; tracks {work}")
        # after the intake clear: the completion policy never closes a report awaiting triage
        caught_up = () if created else self._caught_up(request.report, work, request.actor)
        return DispositionOutcome(
            request.report,
            request.disposition,
            state,
            issue_type=request.issue_type,
            priority=request.priority,
            work=work,
            created_work=created,
            milestones=caught_up,
        )

    def _create_work_for(self, view: BeadView, request: DispositionRequest) -> str:
        """Create the work bead of ``accept --new-work`` after checking, with nothing written,
        that the report could track one (a bound, open report that tracks nothing yet)."""
        if view.binding is None:
            raise IntakeRefused(
                RefusalReason.NOT_A_REPORT,
                f"{request.report} is not a report bound to an external issue; a plain accept "
                "already leaves it as ordinary backlog",
            )
        if view.status == "closed":
            raise IntakeRefused(
                RefusalReason.REPORT_CLOSED, f"{request.report} is closed; nothing to accept"
            )
        tracked = self._store.tracked_work(request.report)
        if tracked:
            raise IntakeRefused(
                RefusalReason.ALREADY_LINKED_ELSEWHERE,
                f"{request.report} already tracks {tracked}; finish with "
                f"`accept {request.report} --work {tracked}`, or `link --replace` to move it",
            )
        draft = WorkDraft(
            title=view.title,
            issue_type=request.issue_type or view.issue_type or "task",
            actor=request.actor,
            description=_new_work_description(request.report),
            priority=request.priority,
        )
        try:
            return self._store.create_work(draft)
        except IntakeStoreError as exc:
            raise _retag(exc, "create_work", request.report) from exc

    def _discard_work(self, work: str, request: DispositionRequest) -> None:
        """Close the work bead an accept created and could not link (best effort: the failure
        being reported is the link's, and an orphaned open work bead is visible in the backlog)."""
        self._close_created(work, request.report, request.actor, "accept")

    def _close_created(self, work: str, report: str, actor: str, verb: str) -> None:
        try:
            self._store.close(work, reason=f"not linked to {report}; {verb} failed", actor=actor)
        except (IntakeRefused, IntakeStoreError):
            pass

    def _reject(self, request: DispositionRequest) -> DispositionOutcome:
        state = self._clear_intake(request, f"rejected: {request.reason}")
        self._store.close(request.report, reason=request.reason, actor=request.actor)
        return DispositionOutcome(request.report, request.disposition, state)

    def _promote(self, request: DispositionRequest) -> DispositionOutcome:
        state = self._clear_intake(request, "handed to the planner for adoption")
        return DispositionOutcome(request.report, request.disposition, state)

    def _reroute(self, request: DispositionRequest) -> DispositionOutcome:
        if request.seat:  # bounce to a superintendent seat; the report stays queued
            self._store.assign(request.report, request.seat, actor=request.actor)
            return DispositionOutcome(request.report, request.disposition, "", seat=request.seat)
        state = self._clear_intake(request, request.reason)
        self._store.close(request.report, reason=request.reason, actor=request.actor)
        return DispositionOutcome(request.report, request.disposition, state)

    # -- bindings ----------------------------------------------------------------------------

    def binding_of(self, report: str) -> IssueBinding | None:
        view = self._store.get(report)
        return view.binding if view is not None else None

    def reports_for_work(self, work: str) -> tuple[IssueBinding, ...]:
        bindings: list[IssueBinding] = []
        for report in self._store.trackers_of(work):
            binding = self.binding_of(report)
            if binding is not None:
                bindings.append(binding)
        return tuple(bindings)

    # -- the link between reports and work (I5) ----------------------------------------------

    def link(self, request: LinkRequest) -> LinkOutcome:
        """A bound report ``tracks`` a work bead of this store (:class:`~.linker.LinkService`);
        with ``catch_up_on_link``, work that already merged then catches the report up."""
        outcome = self._linker.link(request)
        caught_up = self._caught_up(request.report, request.work, request.actor)
        self._refresh_labels((request.report,), request.actor)
        if not caught_up:
            return outcome
        return dataclasses.replace(outcome, milestones=caught_up)

    def _caught_up(self, report: str, work: str, actor: str) -> tuple:
        """The milestones a link to already merged ``work`` caught ``report`` up on (none unless
        ``catch_up_on_link`` and a milestone service are bound). Never raises: the link stands,
        and a failed catch-up is reported on its item."""
        if not self._catch_up or self._milestones is None:
            return ()
        return self._milestones.on_linked(report, work, actor).items

    def unlink(self, report: str, *, actor: str) -> LinkOutcome:
        outcome = self._linker.unlink(report, actor=actor)
        self._refresh_labels((report,), actor)
        return outcome

    def work_progress(self, report: str) -> WorkProgress:
        return self._linker.work_progress(report)

    # -- reproduction work (bh-u3a53): investigates, never tracks -----------------------------

    def investigate(self, request: InvestigateRequest) -> InvestigateOutcome:
        """A reproduction bead investigates a bound report: an existing bead (``work``) or one
        created now (``new_work``). The relation is its own edge, never ``tracks``: it writes no
        ``delivery`` value, runs no catch-up and drafts nothing, and no trigger ever reads it, so
        closing or merging the reproduction bead tells the reporter nothing and never closes the
        report. The report's ``intake`` value is untouched (reproduction is a triage step).

        Every refusal is decided before the first write; a created bead whose edge then fails is
        closed again (best effort), as ``accept --new-work`` does."""
        if bool(request.work) == request.new_work:
            raise IntakeRefused(
                RefusalReason.INVALID_REQUEST,
                "pass exactly one of --work <id> (an existing bead) or --new-work",
            )
        if not request.new_work:
            return self._linker.investigate(request.report, request.work, actor=request.actor)
        view = self._linker.check_investigable(request.report)
        draft = WorkDraft(
            title=f"Reproduce: {view.title}",
            issue_type=request.issue_type or "task",
            actor=request.actor,
            description=_reproduction_description(request.report),
            priority=request.priority,
        )
        try:
            work = self._store.create_work(draft)
        except IntakeStoreError as exc:
            raise _retag(exc, "create_work", request.report) from exc
        try:
            outcome = self._linker.investigate(request.report, work, actor=request.actor)
        except (IntakeRefused, IntakeStoreError):
            self._close_created(work, request.report, request.actor, "investigate")
            raise
        return dataclasses.replace(outcome, created_work=True)

    def uninvestigate(self, report: str, work: str, *, actor: str) -> InvestigateOutcome:
        return self._linker.uninvestigate(report, work, actor=actor)

    def relations(self, report: str) -> ReportRelations:
        return self._linker.relations(report)

    # -- the conversation model -------------------------------------------------------------

    def _thread_service(self) -> ThreadService:
        if self._threads is None:
            raise _comments_off()
        return self._threads

    def ingest_thread(self, request: ThreadRequest) -> ThreadOutcome:
        return self._thread_service().ingest_thread(request)

    def reconcile_thread(self, request: ThreadRequest) -> ThreadOutcome:
        return self._thread_service().reconcile_thread(request)

    def reconcile_presence(self, request: PresenceRequest) -> PresenceOutcome:
        return self._thread_service().reconcile_presence(request)

    def thread_of(self, report: str) -> ThreadView | None:
        return None if self._threads is None else self._threads.thread_of(report)

    def mark_considered(self, request: ConsiderRequest) -> ThreadOutcome:
        return self._thread_service().mark_considered(request)

    # -- waiting for the reporter (bh-61xkc) --------------------------------------------------

    def needs_info(self, request: NeedsInfoRequest) -> NeedsInfoOutcome:
        """Ask a bound report's reporter for missing facts: draft the question as a supervised
        ``reply`` record (posted only after an operator approves and sends it), then move the
        report to ``intake:needs-info`` so it leaves the untriaged queue. Refused, with nothing
        written, without the outbox or comment ingestion (a report that waits must be able to
        hear the reply), and for a born-native, closed or already fielded report, or one whose
        thread holds evidence triage has not read. The draft comes first, so a crash before the
        state write leaves an untriaged report and a draft that re-asking finds again (O2)."""
        outbox = self._outbox_service()
        threads = self._thread_service()
        view = self._store.get(request.report)
        thread = threads.thread_of(request.report)
        unread = thread.new_evidence if thread is not None else ()
        checked = check_ask(request.report, view, request.text, unread)
        drafted = outbox.draft_reply(ReplyDraft(request.report, request.text, request.actor))
        written = not is_needs_info(checked.labels)
        if written:
            try:
                self._store.set_state(
                    request.report,
                    INTAKE_DIM,
                    NEEDS_INFO,
                    reason=f"needs info: asked the reporter (outbox {drafted.outbox_id})",
                    actor=request.actor,
                )
            except IntakeStoreError as exc:
                raise _retag(exc, "stamp:intake", request.report) from exc
        self._refresh_labels((request.report,), request.actor)
        return NeedsInfoOutcome(
            request.report,
            drafted.outbox_id,
            state_written=written,
            held=drafted.state is OpState.HELD,
            detail=drafted.detail,
        )

    def wake(self, request: WakeRequest) -> WakeOutcome:
        """Move a report waiting on ``intake:needs-info`` back to ``intake:untriaged`` when its
        thread has new human evidence (a reporter reply since triage's last read). Reads the
        stored thread only (``thread ingest`` refreshes it); never marks anything considered, so
        the reply stays new evidence for the next triage pass. The report keeps its hold."""
        threads = self._thread_service()
        view = check_wake(request.report, self._store.get(request.report))
        outcome = self._wake(view, threads, request.actor)
        if outcome.woken:
            self._refresh_labels((request.report,), request.actor)
        return outcome

    def wake_all(self, *, actor: str) -> WakeSweep:
        """``wake`` every open report on ``intake:needs-info``; one report failing is reported on
        its item and the rest are still handled."""
        threads = self._thread_service()
        items: list[WakeOutcome] = []
        for view in self._store.list_open_labeled(INTAKE_NEEDS_INFO):
            try:
                items.append(self._wake(check_wake(view.id, view), threads, actor))
            except IntakeRefused as exc:
                items.append(WakeOutcome(view.id, error=exc.detail or exc.reason.value))
            except IntakeStoreError as exc:
                items.append(WakeOutcome(view.id, error=exc.detail))
        sweep = WakeSweep(tuple(items))
        self._refresh_labels(sweep.woken, actor)
        return sweep

    def _wake(self, view: BeadView, threads: ThreadService, actor: str) -> WakeOutcome:
        thread = threads.thread_of(view.id)
        evidence = thread.new_evidence if thread is not None else ()
        if not wakes(evidence):
            detail = (
                "no thread ingested yet (run `bh work thread ingest`)"
                if thread is None
                else "no new reply from the reporter since triage last read the thread"
            )
            return WakeOutcome(view.id, detail=detail)
        try:
            self._store.set_state(
                view.id,
                INTAKE_DIM,
                UNTRIAGED,
                reason=f"woken: the reporter replied ({', '.join(evidence)})",
                actor=actor,
            )
        except IntakeStoreError as exc:
            raise _retag(exc, "stamp:intake", view.id) from exc
        return WakeOutcome(view.id, woken=True, evidence=tuple(evidence))

    # -- the lifetime intake-hold ------------------------------------------------------------

    def sweep_holds(self, request: HoldSweepRequest) -> HoldSweep:
        """Compare the bound reports with the open holds; with ``request.repair`` also fix what is
        repairable: open a hold on an open report that has none, resolve a hold left on a closed
        report. Idempotent, and a repair that fails is reported, not raised, so one bad report
        never hides the rest. An orphaned hold is reported only."""
        reports = self._store.bound_reports()
        findings = hold_findings(
            [(view.id, view.status == "closed") for view in reports], self._store.holds()
        )
        if not request.repair:
            return HoldSweep(findings)
        repaired: list[HoldFinding] = []
        failed: list[tuple[HoldFinding, str]] = []
        for finding in findings:
            if not finding.repairable:
                continue
            try:
                if finding.kind is HoldFindingKind.MISSING:
                    self._store.open_hold(finding.report, actor=request.actor)
                else:
                    self._store.resolve_hold(finding.gate, actor=request.actor)
            except IntakeStoreError as exc:
                failed.append((finding, exc.detail))
            else:
                repaired.append(finding)
        return HoldSweep(findings, tuple(repaired), tuple(failed))

    # -- the supervised outbox ---------------------------------------------------------------

    def _outbox_service(self) -> OutboxService:
        if self._outbox is None:
            raise _outbox_off()
        return self._outbox

    def draft_reply(self, request: ReplyDraft) -> OutboxResult:
        return self._outbox_service().draft_reply(request)

    def outbox(self, query: OutboxQuery) -> OutboxListing:
        return self._outbox_service().outbox(query)

    def render(self, outbox_id: str) -> RenderedRecord:
        return self._outbox_service().render(outbox_id)

    def approve(self, request: OutboxApprove) -> OutboxResult:
        return self._outbox_service().approve(request)

    def send(self, outbox_id: str, *, actor: str, kind: OpKind = OpKind.COMMENT) -> OutboxResult:
        return self._outbox_service().send(outbox_id, actor=actor, kind=kind)

    def reconcile(self, outbox_id: str | None = None, *, actor: str) -> tuple[OutboxResult, ...]:
        return self._outbox_service().reconcile(outbox_id, actor=actor)

    def discard(
        self,
        outbox_id: str,
        *,
        reason: str,
        actor: str,
        accept_unknown: bool = False,
        kind: OpKind | None = None,
    ) -> OutboxResult:
        return self._outbox_service().discard(
            outbox_id, reason=reason, actor=actor, accept_unknown=accept_unknown, kind=kind
        )

    def retry(self, outbox_id: str, *, actor: str, kind: OpKind = OpKind.COMMENT) -> OutboxResult:
        return self._outbox_service().retry(outbox_id, actor=actor, kind=kind)

    # -- the merge and release milestones (I10): they draft, they never send -----------------

    def _milestone_service(self) -> MilestoneService:
        if self._milestones is None:
            raise _disabled()
        return self._milestones

    def on_work_merged(self, fact: MergeFact) -> MilestoneOutcome:
        return self._after_milestones(self._milestone_service().on_work_merged(fact), fact.actor)

    def on_release(self, fact: ReleaseFact) -> MilestoneOutcome:
        return self._after_milestones(self._milestone_service().on_release(fact), fact.actor)

    def on_work_started(self, fact: StartFact) -> MilestoneOutcome:
        return self._after_milestones(self._milestone_service().on_work_started(fact), fact.actor)

    def delivery_notice(self, fact: NoticeFact) -> MilestoneOutcome:
        return self._after_milestones(self._milestone_service().delivery_notice(fact), fact.actor)

    # -- review packets (bh-hrx2r): ingest every pending thread, then cluster-aware batches -----

    def review_packets(self, request: ReviewRequest) -> ReviewRun:
        return self._review.packets(request)

    def review_metrics(self) -> ReviewMetrics:
        return self._review.metrics()

    # -- the label projection (bh-uim9i): sync and define draft or apply; the batch is the
    # outbox's own approval, so it needs the outbox, not the projection --------------------------

    def _projection_service(self) -> LabelProjection:
        if self._projection is None:
            raise _labels_off()
        return self._projection

    def sync_labels(self, request: LabelSyncRequest) -> LabelSyncOutcome:
        return self._projection_service().sync(request)

    def define_labels(self, request: LabelDefineRequest) -> LabelDefineOutcome:
        return self._projection_service().define(request)

    def review_label_batch(self) -> LabelBatch:
        return self._outbox_service().review_label_batch()

    def approve_label_batch(self, request: LabelBatchApprove) -> LabelBatchOutcome:
        return self._outbox_service().approve_label_batch(request)

    # -- propose, review and apply-reviewed (bh-9aqmv) ----------------------------------------

    def propose(self, request: ProposeRequest) -> ProposeOutcome:
        return self._proposals.propose(request)

    def review_proposal(self, request: ProposalReviewRequest) -> ProposalReviewOutcome:
        return self._proposals.review(request)

    def apply_reviewed(self, request: ApplyRequest) -> ApplyOutcome:
        # apply refreshes the touched reports' labels itself (fresh, from the tracker): the verbs
        # it runs through this service must not draft a second refresh for the same report
        self._deferred += 1
        try:
            return self._proposals.apply_reviewed(request)
        finally:
            self._deferred -= 1

    # -- discovery (I6) ----------------------------------------------------------------------

    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        """Opt-in, pull-only discovery through the import upsert; see :class:`IssueDiscoverer`."""
        if self._discoverer is None:
            raise IntakeRefused(
                RefusalReason.INTAKE_DISABLED,
                "discover needs a bound tracker with issues.read enabled "
                "(external intake is opt-in, and so is the tracker)",
            )
        return self._discoverer.discover(request)

    # -- import (I4) -------------------------------------------------------------------------

    def import_issue(self, request: ImportRequest) -> ImportOutcome:
        """Import one external issue as an untriaged, held report; see :class:`IssueImporter`."""
        if self._importer is None:
            raise IntakeRefused(
                RefusalReason.INTAKE_DISABLED,
                "import needs a bound tracker with issues.read enabled "
                "(external intake is opt-in, and so is the tracker)",
            )
        return self._importer.import_issue(request)


def _new_work_description(report: str) -> str:
    """What triage writes on the work bead of ``accept --new-work``. Never the issue body: that is
    untrusted public text, and the report that holds it stays the one place it is read."""
    return (
        f"Work created by triage for intake report {report}. Scope and acceptance: to be written "
        f"by triage on this bead; read {report} for what was reported (it is held, not work)."
    )


def _reproduction_description(report: str) -> str:
    """What triage writes on the reproduction bead of ``investigate --new-work``. Never the issue
    body (untrusted public text read on the report), and it says why closing it tells the reporter
    nothing: the bead investigates the report, it does not track it."""
    return (
        f"Reproduction task created by triage for intake report {report}. Verify the reported "
        "behaviour and record reusable evidence: minimal steps or a regression test, the tested "
        "commit or version and environment, observed versus expected behaviour, and a bounded "
        "impact assessment; a failed attempt says whether the evidence was insufficient, the "
        "environment differed, the behaviour is already fixed, or the claim is disproven. This "
        f"bead investigates {report} and does not track it, so closing or merging it sends the "
        f"reporter no delivery milestone and never closes the report. Read {report} for what was "
        "reported (it is held, not work)."
    )


def _comments_off() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        "comment ingestion is off for this hive (intake.external.comments.enabled; opt-in)",
    )


def _outbox_off() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.OUTBOX_DISABLED,
        "the outbox is off for this hive (intake.external.outbox.enabled; opt-in)",
    )


def _labels_off() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        "the label projection is off for this hive (intake.external.labels.enabled; opt-in)",
    )


class NullIntake:
    """Bound when intake is off: empty reads, an empty work lookup, and every write refused with
    ``INTAKE_DISABLED``. Free to call from hooks (merge, release) in every hive."""

    def file_report(self, draft: ReportDraft) -> str:
        raise _disabled()

    def import_issue(self, request: ImportRequest) -> ImportOutcome:
        raise _disabled()

    def link(self, request: LinkRequest) -> LinkOutcome:
        raise _disabled()

    def unlink(self, report: str, *, actor: str) -> LinkOutcome:
        raise _disabled()

    def queue(self, query: QueueQuery) -> QueueView:
        return QueueView()

    def dispose(self, request: DispositionRequest) -> DispositionOutcome:
        raise _disabled()

    def binding_of(self, report: str) -> IssueBinding | None:
        return None

    def reports_for_work(self, work: str) -> tuple[IssueBinding, ...]:
        return ()

    def work_progress(self, report: str) -> WorkProgress:
        return WorkProgress.UNLINKED

    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        raise _disabled()

    def ingest_thread(self, request: ThreadRequest) -> ThreadOutcome:
        raise _disabled()

    def reconcile_thread(self, request: ThreadRequest) -> ThreadOutcome:
        raise _disabled()

    def reconcile_presence(self, request: PresenceRequest) -> PresenceOutcome:
        raise _disabled()

    def thread_of(self, report: str) -> ThreadView | None:
        return None

    def mark_considered(self, request: ConsiderRequest) -> ThreadOutcome:
        raise _disabled()

    def needs_info(self, request: NeedsInfoRequest) -> NeedsInfoOutcome:
        raise _disabled()

    def wake(self, request: WakeRequest) -> WakeOutcome:
        raise _disabled()

    def wake_all(self, *, actor: str) -> WakeSweep:
        raise _disabled()

    def sweep_holds(self, request: HoldSweepRequest) -> HoldSweep:
        return HoldSweep()

    def draft_reply(self, request: ReplyDraft) -> OutboxResult:
        raise _disabled()

    def outbox(self, query: OutboxQuery) -> OutboxListing:
        return OutboxListing()

    def render(self, outbox_id: str) -> RenderedRecord:
        raise _disabled()

    def approve(self, request: OutboxApprove) -> OutboxResult:
        raise _disabled()

    def send(self, outbox_id: str, *, actor: str, kind: OpKind = OpKind.COMMENT) -> OutboxResult:
        raise _disabled()

    def reconcile(self, outbox_id: str | None = None, *, actor: str) -> tuple[OutboxResult, ...]:
        raise _disabled()

    def discard(
        self,
        outbox_id: str,
        *,
        reason: str,
        actor: str,
        accept_unknown: bool = False,
        kind: OpKind | None = None,
    ) -> OutboxResult:
        raise _disabled()

    def retry(self, outbox_id: str, *, actor: str, kind: OpKind = OpKind.COMMENT) -> OutboxResult:
        raise _disabled()

    def on_work_merged(self, fact: MergeFact) -> MilestoneOutcome:
        return MilestoneOutcome()  # free: nothing is read, written or drafted

    def on_release(self, fact: ReleaseFact) -> MilestoneOutcome:
        return MilestoneOutcome()

    def on_work_started(self, fact: StartFact) -> MilestoneOutcome:
        return MilestoneOutcome()  # free: the claim hook reads, writes and drafts nothing

    def delivery_notice(self, fact: NoticeFact) -> MilestoneOutcome:
        raise _disabled()

    def investigate(self, request: InvestigateRequest) -> InvestigateOutcome:
        raise _disabled()

    def uninvestigate(self, report: str, work: str, *, actor: str) -> InvestigateOutcome:
        raise _disabled()

    def relations(self, report: str) -> ReportRelations:
        return ReportRelations(report)  # an empty read, like ``work_progress``

    def review_packets(self, request: ReviewRequest) -> ReviewRun:
        raise _disabled()  # review ingests threads: a write

    def review_metrics(self) -> ReviewMetrics:
        return ReviewMetrics()  # an empty read

    def sync_labels(self, request: LabelSyncRequest) -> LabelSyncOutcome:
        raise _disabled()

    def define_labels(self, request: LabelDefineRequest) -> LabelDefineOutcome:
        raise _disabled()

    def review_label_batch(self) -> LabelBatch:
        return LabelBatch()  # an empty read, like ``outbox``

    def approve_label_batch(self, request: LabelBatchApprove) -> LabelBatchOutcome:
        raise _disabled()

    def propose(self, request: ProposeRequest) -> ProposeOutcome:
        raise _disabled()

    def review_proposal(self, request: ProposalReviewRequest) -> ProposalReviewOutcome:
        raise _disabled()

    def apply_reviewed(self, request: ApplyRequest) -> ApplyOutcome:
        raise _disabled()  # even a dry run reads a store intake does not have


def _disabled() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED, "external intake is off for this hive (it is opt-in)"
    )


def bind_intake(enabled: bool, build: Callable[[], IntakePort]) -> IntakePort:
    """The port a consumer receives: ``build()`` when intake is on, else :class:`NullIntake`.

    ``build`` is only called when enabled, so a disabled hive never constructs a store."""
    return build() if enabled else NullIntake()


__all__ = ["IntakeService", "NullIntake", "bind_intake"]
