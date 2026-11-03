"""``LinkService``: many reports to one work bead (I5, external-intake-module-adr.md section 6).

The operations behind ``bh work link`` and ``accept --work`` / ``--new-work``. A bound report
``tracks`` a work bead through one ``tracks`` edge inside the operational store (never the public
one, never another hive's), several reports to one work. Linking changes no readiness: the edge
is non-blocking in bd, the report keeps its ``intake-hold:`` gate for life so it is never ready
work, and the work bead is ordinary dispatchable work. A link writes ``delivery:pending``; an
unlink cannot clear it (``bd set-state`` has no unset), so it keeps the value and records an
``unlinked`` fact in the report's ``intake.outbox`` document, where the other delivery facts live.

A reproduction bead may also INVESTIGATE a report (bh-u3a53): its own edge, never ``tracks``, so
no milestone trigger, claim hook or link catch-up (which all read ``trackers_of``) ever sees it. An
investigation writes that edge and nothing else.

Every refusal is decided before the first write (the pure rules in ``domain/link.py``). Writes run
under the :class:`IntakeLock` (the one lock of every mutating intake operation) when one is bound;
the ``intake.outbox`` write re-checks the stored ``seq`` as always. Nothing here reads or writes a
tracker.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator

from ..contracts import IntakeLock, ReportStore
from ..domain import (
    DELIVERY_DIM,
    BeadView,
    DeliveryFacts,
    IntakeRefused,
    IntakeStoreError,
    InvestigateOutcome,
    LinkKind,
    LinkOutcome,
    LinkRequest,
    OutboxDocument,
    RefusalReason,
    ReportRelations,
    UnlinkedFact,
    WorkProgress,
    check_investigable,
    check_investigate,
    check_link,
    delivery_after_unlink,
    delivery_at_link,
    delivery_of,
    in_operational_store,
    work_progress_of,
)
from .threads import binding_key


def _retag(exc: IntakeStoreError, operation: str, bead: str) -> IntakeStoreError:
    return IntakeStoreError(exc.detail, bead=bead, operation=operation, returncode=exc.returncode)


class LinkService:
    """Links, unlinks and reads the progress of the work a report tracks, over one
    :class:`ReportStore`. ``lock`` is the per-binding :class:`IntakeLock`; ``None`` takes none (an
    in-process store with a single writer)."""

    def __init__(self, store: ReportStore, lock: IntakeLock | None = None) -> None:
        self._store = store
        self._lock = lock

    @contextlib.contextmanager
    def _held(self, report: BeadView) -> Iterator[None]:
        if self._lock is None or report.binding is None:
            yield
            return
        with self._lock.hold(binding_key(report.binding)):
            yield

    # -- link ----------------------------------------------------------------------------------

    def check(self, request: LinkRequest) -> tuple[BeadView, LinkKind, str]:
        """Every refusal of a link, with nothing written: the report, the kind of link it would
        be and the work it tracks now (empty when none)."""
        scope = self._store.scope()
        report = self._store.get(request.report)
        work = self._store.get(request.work) if in_operational_store(scope, request.work) else None
        tracked = self._store.tracked_work(request.report) if report is not None else None
        kind = check_link(
            scope=scope,
            report_id=request.report,
            report=report,
            work_id=request.work,
            work=work,
            tracked=tracked,
            replace=request.replace,
        )
        assert report is not None  # check_link refuses a missing report
        return report, kind, tracked or ""

    def link(self, request: LinkRequest) -> LinkOutcome:
        """Make ``request.report`` track ``request.work``; idempotent (an existing edge only
        repairs a missing ``delivery`` value or a stale ``unlinked`` fact)."""
        report, kind, tracked = self.check(request)
        with self._held(report):
            doc = self._read_outbox(request.report)
            stale = doc.facts.unlinked if doc is not None else None
            if kind is LinkKind.RELINKED:
                self._edge("untrack", self._store.untrack, request.report, tracked, request.actor)
            if kind is not LinkKind.ALREADY_LINKED:
                self._edge("track", self._store.track, request.report, request.work, request.actor)
            if stale is not None:
                self._write_facts(request.report, doc, None, request.actor)
            value = delivery_at_link(
                report.labels,
                edge_written=kind is not LinkKind.ALREADY_LINKED,
                unlinked_work=stale.work if stale is not None else "",
                work=request.work,
            )
            if value is not None:
                try:
                    self._store.set_state(
                        request.report,
                        DELIVERY_DIM,
                        value,
                        reason=f"linked to {request.work}",
                        actor=request.actor,
                    )
                except IntakeStoreError as exc:
                    raise _retag(exc, f"stamp:{DELIVERY_DIM}", request.report) from exc
        return LinkOutcome(
            kind,
            request.report,
            request.work,
            previous=tracked if kind is LinkKind.RELINKED else "",
            delivery=value or delivery_of(report.labels) or "",
        )

    @staticmethod
    def _edge(operation: str, call: Callable[..., None], report: str, work: str, actor: str):
        try:
            call(report, work, actor=actor)
        except IntakeStoreError as exc:
            raise _retag(exc, operation, report) from exc

    # -- unlink --------------------------------------------------------------------------------

    def unlink(self, report_id: str, *, actor: str) -> LinkOutcome:
        """Remove the report's ``tracks`` edge. The ``delivery`` value stays (it cannot be
        cleared) and the ``unlinked`` fact records the work it described; the other reports of that
        work are untouched. A report that tracks nothing is a no-op."""
        report = self._store.get(report_id)
        if report is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report_id} not found")
        if report.binding is None:
            raise IntakeRefused(
                RefusalReason.NOT_A_REPORT,
                f"{report_id} is not a report bound to an external issue; there is nothing to "
                "unlink",
            )
        work = self._store.tracked_work(report_id)
        kept = delivery_after_unlink(report.labels).value or ""
        if work is None:
            return LinkOutcome(LinkKind.UNLINKED, report_id, delivery=kept)
        with self._held(report):
            doc = self._read_outbox(report_id)
            # the fact first: a failure after it leaves an edge and a fact that the next unlink
            # (or link) tidies; the reverse order would leave a value no fact explains
            self._write_facts(report_id, doc, UnlinkedFact(work), actor)
            try:
                self._store.untrack(report_id, work, actor=actor)
            except IntakeStoreError as exc:
                raise _retag(exc, "untrack", report_id) from exc
        return LinkOutcome(LinkKind.UNLINKED, report_id, work, delivery=kept)

    # -- the unlinked fact ---------------------------------------------------------------------

    def _read_outbox(self, report: str) -> OutboxDocument | None:
        return self._store.read_outbox(report)

    def _write_facts(
        self,
        report: str,
        doc: OutboxDocument | None,
        unlinked: UnlinkedFact | None,
        actor: str,
    ) -> None:
        """Set (or clear) the ``unlinked`` fact in one ``intake.outbox`` write, keeping every other
        fact and record. Writes nothing when the stored fact is already the wanted one."""
        current = doc or OutboxDocument()
        if current.facts.unlinked == unlinked:
            return
        facts = DeliveryFacts(current.facts.merged, current.facts.released, unlinked)
        self._store.write_outbox(
            report,
            OutboxDocument(current.seq + 1, facts, current.records, current.schema),
            expect_seq=current.seq,
            actor=actor,
        )

    # -- investigations (bh-u3a53): reproduction work that never carries delivery ---------------

    def check_investigable(self, report_id: str) -> BeadView:
        """The report-side refusals of an investigation, with nothing written (``--new-work``
        runs them before it creates the reproduction bead)."""
        report = self._store.get(report_id) if report_id else None
        check_investigable(scope=self._store.scope(), report_id=report_id, report=report)
        assert report is not None  # check_investigable refuses a missing report
        return report

    def investigate(self, report_id: str, work_id: str, *, actor: str) -> InvestigateOutcome:
        """Make ``work_id`` investigate ``report_id``; idempotent. Writes ONE edge and nothing
        else: no ``delivery`` value, no outbox fact, no milestone. The ``tracks`` edge (if any)
        and every other investigation of the report are untouched."""
        scope = self._store.scope()
        report = self._store.get(report_id)
        work = self._store.get(work_id) if in_operational_store(scope, work_id) else None
        tracked = self._store.tracked_work(report_id) if report is not None else None
        investigated = self._store.investigations_of(report_id) if report is not None else ()
        kind = check_investigate(
            scope=scope,
            report_id=report_id,
            report=report,
            work_id=work_id,
            work=work,
            tracked=tracked,
            investigated_by=investigated,
        )
        assert report is not None  # check_investigate refuses a missing report
        if kind is LinkKind.LINKED:
            with self._held(report):
                self._edge("investigate", self._store.investigate, report_id, work_id, actor)
        return InvestigateOutcome(kind, report_id, work_id, relations=self.relations(report_id))

    def uninvestigate(self, report_id: str, work_id: str, *, actor: str) -> InvestigateOutcome:
        """Remove ``work_id``'s investigation of ``report_id``; the ``tracks`` edge and the other
        investigations stay. Allowed on a closed report (tidying never needs it open); a bead that
        does not investigate the report is a no-op with an empty ``work``."""
        report = self._store.get(report_id)
        if report is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report_id} not found")
        if report.binding is None:
            raise IntakeRefused(
                RefusalReason.NOT_A_REPORT,
                f"{report_id} is not a report bound to an external issue; nothing investigates it",
            )
        if work_id not in self._store.investigations_of(report_id):
            return InvestigateOutcome(
                LinkKind.UNLINKED, report_id, relations=self.relations(report_id)
            )
        with self._held(report):
            self._edge("uninvestigate", self._store.uninvestigate, report_id, work_id, actor)
        return InvestigateOutcome(
            LinkKind.UNLINKED, report_id, work_id, relations=self.relations(report_id)
        )

    def relations(self, report_id: str) -> ReportRelations:
        """Both kinds of link of one report, read on demand: the work it tracks (with that work's
        derived progress) and every bead that investigates it."""
        if self._store.get(report_id) is None:
            raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report_id} not found")
        tracked = self._store.tracked_work(report_id) or ""
        view = self._store.get(tracked) if tracked else None
        return ReportRelations(
            report_id,
            tracks=tracked,
            investigated_by=self._store.investigations_of(report_id),
            progress=(
                WorkProgress.UNLINKED
                if view is None
                else work_progress_of(view.status, view.labels)
            ),
        )

    # -- the derived progress ------------------------------------------------------------------

    def work_progress(self, report_id: str) -> WorkProgress:
        """Where the tracked work has got to: derived on read from its status and ``review:``
        label, never stored. ``unlinked`` when the report tracks nothing (or the work is gone)."""
        work = self._store.tracked_work(report_id)
        view = self._store.get(work) if work else None
        if view is None:
            return WorkProgress.UNLINKED
        return work_progress_of(view.status, view.labels)


__all__ = ["LinkService"]
