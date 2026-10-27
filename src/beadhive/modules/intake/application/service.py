"""``IntakeService``, the one implementation of :class:`IntakePort`, and ``NullIntake``.

The skeleton implements what only needs the :class:`ReportStore`: filing a report, the queue,
the dispositions, bindings and the work lookup. Import, link and discovery are operations of the
port that later beads own; until then they refuse with ``NOT_AVAILABLE``. Presentation, hive
resolution, clone-on-demand and push stay with the legacy callers.
"""

from __future__ import annotations

from collections.abc import Callable

from ..contracts import IntakePort, LabelPolicy, ReportStore
from ..domain import (
    INTAKE_DIM,
    INTAKE_UNTRIAGED,
    REPORT_TYPES,
    BeadView,
    DiscoveryOutcome,
    DiscoveryRequest,
    Disposition,
    DispositionOutcome,
    DispositionRequest,
    ImportOutcome,
    ImportRequest,
    IntakeRefused,
    IssueBinding,
    LinkOutcome,
    LinkRequest,
    QueueItem,
    QueueQuery,
    QueueView,
    RefusalReason,
    ReportDraft,
    channel_of,
    disposition_state,
    dupes_touching,
    may_disposition,
)


def _state_value(label: str) -> tuple[str, str]:
    """A ``<dim>:<value>`` label-cache constant -> the ``(dim, value)`` ``set_state`` takes, so
    the vocabulary stays single-owned in the domain."""
    dimension, _, value = label.partition(":")
    return dimension, value


def _channel(view: BeadView) -> str:
    return channel_of(view.labels, view.source_system) or ""


class IntakeService:
    """Implements :class:`IntakePort` over one :class:`ReportStore` and a :class:`LabelPolicy`.

    ``binary`` is the CLI alias used in the event reason of a filed report (``filed via bh
    report``), so the recorded history reads as it does today."""

    def __init__(self, store: ReportStore, labels: LabelPolicy, *, binary: str = "bh") -> None:
        self._store = store
        self._labels = labels
        self._binary = binary

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
            self._store.set_state(report, dimension, value, reason=reason, actor=draft.actor)
        return report

    # -- queue -------------------------------------------------------------------------------

    def queue(self, query: QueueQuery) -> QueueView:
        """Untriaged intake beads, source-agnostic (keyed on ``intake:untriaged`` so any channel
        shares one queue); ``channel`` narrows to one resolved ``origin`` channel."""
        views = self._store.list_open_labeled(INTAKE_UNTRIAGED)
        if query.channel:
            views = tuple(v for v in views if _channel(v) == query.channel)
        items = tuple(
            QueueItem(v.id, v.title, v.issue_type, _channel(v), v.labels, v.binding) for v in views
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
        view = self._store.get(request.report)
        if view is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{request.report} not found")
        if not may_disposition(view.labels):
            raise IntakeRefused(
                RefusalReason.NOT_UNTRIAGED,
                f"{request.report} is not an untriaged intake bead — nothing to triage",
            )
        handler = {
            Disposition.ACCEPT: self._accept,
            Disposition.REJECT: self._reject,
            Disposition.PROMOTE: self._promote,
            Disposition.REROUTE: self._reroute,
        }.get(request.disposition)
        if handler is None:  # pragma: no cover - the enum is closed; defensive
            raise IntakeRefused(RefusalReason.UNKNOWN_DISPOSITION, str(request.disposition))
        return handler(request)

    def _clear_intake(self, request: DispositionRequest, reason: str) -> str:
        value = disposition_state(request.disposition.value) or ""
        self._store.set_state(request.report, INTAKE_DIM, value, reason=reason, actor=request.actor)
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

    # -- owned by later beads ----------------------------------------------------------------

    def import_issue(self, request: ImportRequest) -> ImportOutcome:
        raise _not_available("import")

    def link(self, request: LinkRequest) -> LinkOutcome:
        raise _not_available("link")

    def unlink(self, report: str, *, actor: str) -> LinkOutcome:
        raise _not_available("unlink")

    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        raise _not_available("discover")


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


def _disabled() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED, "external intake is off for this hive (it is opt-in)"
    )


def bind_intake(enabled: bool, build: Callable[[], IntakePort]) -> IntakePort:
    """The port a consumer receives: ``build()`` when intake is on, else :class:`NullIntake`.

    ``build`` is only called when enabled, so a disabled hive never constructs a store."""
    return build() if enabled else NullIntake()


__all__ = ["IntakeService", "NullIntake", "bind_intake"]
