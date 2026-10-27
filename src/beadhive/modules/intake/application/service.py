"""``IntakeService``, the one implementation of :class:`IntakePort`, and ``NullIntake``.

The service implements what only needs the :class:`ReportStore`: filing a report, the queue, the
dispositions, bindings and the work lookup (``bh report``, ``bh escalate`` and the triage verbs
run through it). The conversation operations delegate to an optional
:class:`~.threads.ThreadService`; without one (comment ingestion off, the default) they refuse
with ``INTAKE_DISABLED`` and ``thread_of`` reads nothing. The outbox operations delegate to an
optional :class:`~.sender.OutboxService`; without one (the outbox off, the default) every one of
them refuses with ``OUTBOX_DISABLED``, so nothing can be drafted, approved or sent. Import delegates
to an optional :class:`~.importer.IssueImporter`; without one (no tracker bound) it refuses with
``INTAKE_DISABLED``. Link and discovery are operations of the port that later beads own; until then
they refuse with ``NOT_AVAILABLE``.
Presentation, hive resolution, clone-on-demand and push stay with the legacy callers.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from ..contracts import IntakePort, LabelPolicy, ReportStore
from ..domain import (
    INTAKE_DIM,
    INTAKE_UNTRIAGED,
    REPORT_TYPES,
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
    IssueBinding,
    LinkOutcome,
    LinkRequest,
    OpKind,
    OutboxApprove,
    OutboxListing,
    OutboxQuery,
    OutboxResult,
    PresenceOutcome,
    PresenceRequest,
    QueueItem,
    QueueQuery,
    QueueView,
    RefusalReason,
    RenderedRecord,
    ReplyDraft,
    ReportDraft,
    ThreadOutcome,
    ThreadRequest,
    ThreadView,
    channel_of,
    disposition_state,
    dupes_touching,
    hold_findings,
    may_disposition,
)
from .importer import IssueImporter
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
    ``importer`` only when a tracker is (``bh work import`` refuses without it)."""

    def __init__(
        self,
        store: ReportStore,
        labels: LabelPolicy,
        *,
        binary: str = "bh",
        threads: ThreadService | None = None,
        outbox: OutboxService | None = None,
        importer: IssueImporter | None = None,
    ) -> None:
        self._store = store
        self._labels = labels
        self._binary = binary
        self._threads = threads
        self._outbox = outbox
        self._importer = importer

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
        return self._reroute(
            dataclasses.replace(request, disposition=Disposition.REROUTE, reason=reason, seat="")
        )

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

    # -- owned by later beads ----------------------------------------------------------------

    def link(self, request: LinkRequest) -> LinkOutcome:
        raise _not_available("link")

    def unlink(self, report: str, *, actor: str) -> LinkOutcome:
        raise _not_available("unlink")

    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        raise _not_available("discover")

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


def _not_available(operation: str) -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.NOT_AVAILABLE, f"intake {operation} is not implemented in this build"
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


def _disabled() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED, "external intake is off for this hive (it is opt-in)"
    )


def bind_intake(enabled: bool, build: Callable[[], IntakePort]) -> IntakePort:
    """The port a consumer receives: ``build()`` when intake is on, else :class:`NullIntake`.

    ``build`` is only called when enabled, so a disabled hive never constructs a store."""
    return build() if enabled else NullIntake()


__all__ = ["IntakeService", "NullIntake", "bind_intake"]
