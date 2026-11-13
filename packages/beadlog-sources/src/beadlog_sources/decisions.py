"""Operator decisions of the backfill re-run loop: one closed ``decision`` bead per unit key.

``bh backfill decide`` records a per-unit decision (``skip``, ``noise`` or ``link``) in the
hive's own bead store (``docs/design/backfill-rerun-control-adr.md``, Q3), and B5 consults it
before every other step (:data:`~beadlog_sources.identify.STEP_DECISION`). This module is the
neutral half: the record's shape, the events that write it, and the reader that finds the
decisions in a baseline. It has no store, git or clock (``decided_at`` is an argument).

**The record.** One bead per unit key, id ``mint_bead_id(prefix, group, "decision:" + key)`` (so
deciding the same unit twice is the same bead), type ``decision``, closed, titled
``Backfill decision: <kind> <key>``, labelled with the caller's labels (``origin:backfill`` and
the hive identity triplet) plus :data:`SOURCE_LABEL`, with ``external_ref`` =
:data:`REF_PREFIX` + the key (a namespace no unit key uses, so B5 step 1 never mistakes it for
the unit's own bead) and the metadata members :data:`METADATA_MEMBERS`: ``backfill.decision``,
``backfill.unit``, ``backfill.target`` (the bead for ``link``, the covering epic for ``noise``),
``backfill.reason``, ``backfill.decided_by`` and ``backfill.decided_at``. A new decision on a
unit that already has a record updates the same bead's metadata; bd keeps the earlier answer in
its history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from beadlog import BeadEvent, BeadRef, BeadState, fold, mint_event

from ._sources import mint_bead_id
from .commits import just_after

SOURCE_LABEL = "source:decision"
REF_PREFIX = "bfdec:"
BEAD_TYPE = "decision"
SOURCE_SYSTEM = "backfill-decision"

SKIP = "skip"
NOISE = "noise"
LINK = "link"
KINDS = (SKIP, NOISE, LINK)

M_DECISION = "metadata.backfill.decision"
M_UNIT = "metadata.backfill.unit"
M_TARGET = "metadata.backfill.target"
M_REASON = "metadata.backfill.reason"
M_BY = "metadata.backfill.decided_by"
M_AT = "metadata.backfill.decided_at"
METADATA_MEMBERS = (M_DECISION, M_UNIT, M_TARGET, M_REASON, M_BY, M_AT)


class DecisionError(ValueError):
    """A decision the ADR does not admit (unknown kind, a target where none belongs ...)."""


@dataclass(frozen=True)
class Decision:
    """One recorded decision: ``kind`` on ``unit``, ``target`` for ``link`` / ``noise``."""

    unit: str
    kind: str
    target: str | None
    bead: str  # the decision bead's own id
    reason: str = ""
    decided_by: str = ""
    decided_at: str = ""

    def to_json(self) -> dict[str, str | None]:
        return {
            "unit": self.unit,
            "decision": self.kind,
            "target": self.target,
            "bead": self.bead,
            "reason": self.reason,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }


def decision_id(prefix: str, group: str, key: str) -> str:
    """The deterministic id of the decision bead of unit ``key`` (the prefix is never derived)."""
    return mint_bead_id(prefix, group, "decision:" + key)


def is_decision(state: BeadState) -> bool:
    """Whether a folded bead is a decision record (its provenance label)."""
    return SOURCE_LABEL in state.labels


def check(kind: str, target: str | None) -> None:
    """Refuse a kind outside :data:`KINDS`, a ``skip`` with a target, or ``link`` / ``noise``
    without one."""
    if kind not in KINDS:
        raise DecisionError(f"decision must be one of {list(KINDS)}, not {kind!r}")
    if kind == SKIP and target:
        raise DecisionError("a skip decision takes no target bead")
    if kind != SKIP and not target:
        raise DecisionError(f"a {kind} decision needs a target bead")


def _title(kind: str, key: str) -> str:
    return f"Backfill decision: {kind} {key}"


def metadata(
    key: str, kind: str, target: str | None, reason: str, decided_by: str, decided_at: str
) -> dict[str, str]:
    """The ``metadata.backfill.*`` members of a decision (``target`` / ``reason`` when given)."""
    out = {M_DECISION: kind, M_UNIT: key, M_BY: decided_by, M_AT: decided_at}
    if target:
        out[M_TARGET] = target
    if reason:
        out[M_REASON] = reason
    return out


def decision_events(
    *,
    prefix: str,
    group: str,
    key: str,
    kind: str,
    target: str | None = None,
    reason: str = "",
    decided_by: str,
    decided_at: str,
    labels: Sequence[str] = (),
    existing: Mapping[str, object] | None = None,
) -> tuple[BeadEvent, ...]:
    """The events that record (or, with ``existing`` fields of the live record, update) a
    decision: a ``create`` with its labels and a closing ``phase``, or ``set`` events for what
    differs. Every event is dated ``decided_at``."""
    check(kind, target)
    bead = BeadRef(group, decision_id(prefix, group, key))
    meta = metadata(key, kind, target, reason, decided_by, decided_at)

    def emit(event_kind: str, data: Mapping[str, object], at: str = decided_at) -> BeadEvent:
        return mint_event(
            event_kind,
            bead,
            data,
            occurred_at=at,
            occurred_precision=None,
            recorded_at=None,
            extracted_at=decided_at,
            source=(SOURCE_SYSTEM, key),
            provenance="observed",
        )

    if existing is not None:
        fields = {"title": _title(kind, key), **meta}
        return tuple(
            emit("set", {"field": name, "value": value})
            for name, value in fields.items()
            if existing.get(name) != value
        )
    out = [
        emit(
            "create",
            {
                "type": BEAD_TYPE,
                "fields": {
                    "title": _title(kind, key),
                    "external_ref": REF_PREFIX + key,
                    "description": f"Operator decision on backfill unit {key}.",
                    **meta,
                },
            },
        )
    ]
    for label in dict.fromkeys([*labels, SOURCE_LABEL]):
        out.append(emit("label_add", {"label": label}))
    closed = {"phase": "closed", "reason": f"operator decision: {kind}"}
    out.append(emit("phase", closed, just_after(decided_at)))  # strictly after the create
    return tuple(out)


def read_decisions(baseline: Iterable[BeadEvent]) -> dict[str, Decision]:
    """The live decision records of a baseline, by unit key (an unknown kind is ignored)."""
    return decisions_of(fold(baseline))


def decisions_of(states: Mapping[BeadRef, BeadState]) -> dict[str, Decision]:
    """:func:`read_decisions` over folded states."""
    out: dict[str, Decision] = {}
    for ref, state in sorted(states.items(), key=lambda item: item[0]):
        if not state.exists or state.deleted or state.erased or not is_decision(state):
            continue
        fields = state.fields
        external = fields.get("external_ref")
        key = fields.get(M_UNIT) or (
            external[len(REF_PREFIX) :]
            if isinstance(external, str) and external.startswith(REF_PREFIX)
            else None
        )
        kind = fields.get(M_DECISION)
        if not isinstance(key, str) or not key or kind not in KINDS:
            continue
        target = fields.get(M_TARGET)
        out[key] = Decision(
            key,
            str(kind),
            target if isinstance(target, str) and target else None,
            ref.id,
            str(fields.get(M_REASON) or ""),
            str(fields.get(M_BY) or ""),
            str(fields.get(M_AT) or ""),
        )
    return out


__all__ = [
    "BEAD_TYPE",
    "KINDS",
    "LINK",
    "METADATA_MEMBERS",
    "NOISE",
    "REF_PREFIX",
    "SKIP",
    "SOURCE_LABEL",
    "Decision",
    "DecisionError",
    "check",
    "decision_events",
    "decision_id",
    "decisions_of",
    "is_decision",
    "metadata",
    "read_decisions",
]
