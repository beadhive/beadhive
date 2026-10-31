"""``fold``: the state of each bead at a cut (format ADR section 6).

Every facet resolves last-writer-wins on the ordering key: events are applied in key order and
the latest event touching a facet decides it. Facets are independent, so an ``edge_add`` that
sorts before its bead's ``create`` still folds. Tombstones apply to every cut.

Facets: existence (``create`` / ``delete``); ``type`` (``create``, or ``set`` of the core field
``type``); ``phase`` (``create`` -> ``open``, ``phase``, ``retire`` -> ``retired`` plus its
details); one per ``fields.<name>``; one per label; one per edge ``(type, target)``. Comments
append.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ._json import canonical_bytes, freeze, thaw
from ._merge import erasure, union_merge
from ._model import BeadEvent, BeadRef, Edge, EventKind
from ._order import OrderKey, order_key, ordering_key
from ._time import instant

_EMPTY: Mapping[str, Any] = MappingProxyType({})


def _edge_sort_key(edge: Edge) -> bytes:
    return canonical_bytes(edge.to_json())


@dataclass(frozen=True)
class Comment:
    """A folded comment: its event id (the comment's identity), author, body and times.
    ``body`` is ``None`` when a redaction withheld it."""

    id: str
    actor: str | None
    body: str | None
    occurred_at: str | None
    recorded_at: str | None


@dataclass(frozen=True)
class BeadState:
    """The folded state of one bead at a cut.

    ``exists`` / ``deleted`` reflect the latest ``create`` / ``delete`` (both false when the cut
    holds neither). ``erased`` means a ``scope: bead`` tombstone erased the bead: it keeps its
    identity and nothing else. ``fields`` omits null values; ``labels`` are sorted and
    ``edges`` sorted by the JCS bytes of each edge; ``retired`` holds the ``retire`` details
    while the phase is ``retired``.
    """

    bead: BeadRef
    exists: bool = False
    deleted: bool = False
    erased: bool = False
    type: str | None = None
    phase: str | None = None
    fields: Mapping[str, Any] = field(default=_EMPTY, hash=False)
    labels: tuple[str, ...] = ()
    edges: tuple[Edge, ...] = ()
    retired: Mapping[str, Any] | None = field(default=None, hash=False)
    comments: tuple[Comment, ...] = ()

    def to_checkpoint_state(self) -> dict[str, Any]:
        """The ``checkpoint`` kind's ``data.state`` object for this state (format ADR section 5)."""
        if self.type is None or self.phase is None:
            raise ValueError(f"{self.bead} has no type or phase to checkpoint")
        state: dict[str, Any] = {
            "type": self.type,
            "phase": self.phase,
            "fields": thaw(self.fields),
            "labels": list(self.labels),
            "edges": [edge.to_json() for edge in self.edges],
            "deleted": self.deleted,
        }
        if self.retired is not None:
            state["retired"] = thaw(self.retired)
        return state


# The data members each kind needs to assign its facet (a comment body may be withheld).
_NEEDED = {
    EventKind.CREATE: frozenset({"type", "fields"}),
    EventKind.SET: frozenset({"field", "value"}),
    EventKind.LABEL_ADD: frozenset({"label"}),
    EventKind.LABEL_REMOVE: frozenset({"label"}),
    EventKind.EDGE_ADD: frozenset({"type", "target"}),
    EventKind.EDGE_REMOVE: frozenset({"type", "target"}),
    EventKind.PHASE: frozenset({"phase"}),
}


class _Builder:
    def __init__(self, bead: BeadRef) -> None:
        self.bead = bead
        self.existence: bool | None = None  # True: latest is create; False: latest is delete
        self.type: str | None = None
        self.phase: str | None = None
        self.retired: Mapping[str, Any] | None = None
        self.fields: dict[str, Any] = {}
        self.labels: dict[str, bool] = {}
        self.edges: dict[Edge, bool] = {}
        self.comments: list[Comment] = []

    def seed(self, checkpoint: BeadEvent) -> None:
        state = checkpoint.data["state"]
        self.existence = not state.get("deleted", False)
        self.type = state.get("type")
        self.phase, self.retired = state.get("phase"), state.get("retired")
        self.fields.update(state.get("fields") or {})
        self.labels.update({label: True for label in state.get("labels") or ()})
        self.edges.update({Edge.from_json(edge): True for edge in state.get("edges") or ()})

    def apply(self, event: BeadEvent) -> None:
        kind, data = event.kind, event.data
        if event.redacted and not _NEEDED.get(kind, frozenset()) <= set(data):
            return  # a redaction withheld what this change assigns: the facet stays unknown
        if kind is EventKind.CREATE:
            self.existence = True
            self.type = data.get("type")
            self.phase, self.retired = "open", None
            self.fields.update(data.get("fields") or {})
        elif kind is EventKind.SET:
            if data["field"] == "type":
                self.type = data["value"]
            else:
                self.fields[data["field"]] = data["value"]
        elif kind in (EventKind.LABEL_ADD, EventKind.LABEL_REMOVE):
            self.labels[data["label"]] = kind is EventKind.LABEL_ADD
        elif kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
            self.edges[Edge.from_json(data)] = kind is EventKind.EDGE_ADD
        elif kind is EventKind.COMMENT:
            self.comments.append(
                Comment(
                    event.id, event.actor, data.get("body"), event.occurred_at, event.recorded_at
                )
            )
        elif kind is EventKind.PHASE:
            self.phase, self.retired = data["phase"], None
        elif kind is EventKind.RETIRE:
            self.phase, self.retired = "retired", data
        elif kind is EventKind.DELETE:
            self.existence = False
        # tombstone: applied before folding; checkpoint: an assertion unless it seeds

    def build(self) -> BeadState:
        return BeadState(
            bead=self.bead,
            exists=self.existence is True,
            deleted=self.existence is False,
            type=self.type,
            phase=self.phase,
            fields=freeze(
                {name: value for name, value in sorted(self.fields.items()) if value is not None}
            ),
            labels=tuple(sorted(label for label, present in self.labels.items() if present)),
            edges=tuple(
                sorted(
                    (edge for edge, present in self.edges.items() if present), key=_edge_sort_key
                )
            ),
            retired=freeze(self.retired) if self.retired is not None else None,
            comments=tuple(self.comments),
        )


def checkpoint_key(checkpoint: BeadEvent) -> OrderKey:
    """The ordering key of the event a checkpoint ``covers_through``."""
    covers = checkpoint.data["covers_through"]
    return order_key(covers["occurred_at"], covers["recorded_at"], covers["id"])


def _within(at: str | OrderKey | None) -> Any:
    if at is None:
        return lambda key: True
    if isinstance(at, OrderKey):
        return lambda key: key <= at
    cut = instant(at)
    return lambda key: key.k1 <= cut


def fold(
    events: Iterable[BeadEvent],
    *,
    at: str | OrderKey | None = None,
    recorded_as_of: str | None = None,
) -> dict[BeadRef, BeadState]:
    """Fold events into per-bead state at a cut.

    ``at=None`` is the open "now" cut (undated events included). A timestamp ``at=T`` is a
    valid-time cut keeping events whose k1 <= T; an :class:`OrderKey` keeps events at or before
    that key. ``recorded_as_of=R`` is the bitemporal "as recorded at R" read: it first drops
    events whose ``recorded_at`` is null or later than R. The log is folded as its own
    :func:`union_merge`, so duplicate ids fold once and an id collision raises.

    A checkpoint seeds its bead when no non-checkpoint event of the bead lies at or before its
    ``covers_through`` (the latest such checkpoint wins); otherwise it is an assertion and does
    not change the fold. Beads with no event inside the cut are absent from the result.
    """
    merged = union_merge([events])
    _, erased_beads = erasure(merged)
    if recorded_as_of is not None:
        horizon = instant(recorded_as_of)
    by_bead: dict[BeadRef, list[BeadEvent]] = {}
    for event in merged:
        if recorded_as_of is not None and (
            event.recorded_at is None or instant(event.recorded_at) > horizon
        ):
            continue
        by_bead.setdefault(event.bead, []).append(event)

    inside = _within(at)
    states: dict[BeadRef, BeadState] = {}
    for bead in sorted(set(by_bead) | erased_beads):
        if bead in erased_beads:
            states[bead] = BeadState(bead=bead, erased=True)
            continue
        history = sorted(by_bead[bead], key=ordering_key)
        changes = [event for event in history if event.kind is not EventKind.CHECKPOINT]
        checkpoints = [event for event in history if event.kind is EventKind.CHECKPOINT]
        first_change = ordering_key(changes[0]) if changes else None
        seeds = [
            checkpoint
            for checkpoint in checkpoints
            if first_change is None or first_change > checkpoint_key(checkpoint)
        ]
        builder = _Builder(bead)
        touched = False
        if seeds:
            seed = max(seeds, key=checkpoint_key)
            if inside(checkpoint_key(seed)):
                builder.seed(seed)
                touched = True
        for event in changes:
            key = ordering_key(event)
            if not inside(key):
                break
            builder.apply(event)
            touched = True
        if touched:
            states[bead] = builder.build()
    return states
