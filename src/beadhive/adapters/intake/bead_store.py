"""``BdReportStore``: the intake module's :class:`ReportStore` over ``bd``.

It speaks only named ``beadhive_bd_cli`` routes (injected, so this adapter never imports the
package or a bd argv builder) and is bound to ONE operational store by the composition root,
never to a public one. Every failed write raises :class:`IntakeStoreError` carrying the failed
operation, the bead and bd's exit status; a read that bd cannot answer degrades to the empty
result the legacy callers always got (None / no rows).

``intake.source`` (the binding facts and last issue snapshot) is a flat top-level metadata key
rewritten whole; see :mod:`beadhive.adapters.intake.documents`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from beadhive.modules.intake.contracts import (
    BeadView,
    DuplicatePair,
    IntakeStoreError,
    ReportDraft,
    SourceSnapshot,
    StoreScope,
)

from .documents import SOURCE_KEY, bead_view, current_source, source_document


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
        data = self._routes.issue_show_raw(bead)
        if isinstance(data, list):
            data = data[0] if data else None
        return bead_view(data) if isinstance(data, dict) else None

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

    def tracked_work(self, report: str) -> str | None:
        ids = self._edge_ids(report, direction="down")
        return ids[0] if ids else None

    def trackers_of(self, work: str) -> tuple[str, ...]:
        return self._edge_ids(work, direction="up")

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
        return str(created)

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        result = self._routes.issue_set_state(
            bead, f"{dimension}={value}", reason=reason, actor=actor, capture=True
        )
        self._check(result, "set_state", bead)

    def record_snapshot(self, bead: str, snapshot: SourceSnapshot, *, actor: str) -> None:
        """Rewrite the whole ``intake.source`` document with ``snapshot``, keeping its binding."""
        data = self._routes.issue_show_raw(bead)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
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
        result = self._routes.issue_close(bead, reason=reason, actor=actor, capture=True)
        self._check(result, "close", bead, prefix="bd close failed: ")

    def assign(self, bead: str, seat: str, *, actor: str) -> None:
        result = self._routes.issue_assign(bead, seat, actor=actor, capture=True)
        self._check(result, "assign", bead)

    def track(self, report: str, work: str, *, actor: str) -> None:
        result = self._routes.dependency_add(report, work, "tracks", actor=actor)
        self._check(result, "track", report)

    def untrack(self, report: str, work: str, *, actor: str) -> None:
        result = self._routes.dependency_remove(report, work, actor=actor)
        self._check(result, "untrack", report)

    # -- helpers -----------------------------------------------------------------------------

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
