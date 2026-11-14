"""The unit selector of ``bh backfill decide --select`` (ADR ``backfill-merge-heavy-repos``, D6).

One clause is ``<field><op><value>`` with ``op`` ``=`` (exact) or ``~`` (``re.search``, no flags,
at most :data:`MAX_PATTERN` characters); a value may be quoted with ``'`` or ``"``. Clauses are
ANDed. The fields are a fixed list (:data:`FIELDS`); a field the collation lacks (``type`` without
``collate.merge_types``, ``noise`` with ``collate.noise = []``, ``status`` before the status step)
is a usage error, never a silent zero match. Matches are sorted by unit key, so equal inputs
select equal sets.

Guard rails: ``attach`` units (their identity is an existing bead) and units that already have a
live bead (a PRESENT verdict, or a live bead whose ``external_ref`` is the unit key) are excluded
and reported, so a selector cannot write a decision contradicting an existing bead.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from beadlog_sources import Collation
from beadlog_sources.collate import ATTACH, UNIT_KINDS, UNIT_RULES
from beadlog_sources.identify import CLASS_SKIP
from beadlog_sources.lifecycle import STATUSES

from . import backfill_views

FIELDS = ("rule", "kind", "area", "title", "key", "decision", "status", "type", "noise")
MAX_PATTERN = 200
DECISIONS = ("NEW", "PRESENT", "NOISE", CLASS_SKIP, backfill_views.RESIDUAL)
_VOCABULARY: dict[str, tuple[str, ...]] = {
    "rule": tuple(UNIT_RULES),
    "kind": tuple(UNIT_KINDS),
    "decision": DECISIONS,
    "status": tuple(STATUSES),
}
_CLAUSE = re.compile(r"^\s*([A-Za-z_]+)\s*([=~])\s*(.*?)\s*$", re.DOTALL)


class SelectError(ValueError):
    """A usage error of the selector (bad clause, unknown field, a field the collation lacks)."""


@dataclass(frozen=True)
class Clause:
    """One parsed ``field op value`` clause; ``raw`` is the clause as typed."""

    field: str
    op: str
    value: str
    raw: str
    pattern: re.Pattern[str] | None = None

    def matches(self, actual: str | None) -> bool:
        if actual is None:
            return False
        if self.pattern is not None:
            return self.pattern.search(actual) is not None
        return actual == self.value


def parse_clause(text: str) -> Clause:
    """Parse one ``--select`` value, or raise :class:`SelectError`."""
    found = _CLAUSE.match(text)
    if found is None:
        raise SelectError(f"usage: --select needs <field><op><value> ('=' or '~'), got {text!r}")
    name, op, value = found.groups()
    if name not in FIELDS:
        raise SelectError(f"usage: unknown --select field {name!r}; use one of {', '.join(FIELDS)}")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if op == "~":
        if len(value) > MAX_PATTERN:
            raise SelectError(f"usage: a --select pattern is at most {MAX_PATTERN} characters")
        try:
            return Clause(name, op, value, text.strip(), re.compile(value))
        except re.error as exc:
            raise SelectError(
                f"usage: --select {name}~ is not a regular expression ({exc})"
            ) from None
    vocabulary = _VOCABULARY.get(name)
    if vocabulary is not None:
        wanted = value.upper() if name == "decision" and value.lower() != "residual" else value
        if wanted not in vocabulary:
            raise SelectError(
                f"usage: --select {name}={value!r} is not one of {', '.join(vocabulary)}"
            )
        value = wanted
    return Clause(name, op, value, text.strip())


def parse_clauses(texts: Sequence[str]) -> tuple[Clause, ...]:
    if not texts:
        raise SelectError("usage: --select needs at least one <field><op><value> clause")
    return tuple(parse_clause(t) for t in texts)


def _check_available(collation: Collation, clauses: Sequence[Clause]) -> None:
    """A field the collation lacks is a usage error naming the option that produces it."""
    cluster = collation.options.get("cluster") or {}
    for clause in clauses:
        if clause.field == "type" and not cluster.get("merge_types"):
            raise SelectError(
                "usage: this collation has no merge types, so `type` cannot select; set "
                'collate.merge_types=["*"] (or the types you want), re-run `bh backfill collate`'
            )
        if clause.field == "noise" and not cluster.get("noise"):
            raise SelectError(
                "usage: this collation flags no noise, so `noise` cannot select; set "
                'collate.noise=["branch-sync"] (the default), re-run `bh backfill collate`'
            )
        if clause.field == "status" and not collation.lifecycle:
            raise SelectError(
                "usage: this collation has no status (lifecycle) records, so `status` cannot "
                "select; run `bh backfill history --until status` (or later) first"
            )


@dataclass(frozen=True)
class Selection:
    """``matched`` unit keys (sorted) and the ``excluded`` ones as ``(key, reason)``."""

    matched: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]


def select_units(
    collation: Collation, clauses: Sequence[Clause], live_refs: Collection[str] = ()
) -> Selection:
    """The units every clause matches, minus the guard-railed ones (attach, live bead)."""
    _check_available(collation, clauses)
    decided = backfill_views.decisions(collation)
    life = {record["unit"]: record for record in collation.lifecycle}
    matched: list[str] = []
    excluded: list[tuple[str, str]] = []
    for unit in sorted(collation.units, key=lambda u: u.key):
        record = decided.get(unit.key)
        values: Mapping[str, str | None] = {
            "rule": unit.rule,
            "kind": unit.kind,
            "area": unit.area,
            "title": unit.title,
            "key": unit.key,
            "decision": record["class"] if record else backfill_views.RESIDUAL,
            "status": (life.get(unit.key) or {}).get("status"),
            "type": unit.type,
            "noise": unit.noise,
        }
        if not all(c.matches(values[c.field]) for c in clauses):
            continue
        if unit.kind == ATTACH:
            excluded.append((unit.key, "attach unit (its identity is an existing bead)"))
        elif (record or {}).get("class") == "PRESENT" or unit.key in live_refs:
            excluded.append((unit.key, "already has a live bead"))
        else:
            matched.append(unit.key)
    return Selection(tuple(matched), tuple(excluded))


def live_external_refs(states: Mapping[Any, Any]) -> frozenset[str]:
    """The ``external_ref`` of every live bead of the baseline (a decision record's own ref has
    the ``decision:`` prefix, so it never equals a unit key)."""
    out: set[str] = set()
    for state in states.values():
        if state.exists and not state.deleted and not state.erased:
            ref = state.fields.get("external_ref")
            if isinstance(ref, str) and ref:
                out.add(ref)
    return frozenset(out)


def selector_reason(clauses: Sequence[Clause], reason: str = "") -> str:
    """The reason recorded on each decision: the clauses as typed, then the operator's reason."""
    text = "select: " + " AND ".join(c.raw for c in clauses)
    return f"{text}; {reason}" if reason else text
