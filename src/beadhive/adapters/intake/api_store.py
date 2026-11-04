"""``ApiReportStore``: the intake :class:`ReportStore` served from the supervised Beads service.

The same port, the same behaviour, over the API where the Beads service has an operation for it
(``operation_matrix_v1.json``: ``api-ready``) and over the ``bd`` CLI where it has none. It is a
:class:`~beadhive.adapters.intake.bead_store.BdReportStore` whose reads and api-ready writes are
answered by an injected :class:`ReportApi` instead of a ``bd`` subprocess, so every policy the
parent owns (classification inputs, the held close, the ``intake.source`` / ``intake.thread`` /
``intake.outbox`` documents, the seq re-check) is shared, not copied.

What moves to the API, and what does not (and why):

* reads: one bead (``get``, ``read_thread``, ``read_outbox``, the hold and tracking reads, all
  answered by the ONE row, whose ``dependencies`` carry each neighbour's status and reason), the
  open-label queue, the bound-report and outbox listings;
* writes: type/priority updates, assignment, tracking and investigates edges, the plain and the
  held close, and the three metadata documents (a compare-and-set against the row just read, so a
  document that moved since is refused where ``bd update --metadata`` would overwrite it);
* the lookup by ``external_ref`` answers from an index of the bound reports (the API has no
  ``external_ref`` filter) and CONFIRMS A MISS on the CLI, so a bead bound by ``external_ref``
  alone is still found and a re-import still never duplicates it;
* CLI only: the creates, because the service cannot record ``created_by`` (nor derive ``owner``
  as ``bd create`` does) and the reporter's provenance is part of a report's state; the gate
  create / resolve / list and ``bd set-state``, which the operation matrix classifies as CLI
  compatibility (and whose event history is part of intake's contract); and ``bd
  find-duplicates``, which has no API at all.

The route is decided on the first API operation, which every command that mixes the two makes
BEFORE its first CLI write (each reads the report first); a command that needs only the CLI
(``bh report`` filing) never asks and never opens a session.

Nothing here imports a workspace package or the composition root: ``api`` is the injected route
(``None`` selects the parent's CLI behaviour for the whole store), and a failed API call surfaces
as :class:`IntakeStoreError`, never as a silent empty read.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from beadhive.modules.intake.contracts import (
    INVESTIGATES_EDGE,
    OUTBOX_KEY,
    BeadView,
    IntakeRefused,
    IntakeStoreError,
    RefusalReason,
    ReportDraft,
    StoreScope,
    WorkDraft,
    blockers_of,
)

from .bead_store import BdReportStore, ReportRoutes, _first_line
from .documents import SOURCE_KEY

#: ``bd``'s own priority grammar: ``0``-``4`` or ``P0``-``P4``. Anything else is left to ``bd``,
#: whose refusal names the grammar, so an invalid value fails exactly as it always did.
_PRIORITY = re.compile(r"^[pP]?([0-4])$")


class ReportApiError(Exception):
    """A Beads service call failed. ``refused`` is true for a typed refusal (the service answered
    and said no); false for anything that leaves the outcome unknown or the service unreachable."""

    def __init__(self, detail: str, *, refused: bool = False, code: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.refused = refused
        #: The service's stable machine-readable reason (``not_found``, ``not_closable`` ...), for
        #: a typed refusal; empty otherwise.
        self.code = code


class ReportApi(Protocol):
    """The Beads service operations the report store uses, as plain ``bd show`` shaped rows.

    Every method raises :class:`ReportApiError` on failure. ``issue`` returns ``None`` for a bead
    that does not exist, ``dependents`` the beads that point at one, ``issues`` EVERY row (the
    implementation pages) in ``bd list``'s priority order."""

    def issue(self, bead: str) -> dict | None: ...

    def dependents(self, bead: str) -> list[dict]: ...

    def issues(
        self, *, label: str = "", status: str = "", has_metadata_key: str = "", all_: bool = False
    ) -> list[dict]: ...

    def update(
        self,
        bead: str,
        *,
        actor: str,
        issue_type: str = "",
        priority: int | None = None,
        assignee: str | None = None,
    ) -> None: ...

    def close(self, bead: str, *, reason: str, actor: str, force: bool = False) -> None: ...

    def link(self, source: str, target: str, type_: str, *, actor: str) -> None: ...

    def unlink(self, source: str, target: str, *, actor: str) -> None: ...

    def set_document(
        self, bead: str, key: str, document: Any, *, current: Any, present: bool, actor: str
    ) -> None: ...


