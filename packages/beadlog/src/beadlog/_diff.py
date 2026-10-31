"""``diff(proposed, baseline)``: the primitive reconcile classifies (backfill platform ADR §2).

The diff is the events of ``proposed`` whose ids are absent from ``baseline`` (less any the
merged log erases, since erased content is treated as if it never existed), plus per-bead
folded-state deltas: for every bead those events touch, or whose fold the merge changes, the
baseline fold, the proposed fold, the fold of ``union_merge([baseline, proposed])`` and the
facets that differ between the baseline and the merged fold.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ._fold import BeadState, fold
from ._merge import union_merge
from ._model import BeadEvent, BeadRef, Edge
from ._order import OrderKey, sort_events


def _edge_facet(edge: Edge) -> str:
    return f"edge:{edge.type}:{edge.target.group}/{edge.target.id}"


def changed_facets(before: BeadState | None, after: BeadState | None) -> tuple[str, ...]:
    """Names of the facets that differ between two folds of one bead, sorted.

    Names: ``exists``, ``deleted``, ``erased``, ``type``, ``phase``, ``retired``,
    ``fields.<name>``, ``label:<label>``, ``edge:<type>:<group>/<id>`` and
    ``comment:<event id>``. ``None`` stands for a bead absent at the cut.
    """
    if before is None and after is None:
        return ()
    bead = (before or after).bead  # type: ignore[union-attr]
    old = before or BeadState(bead=bead)
    new = after or BeadState(bead=bead)
    changed: set[str] = set()
    for name in ("exists", "deleted", "erased", "type", "phase", "retired"):
        if getattr(old, name) != getattr(new, name):
            changed.add(name)
    for name in set(old.fields) | set(new.fields):
        if old.fields.get(name) != new.fields.get(name):
            changed.add(f"fields.{name}")
    changed.update(f"label:{label}" for label in set(old.labels) ^ set(new.labels))
    changed.update(_edge_facet(edge) for edge in set(old.edges) ^ set(new.edges))
    changed.update(f"comment:{c.id}" for c in set(old.comments) ^ set(new.comments))
    return tuple(sorted(changed))


@dataclass(frozen=True)
class BeadDelta:
    """What merging ``proposed`` into ``baseline`` means for one bead."""

    bead: BeadRef
    events: tuple[BeadEvent, ...]
    baseline: BeadState | None
    proposed: BeadState | None
    merged: BeadState | None
    changed: tuple[str, ...]


@dataclass(frozen=True)
class LogDiff:
    """``missing``: proposed events absent from the baseline (as combined by the merge, never
    erased ones), in part order; ``deltas``: one
    :class:`BeadDelta` per affected bead, sorted by bead."""

    missing: tuple[BeadEvent, ...]
    deltas: tuple[BeadDelta, ...]

    @property
    def empty(self) -> bool:
        return not self.missing and not self.deltas


def diff(
    proposed: Iterable[BeadEvent],
    baseline: Iterable[BeadEvent],
    *,
    at: str | OrderKey | None = None,
) -> LogDiff:
    """Diff a proposed log against a baseline log (both folded at the cut ``at``).

    Raises :class:`~beadlog.IdCollisionError` when the logs disagree on an id's identity.
    """
    proposed = tuple(proposed)
    baseline = tuple(baseline)
    merged = union_merge([baseline, proposed])
    known = {event.id for event in baseline}
    surviving = {event.id: event for event in merged}
    # Erased content is treated as if it never existed: a diff never proposes it again.
    missing = sort_events(
        surviving[event_id]
        for event_id in {e.id for e in proposed} - known
        if event_id in surviving
    )

    before = fold(baseline, at=at)
    offered = fold(proposed, at=at)
    after = fold(merged, at=at)
    touched: dict[BeadRef, list[BeadEvent]] = {}
    for event in missing:
        touched.setdefault(event.bead, []).append(event)

    deltas = []
    for bead in sorted(set(before) | set(after) | set(touched)):
        changed = changed_facets(before.get(bead), after.get(bead))
        if not changed and bead not in touched:
            continue
        deltas.append(
            BeadDelta(
                bead=bead,
                events=tuple(touched.get(bead, ())),
                baseline=before.get(bead),
                proposed=offered.get(bead),
                merged=after.get(bead),
                changed=changed,
            )
        )
    return LogDiff(missing=missing, deltas=tuple(deltas))
