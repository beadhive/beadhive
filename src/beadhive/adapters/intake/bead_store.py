"""``BdReportStore``: the intake module's :class:`ReportStore` over ``bd``.

It speaks only named ``beadhive_bd_cli`` routes (injected, so this adapter never imports the
package or a bd argv builder) and is bound to ONE operational store by the composition root,
never to a public one. Every failed write raises :class:`IntakeStoreError` carrying the failed
operation, the bead and bd's exit status; a read that bd cannot answer degrades to the empty
result the legacy callers always got (None / no rows).

``intake.source`` (the binding facts and last issue snapshot) is a flat top-level metadata key
rewritten whole; see :mod:`beadhive.adapters.intake.documents`. ``intake.thread`` (the
conversation) is another such key: :meth:`BdReportStore.write_thread` re-reads the stored ``seq``
(bd has no compare-and-swap) and then rewrites the whole document in ONE ``bd update --metadata``,
so the records and the thread checkpoint always commit together. Only :meth:`read_thread` and
:meth:`write_thread` decode it; the queue and lookup reads never do.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from beadhive.modules.intake.contracts import (
    INTAKE_HOLD_GATE_TYPE,
    INTAKE_HOLD_REASON,
    THREAD_KEY,
    BeadView,
    DuplicatePair,
    HoldGate,
    IntakeRefused,
    IntakeStoreError,
    RefusalReason,
    ReportDraft,
    SourceSnapshot,
    StoreScope,
    Thread,
    ThreadDocumentError,
    blockers_of,
    is_hold_gate,
    thread_from_document,
    thread_to_document,
)

from .documents import SOURCE_KEY, bead_view, current_source, source_document, stored_thread


class ReportRoutes(Protocol):
    """The ``beadhive_bd_cli.CliRoutes`` methods this store uses (structural, injected)."""

    def create_report(
        self,
        title: str,
        issue_type: str,
        labels: str,
        *,
        description: str = ...,
        actor: str = ...,
        external_ref: str = ...,
        metadata: Any = ...,
    ) -> Any: ...

    def issue_list(self, **kwargs: Any) -> Any: ...

    def issue_show_raw(self, bead: str) -> Any: ...

    def issue_set_state(
        self, bead: str, state: str, *, reason: str, actor: str = ..., capture: bool = ...
    ) -> Any: ...

    def issue_update_fields(
        self,
        bead: str,
        *,
        issue_type: str = ...,
        priority: str = ...,
        actor: str = ...,
        capture: bool = ...,
    ) -> Any: ...

    def issue_assign(
        self, bead: str, assignee: str, *, actor: str = ..., capture: bool = ...
    ) -> Any: ...

    def issue_close(
        self, beads: Any, *, reason: str, actor: str = ..., force: bool = ..., capture: bool = ...
    ) -> Any: ...

    def issue_close_held(
        self, bead: str, *, reason: str, actor: str = ..., capture: bool = ...
    ) -> Any: ...

    def gate_create(
        self, bead: str, gate_type: str, reason: str = ..., *, actor: str = ..., capture: bool = ...
    ) -> Any: ...

    def gate_list(self, *, include_resolved: bool = ..., strict: bool = ...) -> Any: ...

    def gate_resolve(self, gate_id: str, *, reason: str = ..., actor: str = ...) -> Any: ...

    def issue_set_metadata_document(
        self, bead: str, key: str, document: Any, *, actor: str = ..., capture: bool = ...
    ) -> Any: ...

    def dependency_add(self, source: str, target: str, type_: str, *, actor: str = ...) -> Any: ...

    def dependency_remove(self, source: str, target: str, *, actor: str = ...) -> Any: ...

    def dependency_list(self, bead: str, *, direction: str, type_: str) -> Any: ...

    def find_duplicates(self, *, threshold: float, method: str) -> Any: ...


def _first_line(result: Any) -> str:
    """Fallback failure line: the first non-blank line bd printed, else the exit status."""
    for line in (f"{result.stdout or ''}\n{result.stderr or ''}").splitlines():
        if line.strip():
            return line.strip()
    return f"exit {result.returncode}"


class BdReportStore:
    """A :class:`ReportStore` over ``routes``, bound to the store ``scope`` names.

    ``scope`` may be a :class:`StoreScope` or a zero-argument callable returning one, resolved on
    first use: a caller that only lists or disposes never needs the hive's registry entry.
    ``error_line`` renders a failed bd result as one line (the composition root passes bd's own
    ``err_line``)."""

    def __init__(
        self,
        routes: ReportRoutes,
        scope: StoreScope | Callable[[], StoreScope],
        *,
        error_line: Callable[[Any], str] = _first_line,
    ) -> None:
        self._routes = routes
        self._scope = scope
        self._error_line = error_line

    # -- reads -------------------------------------------------------------------------------

    def scope(self) -> StoreScope:
        if callable(self._scope):
            self._scope = self._scope()
        return self._scope  # type: ignore[return-value]

    def get(self, bead: str) -> BeadView | None:
        data = self._show(bead)
        return bead_view(data) if data is not None else None

    def find_by_external_ref(self, url: str) -> tuple[BeadView, ...]:
        if not url:
            return ()
        rows = self._routes.issue_list(external_ref=url, all_=True, limit=0)
        return self._views(rows)

    def list_open_labeled(self, label: str) -> tuple[BeadView, ...]:
        rows = self._routes.issue_list(label=label, status="open", label_first=True)
        return self._views(rows)

    def duplicate_pairs(
        self, *, threshold: float, method: str = "mechanical"
    ) -> tuple[DuplicatePair, ...]:
        data = self._routes.find_duplicates(threshold=threshold, method=method)
        pairs = data.get("pairs") if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            return ()
        return tuple(
            DuplicatePair(
                str(p.get("issue_a_id", "")),
                str(p.get("issue_b_id", "")),
                float(p.get("similarity") or 0.0),
                raw=dict(p),
            )
            for p in pairs
            if isinstance(p, dict)
        )

    def holds(self) -> tuple[HoldGate, ...]:
        """Every open ``intake-hold:`` gate and the beads it blocks, read from the gate's own
        dependents (``bd dep list <gate> --direction up``), so a gate whose edge is gone reads as
        blocking nothing."""
        rows = self._routes.gate_list()
        if not isinstance(rows, list):
            return ()
        holds: list[HoldGate] = []
        for row in rows:
            if not (isinstance(row, dict) and row.get("status") == "open" and is_hold_gate(row)):
                continue
            gate = str(row.get("id", ""))
            edges = self._routes.dependency_list(gate, direction="up", type_="blocks")
            blocked = (
                tuple(sorted(str(r.get("id", "")) for r in edges if isinstance(r, dict)))
                if isinstance(edges, list)
                else ()
            )
            holds.append(HoldGate(gate, blocked))
        return tuple(holds)

    def bound_reports(self) -> tuple[BeadView, ...]:
        rows = self._routes.issue_list(has_metadata_key=SOURCE_KEY, all_=True, limit=0)
        return self._views(rows)

    def tracked_work(self, report: str) -> str | None:
        ids = self._edge_ids(report, direction="down")
        return ids[0] if ids else None

    def trackers_of(self, work: str) -> tuple[str, ...]:
        return self._edge_ids(work, direction="up")

    def read_thread(self, bead: str) -> Thread | None:
        """The bead's ``intake.thread`` (one ``bd show``); None for no bead or no document."""
        row = self._show(bead)
        return self._thread(bead, row) if row is not None else None

    # -- writes ------------------------------------------------------------------------------

    def create(self, draft: ReportDraft) -> str:
        """One ``bd create``: the hive triplet labels, the type, the description and, for a bound
        report, the issue URL as ``external_ref`` plus the ``intake.source`` document, so the
        binding exists atomically with the bead. ``origin`` and ``intake`` are stamped by
        :meth:`set_state`, not here."""
        binding = draft.binding
        metadata = None
        if binding is not None:
            metadata = {SOURCE_KEY: source_document(binding, draft.snapshot)}
        result = self._routes.create_report(
            draft.title,
            draft.issue_type,
            ",".join(self.scope().hive.labels()),
            description=draft.description,
            actor=draft.actor,
            external_ref=binding.url if binding is not None else "",
            metadata=metadata,
        )
        if result.returncode:
            raise IntakeStoreError(
                f"bd create failed: {self._error_line(result)}",
                operation="create",
                returncode=result.returncode,
            )
        try:
            created = (json.loads(result.stdout or "{}") or {}).get("id") or ""
        except (json.JSONDecodeError, AttributeError):
            created = ""
        if not created:
            raise IntakeStoreError("bd create reported no new bead id", operation="create")
        if binding is not None:
            # a bound report is never work: hold it at once, before any state stamp. Not atomic
            # with the create (bd has no transaction across the two); a failure here leaves the
            # report open and unheld, carries its id, and the `bh doctor` hold sweep finds it.
            self.open_hold(str(created), actor=draft.actor)
        return str(created)

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        result = self._routes.issue_set_state(
            bead, f"{dimension}={value}", reason=reason, actor=actor, capture=True
        )
        self._check(result, "set_state", bead)

    def record_snapshot(self, bead: str, snapshot: SourceSnapshot, *, actor: str) -> None:
        """Rewrite the whole ``intake.source`` document with ``snapshot``, keeping its binding."""
        data = self._show(bead)
        if data is None:
            raise IntakeStoreError(f"{bead} not found", bead=bead, operation="record_snapshot")
        document = current_source(data)
        document.update(source_document(None, snapshot))
        result = self._routes.issue_set_metadata_document(
            bead, SOURCE_KEY, document, actor=actor, capture=True
        )
        self._check(result, "record_snapshot", bead)

    def update_fields(self, bead: str, *, issue_type: str, priority: str, actor: str) -> None:
        if not (issue_type or priority):
            return
        result = self._routes.issue_update_fields(
            bead, issue_type=issue_type, priority=priority, actor=actor, capture=True
        )
        self._check(result, "update_fields", bead, prefix="bd update failed: ")

    def close(self, bead: str, *, reason: str, actor: str) -> None:
        """Close ``bead``; a bound report's lifetime hold makes it the held close.

        The ordinary ``bd close`` goes first, so a bead with no hold closes (and fails) exactly as
        it always did. Only a refusal reads the bead's blockers: when every live one is its own
        ``intake-hold:`` gate this closes it with ``bd close --force`` (overriding only that
        refusal) and THEN resolves the hold, so the report is never open and unblocked (resolving
        first re-admits it to ``bd ready``; bh-6wj9y E4). A report with any other live blocker is
        refused, and a failure with no hold in play is the ordinary close failure."""
        result = self._routes.issue_close(bead, reason=reason, actor=actor, capture=True)
        if not result.returncode:
            return
        row = self._show(bead)
        blockers = blockers_of(row) if row is not None else None
        if blockers is None or not blockers.holds:
            self._check(result, "close", bead, prefix="bd close failed: ")
            return
        if blockers.others:
            raise IntakeRefused(
                RefusalReason.LIVE_BLOCKER,
                f"{bead} is blocked by {', '.join(blockers.others)} besides its intake-hold — "
                "resolve those before closing the report",
            )
        if not blockers.closed:
            forced = self._routes.issue_close_held(bead, reason=reason, actor=actor, capture=True)
            self._check(forced, "close", bead, prefix="bd close failed: ")
        for gate in blockers.holds:  # bookkeeping: a closed bead is never ready
            self.resolve_hold(gate, actor=actor)

    def open_hold(self, report: str, *, actor: str) -> None:
        result = self._routes.gate_create(
            report, INTAKE_HOLD_GATE_TYPE, INTAKE_HOLD_REASON, actor=actor, capture=True
        )
        self._check(result, "open_hold", report, prefix="bd gate create failed: ")

    def resolve_hold(self, gate: str, *, actor: str) -> None:
        result = self._routes.gate_resolve(gate, reason="report closed", actor=actor)
        self._check(result, "resolve_hold", gate, prefix="bd gate resolve failed: ")

    def assign(self, bead: str, seat: str, *, actor: str) -> None:
        result = self._routes.issue_assign(bead, seat, actor=actor, capture=True)
        self._check(result, "assign", bead)

    def track(self, report: str, work: str, *, actor: str) -> None:
        result = self._routes.dependency_add(report, work, "tracks", actor=actor)
        self._check(result, "track", report)

    def untrack(self, report: str, work: str, *, actor: str) -> None:
        result = self._routes.dependency_remove(report, work, actor=actor)
        self._check(result, "untrack", report)

    def write_thread(self, bead: str, thread: Thread, *, expect_seq: int, actor: str) -> None:
        """Re-read the stored ``seq``, then rewrite ``intake.thread`` whole in one ``bd update``.

        The re-read is the record half of the ``IntakeLock`` (one host and one process are the
        lock's): a document that moved since the caller read it is refused, writing nothing."""
        row = self._show(bead)
        if row is None:
            raise IntakeStoreError(f"{bead} not found", bead=bead, operation="write_thread")
        stored = self._thread(bead, row)
        current = stored.seq if stored is not None else 0
        if current != expect_seq:
            raise IntakeRefused(
                RefusalReason.THREAD_MOVED,
                f"{bead}'s thread is at seq {current}, not {expect_seq}; re-read and retry",
            )
        if thread.seq <= expect_seq:
            raise IntakeStoreError(
                f"a thread write must advance seq past {expect_seq}",
                bead=bead,
                operation="write_thread",
            )
        if str(row.get("external_ref") or "") != thread.issue.url:
            raise IntakeStoreError(
                f"{bead} is not bound to {thread.issue.url}", bead=bead, operation="write_thread"
            )
        result = self._routes.issue_set_metadata_document(
            bead, THREAD_KEY, thread_to_document(thread), actor=actor, capture=True
        )
        self._check(result, "write_thread", bead)

    # -- helpers -----------------------------------------------------------------------------

    def _show(self, bead: str) -> dict | None:
        data = self._routes.issue_show_raw(bead)
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _thread(bead: str, row: dict) -> Thread | None:
        document = stored_thread(row)
        if document is None:
            return None
        try:
            return thread_from_document(document)
        except ThreadDocumentError as exc:  # never read as empty: the next write would replace it
            raise IntakeStoreError(str(exc), bead=bead, operation="read_thread") from exc

    def _check(self, result: Any, operation: str, bead: str, *, prefix: str = "") -> None:
        if result.returncode:
            raise IntakeStoreError(
                f"{prefix}{self._error_line(result)}",
                bead=bead,
                operation=operation,
                returncode=result.returncode,
            )

    @staticmethod
    def _views(rows: Any) -> tuple[BeadView, ...]:
        if not isinstance(rows, list):
            return ()
        return tuple(bead_view(row) for row in rows if isinstance(row, dict))

    def _edge_ids(self, bead: str, *, direction: str) -> tuple[str, ...]:
        rows = self._routes.dependency_list(bead, direction=direction, type_="tracks")
        if not isinstance(rows, list):
            return ()
        return tuple(sorted(str(r.get("id", "")) for r in rows if isinstance(r, dict)))


__all__ = ["BdReportStore", "ReportRoutes"]
