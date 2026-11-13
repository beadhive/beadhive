"""Rollback inverses per kind (format ADR section 5, "Inverse (rollback)" column).

An inverse restores the facet an applied event touched to its folded value just before the
event. Undoing is fix-forward: a ``create`` is undone by a ``retire`` with reason
:data:`ROLLBACK_REASON`, never by a delete. ``comment`` and ``tombstone`` have no inverse by
design (removing a comment is erasure; erasure is irreversible); the envelope reports them as
rollback residue.

Inverses are dated by a :class:`Stamper`: strictly after every dated event involved and after
the injected clock, strictly increasing, so in a fold-based store each inverse wins last-writer
resolution over the event it undoes. An undated applied event sorts after every dated one by
definition, so no dated inverse can supersede it in such a store; imperative stores (bd) are
unaffected since they apply in call order.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from beadlog import (
    BeadEvent,
    BeadState,
    EventKind,
    Provenance,
    format_timestamp,
    instant,
    mint_event,
)
from beadlog_sources import ROLLBACK_REASON

ROLLBACK_SYSTEM = "beadhive-backfill"
_NO_INVERSE = frozenset({EventKind.COMMENT, EventKind.TOMBSTONE})


def _parse(value: str) -> datetime:
    """A canonical timestamp as an aware datetime, truncated to microseconds."""
    moment = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    fraction = value[20:-1] if value[19] == "." else ""
    return moment + timedelta(microseconds=int(fraction[:6].ljust(6, "0")) if fraction else 0)


class Stamper:
    """Strictly increasing timestamps, each later than ``now`` and every dated event given."""

    def __init__(self, now: str, events: Iterable[BeadEvent]) -> None:
        dated = [
            stamp
            for event in events
            for stamp in (event.occurred_at, event.recorded_at)
            if stamp is not None
        ]
        latest = max(dated, key=instant, default=None)
        base = _parse(now)
        if latest is not None and instant(format_timestamp(base)) <= instant(latest):
            base = _parse(latest[:19] + "Z") + timedelta(seconds=1)
        self._next = base

    def __call__(self) -> str:
        stamp = format_timestamp(self._next)
        self._next += timedelta(microseconds=1)
        return stamp


def has_inverse(event: BeadEvent) -> bool:
    """False for kinds that cannot be undone (``comment``, ``tombstone``)."""
    return event.kind not in _NO_INVERSE


def inverse_of(
    event: BeadEvent, before: BeadState | None, *, stamp: Stamper, scope: str
) -> tuple[BeadEvent, ...] | None:
    """The events that restore what ``event`` changed, given the bead's fold ``before`` it.

    Returns ``None`` when the kind has no inverse by design. Set-element and scalar inverses
    re-assert the prior value, so an inverse of an event that changed nothing is a state no-op
    (the envelope skips those against a fresh read); ``()`` means nothing to restore at all.
    ``scope`` namespaces the inverse events' source refs (the plan hash), so each inverse has
    a stable, distinct id.
    """
    if not has_inverse(event):
        return None
    out: list[BeadEvent] = []

    def emit(kind: EventKind, data: dict[str, Any]) -> None:
        at = stamp()
        out.append(
            mint_event(
                str(kind),
                event.bead,
                data,
                occurred_at=at,
                recorded_at=None,
                extracted_at=at,
                source=(ROLLBACK_SYSTEM, f"rollback/{scope}/{event.id}/{len(out)}"),
                provenance=str(Provenance.DERIVED),
            )
        )

    existed = before is not None and before.exists
    kind, data = event.kind, event.data
    if kind in (EventKind.CREATE, EventKind.CHECKPOINT):
        if not existed:
            emit(EventKind.RETIRE, {"reason": ROLLBACK_REASON})
        elif kind is EventKind.CREATE:
            assert before is not None
            if before.type != data.get("type"):
                emit(EventKind.SET, {"field": "type", "value": before.type})
            for name in sorted(data.get("fields") or {}):
                if before.fields.get(name) != data["fields"][name]:
                    emit(EventKind.SET, {"field": name, "value": _thawed(before.fields.get(name))})
            _restore_phase(before, emit)
    elif kind is EventKind.SET:
        name = data["field"]
        held = before.type if (name == "type" and before) else _field(before, name)
        emit(EventKind.SET, {"field": name, "value": held})
    elif kind in (EventKind.LABEL_ADD, EventKind.LABEL_REMOVE):
        present = before is not None and data["label"] in before.labels
        emit(EventKind.LABEL_ADD if present else EventKind.LABEL_REMOVE, {"label": data["label"]})
    elif kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
        payload = {"type": data["type"], "target": _thawed(data["target"])}
        present = before is not None and any(edge.to_json() == payload for edge in before.edges)
        emit(EventKind.EDGE_ADD if present else EventKind.EDGE_REMOVE, payload)
    elif kind in (EventKind.PHASE, EventKind.RETIRE):
        if before is not None and before.phase is not None:
            _restore_phase(before, emit, force=True)
    elif kind is EventKind.DELETE:
        if existed:
            assert before is not None
            emit(EventKind.CREATE, {"type": before.type, "fields": _thawed(before.fields)})
            _restore_phase(before, emit)
            for label in before.labels:
                emit(EventKind.LABEL_ADD, {"label": label})
            for edge in before.edges:
                emit(EventKind.EDGE_ADD, edge.to_json())
    return tuple(out)


def _restore_phase(before: BeadState, emit: Any, *, force: bool = False) -> None:
    if before.phase == "retired":
        emit(EventKind.RETIRE, _thawed(before.retired or {}))
    elif before.phase is not None and (force or before.phase != "open"):
        emit(EventKind.PHASE, {"phase": before.phase, "reason": ROLLBACK_REASON})


def _field(before: BeadState | None, name: str) -> Any:
    return None if before is None else _thawed(before.fields.get(name))


def _thawed(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _thawed(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thawed(item) for item in value]
    return value
