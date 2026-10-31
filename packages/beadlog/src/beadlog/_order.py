"""The v0.1 total order over events (format ADR section 6) and archive part order."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ._model import BeadEvent, BeadRef
from ._time import Instant, instant


@dataclass(frozen=True, order=True)
class OrderKey:
    """``(k1, k2, id)``: k1 = occurred_at, else recorded_at, else +inf; k2 = recorded_at, else
    +inf; ties broken by id compared as ASCII. Timestamps compare as instants."""

    k1: Instant
    k2: Instant
    id: str


def order_key(occurred_at: str | None, recorded_at: str | None, event_id: str) -> OrderKey:
    """The ordering key of explicit times and an id (e.g. a checkpoint's ``covers_through``)."""
    first = occurred_at if occurred_at is not None else recorded_at
    return OrderKey(instant(first), instant(recorded_at), event_id)


def ordering_key(event: BeadEvent) -> OrderKey:
    """The ordering key of one event."""
    return order_key(event.occurred_at, event.recorded_at, event.id)


def part_order(event: BeadEvent) -> tuple[BeadRef, OrderKey]:
    """Archive part order: ``(bead.group, bead.id, ordering key)``."""
    return (event.bead, ordering_key(event))


def sort_events(events: Iterable[BeadEvent]) -> tuple[BeadEvent, ...]:
    """Events in archive part order."""
    return tuple(sorted(events, key=part_order))
