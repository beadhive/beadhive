"""Generic authoritative agent lifecycle and topology projection.

The Herdr adapter is one consumer of this contract, not its authority.  Callers
provide exact supervisor/work observations and this module supplies the stable,
presentation-neutral record used by operator clients.  In particular, a terminal
phase is an explicit work fact: terminal emulator ``idle`` state is never promoted
to completed Beadhive work.

Retirement is deliberately advisory.  Its receipt names the source revision that
was assessed; the mutating reap command must read the live authority again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any


class AgentHarness(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"
    UNKNOWN = "unknown"


class AgentRole(StrEnum):
    DEVELOPER = "developer"
    DISPATCHER = "dispatcher"
    UNKNOWN = "unknown"


class WorkOperation(StrEnum):
    IMPLEMENT = "work.implement"
    DISPATCH = "work.dispatch"
    SUBMIT = "work.submit"
    REVIEW = "work.review"
    MERGE = "work.merge"
    COMPLETE = "work.complete"
    UNKNOWN = "unknown"


class WorkPhase(StrEnum):
    IMPLEMENT = "implement"
    DISPATCH = "dispatch"
    SUBMIT = "submit"
    REVIEW = "review"
    MERGE = "merge"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class ParentRelation(StrEnum):
    ROOT = "root"
    DIRECT = "direct"
    MISSING = "missing"
    CYCLE = "cycle"
    UNKNOWN = "unknown"


class AgentPresence(StrEnum):
    LIVE = "live"
    RETAINED = "retained"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class RetirementReason(StrEnum):
    RETIRABLE = "retirable"
    LIVE = "live"
    RETAINED = "retained"
    PENDING_REVIEW = "pending-review"
    PENDING_OPERATION = "pending-operation"
    CHILD_RETAINED = "child-retained"
    STALE_REVISION = "stale-revision"
    SUPERVISOR_UNAVAILABLE = "supervisor-unavailable"
    FACTS_INCOMPLETE = "facts-incomplete"


_HARNESS_VALUES = frozenset(item.value for item in AgentHarness)
_ROLE_VALUES = frozenset(item.value for item in AgentRole)
_OPERATION_VALUES = frozenset(item.value for item in WorkOperation)
_PHASE_VALUES = frozenset(item.value for item in WorkPhase)
_PRESENCE_VALUES = frozenset(item.value for item in AgentPresence)
_PENDING_OPERATION_PHASES = frozenset(
    {WorkPhase.DISPATCH.value, WorkPhase.SUBMIT.value, WorkPhase.MERGE.value}
)


def _enum(value: object, allowed: frozenset[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _revision(rows: Sequence[Mapping[str, object]]) -> str:
    facts = [
        {
            key: row.get(key)
            for key in (
                "target",
                "hive",
                "bead",
                "harness",
                "role",
                "operation",
                "phase",
                "terminal_phase",
                "parent_bead",
                "presence",
                "active",
                "source_revision",
            )
        }
        for row in sorted(rows, key=lambda item: str(item.get("target") or ""))
    ]
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _cycle_nodes(parent_by_target: Mapping[str, str | None]) -> set[str]:
    """Return every node participating in a parent cycle, without recursing."""

    cycles: set[str] = set()
    for origin in parent_by_target:
        positions: dict[str, int] = {}
        path: list[str] = []
        current: str | None = origin
        while current is not None and current in parent_by_target:
            if current in positions:
                cycles.update(path[positions[current] :])
                break
            positions[current] = len(path)
            path.append(current)
            current = parent_by_target.get(current)
    return cycles


def _descendant_count(
    target: str, children: Mapping[str, set[str]], cycles: set[str]
) -> int | None:
    if target in cycles:
        return None
    seen: set[str] = set()
    pending = list(children.get(target, ()))
    while pending:
        child = pending.pop()
        if child == target or child in seen:
            return None
        seen.add(child)
        if child in cycles:
            return None
        pending.extend(children.get(child, ()))
    return len(seen)


def _retirement(
    *,
    source_revision: str | None,
    expected_revision: str | None,
    supervisor_available: bool,
    presence: str,
    phase: str,
    terminal_phase: bool | None,
    descendants: int | None,
    complete: bool,
) -> dict[str, object]:
    availability = "forbidden"
    code = RetirementReason.LIVE.value
    reason = "the authoritative supervisor still reports live work"

    if not supervisor_available:
        availability = "unavailable"
        code = RetirementReason.SUPERVISOR_UNAVAILABLE.value
        reason = "the authoritative supervisor roster is unavailable"
    elif source_revision is None or (
        expected_revision is not None and expected_revision != source_revision
    ):
        availability = "unavailable"
        code = RetirementReason.STALE_REVISION.value
        reason = "the retirement observation does not match the current source revision"
    elif not complete or terminal_phase is None or descendants is None:
        availability = "unavailable"
        code = RetirementReason.FACTS_INCOMPLETE.value
        reason = "authoritative lifecycle or topology facts are incomplete"
    elif phase == WorkPhase.REVIEW.value:
        code = RetirementReason.PENDING_REVIEW.value
        reason = "the work is pending review"
    elif phase in _PENDING_OPERATION_PHASES:
        code = RetirementReason.PENDING_OPERATION.value
        reason = f"the {phase} operation has not reached a terminal phase"
    elif descendants:
        code = RetirementReason.CHILD_RETAINED.value
        reason = f"{descendants} active descendant agent(s) are retained"
    elif presence == AgentPresence.RETAINED.value:
        code = RetirementReason.RETAINED.value
        reason = "the supervisor retains the completed agent"
    elif presence == AgentPresence.LIVE.value or not terminal_phase:
        code = RetirementReason.LIVE.value
        reason = "the authoritative supervisor still reports live work"
    elif presence == AgentPresence.STOPPED.value and terminal_phase:
        availability = "allowed"
        code = RetirementReason.RETIRABLE.value
        reason = "the work phase is terminal and no active descendants are retained"
    else:
        availability = "unavailable"
        code = RetirementReason.FACTS_INCOMPLETE.value
        reason = "authoritative agent presence is unknown"

    return {
        "availability": availability,
        "reason_code": code,
        "reason": reason,
        "source_revision": source_revision,
        "advisory": True,
    }


def project_agent_facts(
    observations: Sequence[Mapping[str, object]],
    *,
    source_revision: str | None = None,
    expected_revision: str | None = None,
    supervisor_available: bool = True,
) -> dict[str, Any]:
    """Project exact observations into reusable lifecycle/topology facts.

    ``parent_bead`` is the authority-bearing relation.  A parent target is joined
    only when exactly one active observation names that bead; target spellings are
    never decoded.  Counts include only observations whose ``active`` field is true
    (the default), and total descendants are not guessed across a cycle.
    """

    rows = [dict(item) for item in observations]
    revision = source_revision or (_revision(rows) if supervisor_available else None)
    active_rows = [item for item in rows if item.get("active", True) is True]
    by_bead: dict[str, list[str]] = {}
    for item in active_rows:
        target = _optional_text(item.get("target"))
        bead = _optional_text(item.get("bead"))
        if target and bead:
            by_bead.setdefault(bead, []).append(target)

    parent_by_target: dict[str, str | None] = {}
    relation_by_target: dict[str, str] = {}
    parent_bead_by_target: dict[str, str | None] = {}
    for item in active_rows:
        target = _optional_text(item.get("target"))
        if not target:
            continue
        parent_bead = _optional_text(item.get("parent_bead"))
        parent_bead_by_target[target] = parent_bead
        if parent_bead is None:
            parent_by_target[target] = None
            relation_by_target[target] = ParentRelation.ROOT.value
            continue
        matches = by_bead.get(parent_bead, [])
        if len(matches) == 1:
            parent_by_target[target] = matches[0]
            relation_by_target[target] = ParentRelation.DIRECT.value
        elif not matches:
            parent_by_target[target] = None
            relation_by_target[target] = ParentRelation.MISSING.value
        else:
            parent_by_target[target] = None
            relation_by_target[target] = ParentRelation.UNKNOWN.value

    cycles = _cycle_nodes(parent_by_target)
    children: dict[str, set[str]] = {}
    for child, parent in parent_by_target.items():
        if parent is not None:
            children.setdefault(parent, set()).add(child)

    projected: list[dict[str, object]] = []
    for item in rows:
        target = _optional_text(item.get("target"))
        hive = _optional_text(item.get("hive"))
        bead = _optional_text(item.get("bead"))
        harness = _enum(item.get("harness"), _HARNESS_VALUES, AgentHarness.UNKNOWN.value)
        role = _enum(item.get("role"), _ROLE_VALUES, AgentRole.UNKNOWN.value)
        operation = _enum(item.get("operation"), _OPERATION_VALUES, WorkOperation.UNKNOWN.value)
        phase = _enum(item.get("phase"), _PHASE_VALUES, WorkPhase.UNKNOWN.value)
        terminal_raw = item.get("terminal_phase")
        terminal_phase = terminal_raw if isinstance(terminal_raw, bool) else None
        presence = _enum(item.get("presence"), _PRESENCE_VALUES, AgentPresence.UNKNOWN.value)

        relation = relation_by_target.get(target or "", ParentRelation.UNKNOWN.value)
        parent_target = parent_by_target.get(target or "")
        if target in cycles:
            relation = ParentRelation.CYCLE.value
        direct = len(children.get(target or "", ())) if target else None
        descendants = _descendant_count(target, children, cycles) if target else None
        topology_state = "complete"
        if not supervisor_available:
            topology_state = "unavailable"
            direct = descendants = None
        elif relation in {
            ParentRelation.MISSING.value,
            ParentRelation.UNKNOWN.value,
            ParentRelation.CYCLE.value,
        }:
            topology_state = "partial"

        complete = bool(
            target
            and hive
            and bead
            and harness != AgentHarness.UNKNOWN.value
            and role != AgentRole.UNKNOWN.value
            and operation != WorkOperation.UNKNOWN.value
            and phase != WorkPhase.UNKNOWN.value
            and relation != ParentRelation.UNKNOWN.value
        )
        retirement = _retirement(
            source_revision=revision,
            expected_revision=expected_revision,
            supervisor_available=supervisor_available,
            presence=presence,
            phase=phase,
            terminal_phase=terminal_phase,
            descendants=descendants,
            complete=complete,
        )
        projected.append(
            {
                "schema_version": 1,
                "source_revision": revision,
                "target": target,
                "hive": hive,
                "bead": bead,
                "harness": harness,
                "role": role,
                "work": {
                    "operation": operation,
                    "phase": phase,
                    "terminal_phase": terminal_phase,
                },
                "parent": {
                    "relation": relation,
                    "target": parent_target,
                    "bead": parent_bead_by_target.get(target or ""),
                },
                "topology": {
                    "coverage": topology_state,
                    "direct_active_children": direct,
                    "total_active_descendants": descendants,
                },
                "retirement": retirement,
            }
        )

    projected.sort(key=lambda item: str(item.get("target") or ""))
    coverage = "complete"
    reason_code = None
    if not supervisor_available:
        coverage = "unavailable"
        reason_code = RetirementReason.SUPERVISOR_UNAVAILABLE.value
    elif expected_revision is not None and expected_revision != revision:
        coverage = "stale"
        reason_code = RetirementReason.STALE_REVISION.value
    elif any(item["topology"]["coverage"] != "complete" for item in projected):  # type: ignore[index]
        coverage = "partial"
        reason_code = "topology-partial"
    return {
        "schema_version": 1,
        "source_revision": revision,
        "coverage": {"state": coverage, "reason_code": reason_code},
        "agents": projected,
    }


__all__ = [
    "AgentHarness",
    "AgentPresence",
    "AgentRole",
    "ParentRelation",
    "RetirementReason",
    "WorkOperation",
    "WorkPhase",
    "project_agent_facts",
]