class ApiReportStore(BdReportStore):
    """A :class:`ReportStore` over the Beads service, with ``routes`` for what it cannot serve.

    ``api`` is called whenever the store needs the route and returns the :class:`ReportApi`, or
    ``None`` when the CLI route is selected (it decides once and may raise when the route forbids
    the CLI and no service is there). With ``None`` every method is the parent's."""

    def __init__(
        self,
        routes: ReportRoutes,
        scope: StoreScope | Callable[[], StoreScope],
        *,
        api: Callable[[], ReportApi | None],
        error_line: Callable[[Any], str] = _first_line,
    ) -> None:
        self._select_api = api
        super().__init__(routes, scope, error_line=error_line)
        self._bound: dict[str, set[str]] | None = None

    # -- the route and its failures ------------------------------------------------------------

    def _api(self) -> ReportApi | None:
        return self._select_api()

    @staticmethod
    def _failed(exc: ReportApiError, operation: str, bead: str) -> IntakeStoreError:
        return IntakeStoreError(f"{operation} failed: {exc.detail}", bead=bead, operation=operation)

    # -- reads -------------------------------------------------------------------------------

    def _show(self, bead: str) -> dict | None:
        api = self._api()
        if api is None:
            return super()._show(bead)
        try:
            return api.issue(bead)
        except ReportApiError as exc:
            raise self._failed(exc, "read", bead) from exc

    def _edges(self, bead: str, *, direction: str, type_: str) -> Any:
        api = self._api()
        if api is None:
            return super()._edges(bead, direction=direction, type_=type_)
        try:
            if direction == "up":
                rows = api.dependents(bead)
            else:
                row = api.issue(bead)
                rows = (row or {}).get("dependencies") or []
        except ReportApiError as exc:
            raise self._failed(exc, "read", bead) from exc
        return [
            row for row in rows if isinstance(row, dict) and row.get("dependency_type") == type_
        ]

    def _rows(
        self,
        operation: str,
        *,
        label: str = "",
        status: str = "",
        key: str = "",
        all_: bool = False,
    ) -> tuple[BeadView, ...] | None:
        api = self._api()
        if api is None:
            return None
        try:
            rows = api.issues(label=label, status=status, has_metadata_key=key, all_=all_)
        except ReportApiError as exc:
            raise self._failed(exc, operation, "") from exc
        return self._views(rows)

    def list_open_labeled(self, label: str) -> tuple[BeadView, ...]:
        views = self._rows("list", label=label, status="open")
        return super().list_open_labeled(label) if views is None else views

    def bound_reports(self) -> tuple[BeadView, ...]:
        views = self._rows("list", key=SOURCE_KEY, all_=True)
        return super().bound_reports() if views is None else views

    def reports_with_outbox(self) -> tuple[BeadView, ...]:
        views = self._rows("list", key=OUTBOX_KEY, all_=True)
        return super().reports_with_outbox() if views is None else views

    def find_by_external_ref(self, url: str) -> tuple[BeadView, ...]:
        """The beads bound to ``url``, every status. A hit in the bound-report index is answered
        with each bead's CURRENT row; a miss is confirmed on the CLI route, because the service
        cannot filter on ``external_ref`` and a bead bound by it alone (no ``intake.source``)
        is still a binding. The index is built on first use and dropped by every create, so a
        lookup after a create sees a racing duplicate exactly as the CLI lookup did."""
        api = self._api()
        if api is None or not url:
            return super().find_by_external_ref(url)
        try:
            index = self._bound_index(api)
            found = [row for bead in sorted(index.get(url, ())) if (row := api.issue(bead))]
        except ReportApiError as exc:
            raise self._failed(exc, "find_by_external_ref", "") from exc
        if found:
            return self._views(found)
        return super().find_by_external_ref(url)

    def _bound_index(self, api: ReportApi) -> dict[str, set[str]]:
        if self._bound is None:
            index: dict[str, set[str]] = {}
            for row in api.issues(has_metadata_key=SOURCE_KEY, all_=True):
                ref = str(row.get("external_ref") or "")
                if ref:
                    index.setdefault(ref, set()).add(str(row.get("id", "")))
            self._bound = index
        return self._bound

    # -- writes ------------------------------------------------------------------------------

    def create(self, draft: ReportDraft) -> str:
        """The parent's create (the CLI, see the module docstring), then the lookup index is
        dropped: the next lookup re-reads it, so a racing duplicate of the new bead shows up."""
        self._bound = None
        return super().create(draft)

    def create_work(self, draft: WorkDraft) -> str:
        self._bound = None
        return super().create_work(draft)

    def update_fields(self, bead: str, *, issue_type: str, priority: str, actor: str) -> None:
        api = self._api()
        number = _priority(priority) if priority else None
        if api is None or (priority and number is None):
            return super().update_fields(
                bead, issue_type=issue_type, priority=priority, actor=actor
            )
        if not (issue_type or priority):
            return
        try:
            api.update(bead, actor=actor, issue_type=issue_type, priority=number)
        except ReportApiError as exc:
            raise self._failed(exc, "update_fields", bead) from exc

    def assign(self, bead: str, seat: str, *, actor: str) -> None:
        api = self._api()
        if api is None:
            return super().assign(bead, seat, actor=actor)
        try:
            api.update(bead, actor=actor, assignee=seat)
        except ReportApiError as exc:
            raise self._failed(exc, "assign", bead) from exc

    def track(self, report: str, work: str, *, actor: str) -> None:
        api = self._api()
        if api is None:
            return super().track(report, work, actor=actor)
        try:
            api.link(report, work, "tracks", actor=actor)
        except ReportApiError as exc:
            raise self._failed(exc, "track", report) from exc

    def untrack(self, report: str, work: str, *, actor: str) -> None:
        api = self._api()
        if api is None:
            return super().untrack(report, work, actor=actor)
        try:
            api.unlink(report, work, actor=actor)
        except ReportApiError as exc:
            raise self._failed(exc, "untrack", report) from exc

    def investigate(self, report: str, work: str, *, actor: str) -> None:
        """The ``investigates`` edge runs work -> report, the opposite direction to ``tracks``."""
        api = self._api()
        if api is None:
            return super().investigate(report, work, actor=actor)
        try:
            api.link(work, report, INVESTIGATES_EDGE, actor=actor)
        except ReportApiError as exc:
            raise self._failed(exc, "investigate", report) from exc

    def uninvestigate(self, report: str, work: str, *, actor: str) -> None:
        api = self._api()
        if api is None:
            return super().uninvestigate(report, work, actor=actor)
        if work not in self.investigations_of(report):
            return  # the service removes a (source, target) pair whatever its type
        try:
            api.unlink(work, report, actor=actor)
        except ReportApiError as exc:
            raise self._failed(exc, "uninvestigate", report) from exc

    def _set_document(
        self, bead: str, key: str, document: Any, *, row: dict, actor: str, operation: str
    ) -> None:
        api = self._api()
        if api is None:
            return super()._set_document(
                bead, key, document, row=row, actor=actor, operation=operation
            )
        metadata = row.get("metadata")
        current = metadata.get(key) if isinstance(metadata, Mapping) else None
        present = isinstance(metadata, Mapping) and key in metadata
        try:
            api.set_document(bead, key, document, current=current, present=present, actor=actor)
        except ReportApiError as exc:
            raise self._failed(exc, operation, bead) from exc

    def close(self, bead: str, *, reason: str, actor: str) -> None:
        """The parent's held close over the service: the plain close first (a bead with no hold
        closes, and fails, as it always did); only a REFUSAL by the service reads the blockers,
        and a transport failure is reported as it is. The forced close is the service's own
        ``force`` (the live-blocker refusal and nothing else), then each hold is resolved."""
        api = self._api()
        if api is None:
            return super().close(bead, reason=reason, actor=actor)
        try:
            api.close(bead, reason=reason, actor=actor)
            return
        except ReportApiError as exc:
            if not exc.refused:
                raise self._failed(exc, "close", bead) from exc
            refusal = exc
        row = self._show(bead)
        blockers = blockers_of(row) if row is not None else None
        if blockers is None or not blockers.holds:
            raise self._failed(refusal, "close", bead) from refusal
        if blockers.others:
            raise IntakeRefused(
                RefusalReason.LIVE_BLOCKER,
                f"{bead} is blocked by {', '.join(blockers.others)} besides its intake-hold — "
                "resolve those before closing the report",
            )
        if not blockers.closed:
            try:
                api.close(bead, reason=reason, actor=actor, force=True)
            except ReportApiError as exc:
                raise self._failed(exc, "close", bead) from exc
        for gate in blockers.holds:  # bookkeeping: a closed bead is never ready
            self.resolve_hold(gate, actor=actor)

    # -- unchanged by the route ----------------------------------------------------------------
    #
    # ``holds`` (the gate list stays on the CLI, where it is the faster read, then each gate's
    # dependents over the API), ``is_held`` / ``tracked_work`` / ``trackers_of`` /
    # ``investigations_of`` (all through ``_edges``), ``get`` / ``read_thread`` / ``read_outbox`` /
    # ``write_thread`` / ``write_outbox`` / ``record_snapshot`` (through ``_show`` and
    # ``_set_document``),
    # ``set_state`` / ``open_hold`` / ``resolve_hold`` (CLI only) and ``duplicate_pairs`` (no API).


def _priority(text: str) -> int | None:
    """``bd``'s priority grammar as the service's integer, or ``None`` for anything else."""
    found = _PRIORITY.match(text.strip())
    return int(found.group(1)) if found else None


__all__ = ["ApiReportStore", "ReportApi", "ReportApiError"]
