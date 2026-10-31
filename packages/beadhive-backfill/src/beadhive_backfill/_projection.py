"""Per-pass corpus projection: the folded facets whose change invalidates a plan.

Generalizes ``complexity_backfill.corpus_hash``: a pass names the facets it reads
(:attr:`~beadhive_backfill.HivePass.invalidating_fields`) and, optionally, which beads it
governs (:class:`~beadhive_backfill.ScopedPass`). The projection of a fold is, for every
in-scope bead in bead order, the bead identity plus those facets; its hash is what a plan
records and what apply recomputes against a fresh baseline before touching anything.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from beadlog import BeadRef, BeadState

from ._errors import BackfillError

SCALAR_FACETS = frozenset({"exists", "deleted", "erased", "type", "phase", "retired"})
SET_FACETS = frozenset({"fields", "labels", "edges", "comments"})
FIELD_PREFIX = "fields."
# Everything a fold holds: the projection of a plan that names no pass (a neutral reconcile
# plan), so post-apply verification never depends on a store re-emitting event ids.
ALL_FACETS = SCALAR_FACETS | SET_FACETS

Exclusion = Callable[[BeadState], str | None]


def json_hash(value: Any) -> str:
    """``sha256:`` + hex SHA-256 of sorted-key, whitespace-free JSON (floats allowed)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_facets(facets: Iterable[str]) -> frozenset[str]:
    """Return ``facets`` when every name is a projection facet; raise :class:`BackfillError`."""
    names = frozenset(facets)
    unknown = sorted(
        name
        for name in names
        if name not in ALL_FACETS
        and not (name.startswith(FIELD_PREFIX) and len(name) > len(FIELD_PREFIX))
    )
    if unknown:
        raise BackfillError(f"unknown invalidating facets: {', '.join(unknown)}")
    return names


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def facet_value(state: BeadState, facet: str) -> Any:
    """The JSON value of one projection facet of ``state``.

    Comments project as ``(actor, body)`` in fold order, not by event id, so a store that keeps
    comments but not their BeadLog ids still verifies.
    """
    if facet.startswith(FIELD_PREFIX):
        return _plain(state.fields.get(facet[len(FIELD_PREFIX) :]))
    if facet == "fields":
        return _plain(state.fields)
    if facet == "labels":
        return list(state.labels)
    if facet == "edges":
        return [edge.to_json() for edge in state.edges]
    if facet == "comments":
        return [{"actor": item.actor, "body": item.body} for item in state.comments]
    return _plain(getattr(state, facet))


@dataclass(frozen=True)
class Scope:
    """The in-scope beads of a fold under one exclusion rule, and the exclusion counts."""

    included: tuple[BeadRef, ...]
    excluded: Mapping[BeadRef, str]

    @classmethod
    def of(cls, states: Mapping[BeadRef, BeadState], exclusion: Exclusion | None) -> Scope:
        included: list[BeadRef] = []
        excluded: dict[BeadRef, str] = {}
        for bead in sorted(states):
            reason = exclusion(states[bead]) if exclusion is not None else None
            if reason:
                excluded[bead] = str(reason)
            else:
                included.append(bead)
        return cls(tuple(included), excluded)

    def by_reason(self) -> dict[str, int]:
        return dict(sorted(Counter(self.excluded.values()).items()))


def projection(
    states: Mapping[BeadRef, BeadState],
    facets: Iterable[str],
    exclusion: Exclusion | None = None,
) -> list[dict[str, Any]]:
    """Stable, JSON-serializable inputs whose change invalidates a plan."""
    names = sorted(check_facets(facets))
    out = []
    for bead in Scope.of(states, exclusion).included:
        state = states[bead]
        item: dict[str, Any] = {"bead": [bead.group, bead.id]}
        for name in names:
            item[name] = facet_value(state, name)
        out.append(item)
    return out


def projection_hash(
    states: Mapping[BeadRef, BeadState],
    facets: Iterable[str],
    exclusion: Exclusion | None = None,
) -> str:
    """Hash of :func:`projection`."""
    return json_hash(projection(states, facets, exclusion))


def bead_key(bead: BeadRef) -> str:
    """The audit unit key of a bead: ``<group>/<id>`` (the format's facet spelling)."""
    return f"{bead.group}/{bead.id}"
