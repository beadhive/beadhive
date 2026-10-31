"""``union_merge`` and tombstone application (format ADR sections 6 and 10).

Every combining rule is a semilattice join, so the merge is commutative, associative and
idempotent:

- events union by id; equal ids must carry byte-identical identity projections (members
  withheld by redaction excepted), otherwise the merge fails rather than pick one;
- ``extracted_at`` minimum, ``confidence`` maximum, ``source.evidence`` set union sorted by
  JCS bytes, ``annotations`` per key the value with the smallest JCS bytes;
- redaction: the result keeps every member present in either copy and lists as ``redacted``
  only pointers still withheld after that union (so an unredacted copy supersedes);
- tombstones are absorbing: every erased event is dropped from the result.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ._ids import identity_projection
from ._json import canonical_bytes
from ._model import BeadEvent, BeadRef, EventKind
from ._order import sort_events
from ._time import instant


class IdCollisionError(ValueError):
    """Two events share an id but not their identity material; a merge never picks one."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"id collision: two events named {event_id} differ in identity material")
        self.event_id = event_id


_MISSING = object()


def _pointer_tokens(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON Pointer: {pointer!r}")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def _pointer_get(document: Any, pointer: str) -> Any:
    node = document
    for token in _pointer_tokens(pointer):
        if isinstance(node, Mapping) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return _MISSING
    return node


def _pointer_set(document: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = _pointer_tokens(pointer)
    node: Any = document
    for token in tokens[:-1]:
        if isinstance(node, list):
            node = node[int(token)]
        else:
            node = node.setdefault(token, {})
    if isinstance(node, list):
        node[int(tokens[-1])] = value
    else:
        node[tokens[-1]] = value


def _pointer_remove(document: Any, pointer: str) -> None:
    *parents, last = _pointer_tokens(pointer)
    node = document
    for token in parents:
        if not isinstance(node, dict) or token not in node:
            return
        node = node[token]
    if isinstance(node, dict):
        node.pop(last, None)


def _identity_without(raw: Mapping[str, Any], hidden: Iterable[str]) -> bytes:
    projection = identity_projection(raw)
    for pointer in sorted(hidden, key=len, reverse=True):
        _pointer_remove(projection, pointer)
    return canonical_bytes(projection)


def _combine(left: BeadEvent, right: BeadEvent) -> BeadEvent:
    """Join two copies of one event id."""
    a, b = left.to_json(), right.to_json()
    withheld_a, withheld_b = set(left.redacted or ()), set(right.redacted or ())
    hidden = withheld_a | withheld_b
    if _identity_without(a, hidden) != _identity_without(b, hidden):
        raise IdCollisionError(left.id)

    merged = a
    for pointer in sorted(withheld_a, key=len):
        value = _pointer_get(b, pointer)
        if value is not _MISSING and _pointer_get(merged, pointer) is _MISSING:
            _pointer_set(merged, pointer, value)
    still_withheld = sorted(p for p in hidden if _pointer_get(merged, p) is _MISSING)
    if still_withheld:
        merged["redacted"] = still_withheld
    else:
        merged.pop("redacted", None)

    merged["extracted_at"] = min(a["extracted_at"], b["extracted_at"], key=instant)

    confidences = [c for c in (a.get("confidence"), b.get("confidence")) if c is not None]
    if confidences:
        merged["confidence"] = max(confidences)

    evidence = {
        canonical_bytes(item): item
        for item in (*a["source"].get("evidence", ()), *b["source"].get("evidence", ()))
    }
    if evidence:
        merged["source"]["evidence"] = [evidence[key] for key in sorted(evidence)]

    annotations: dict[str, Any] = {}
    for copy in (a.get("annotations") or {}, b.get("annotations") or {}):
        for key, value in copy.items():
            if key not in annotations or canonical_bytes(value) < canonical_bytes(annotations[key]):
                annotations[key] = value
    if annotations:
        merged["annotations"] = annotations
    return BeadEvent.from_json(merged)


def _normalize(event: BeadEvent) -> BeadEvent:
    """One copy joined with itself: evidence de-duplicated and sorted (idempotence)."""
    if len(event.source.evidence) < 2:
        return event
    return _combine(event, event)


def erasure(events: Iterable[BeadEvent]) -> tuple[frozenset[str], frozenset[BeadRef]]:
    """Ids erased by ``scope: events`` tombstones and beads erased by ``scope: bead`` ones."""
    erased_ids: set[str] = set()
    erased_beads: set[BeadRef] = set()
    for event in events:
        if event.kind is not EventKind.TOMBSTONE:
            continue
        if event.data.get("scope") == "bead":
            erased_beads.add(event.bead)
        else:
            erased_ids.update(event.data.get("events") or ())
    return frozenset(erased_ids), frozenset(erased_beads)


def apply_tombstones(events: Iterable[BeadEvent]) -> tuple[BeadEvent, ...]:
    """Drop every erased event. Tombstones themselves are never erased (they are absorbing)."""
    events = tuple(events)
    erased_ids, erased_beads = erasure(events)
    return tuple(
        event
        for event in events
        if event.kind is EventKind.TOMBSTONE
        or (event.id not in erased_ids and event.bead not in erased_beads)
    )


def union_merge(logs: Iterable[Iterable[BeadEvent]]) -> tuple[BeadEvent, ...]:
    """Merge event logs into one, in archive part order (format ADR section 6).

    Raises :class:`IdCollisionError` when two events share an id but not their identity
    material.
    """
    by_id: dict[str, BeadEvent] = {}
    for log in logs:
        for event in log:
            held = by_id.get(event.id)
            by_id[event.id] = _normalize(event) if held is None else _combine(held, event)
    return sort_events(apply_tombstones(by_id.values()))
