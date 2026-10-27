"""The lifetime intake-hold on a bound report: records and pure rules. Standard library only.

A bound report (one created from an external issue) is never work. Its whole life it carries one
open human gate, the *intake-hold*, which blocks it natively: it stays out of ``bd ready`` and
``bh work ready``, ``bh work claim`` refuses it, and a plain close is refused by the store
(external-intake-module-adr.md section 7, bh-6wj9y R3 and E4). The report closes only through the
store's held close: force-close first, then resolve the hold as bookkeeping, so there is no
instant at which it is open and unblocked.

This module owns the vocabulary of that hold (its marker, reason and gate type), the rule that
reads a report's blockers, and the sweep that compares bound reports with open holds. The root's
``guard.INTAKE_HOLD_GATE_MARKER`` spells the same marker for ``bh work``; a test pins the two
equal, because this module may not import the root.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: The marker the hold's reason carries; the root's seat policy and gate classifier match it.
INTAKE_HOLD_MARKER = "intake-hold:"
#: The reason a hold gate is created with (visible in ``bd show``).
INTAKE_HOLD_REASON = f"{INTAKE_HOLD_MARKER} external report, never work"
#: bd's gate type for the hold: a human gate, which no timer or watcher can satisfy.
INTAKE_HOLD_GATE_TYPE = "human"
#: The dependency types bd counts as blocking a bead's close and readiness
#: (``AffectsReadyWork`` without ``parent-child``).
BLOCKING_EDGE_TYPES = frozenset({"blocks", "conditional-blocks", "waits-for"})


def is_hold_gate(gate: Mapping[str, Any]) -> bool:
    """True when a bd gate or dependency row is an ``intake-hold:`` gate, matched on the marker in
    its reason or in the ``Reason: ...`` tail of its description (the two shapes bd renders)."""
    reason = str(gate.get("reason") or "").lower()
    description = str(gate.get("description") or "").lower()
    return INTAKE_HOLD_MARKER in reason or f"reason: {INTAKE_HOLD_MARKER}" in description


@dataclass(frozen=True)
class Blockers:
    """What blocks a report's close, split into its own holds and everything else.

    ``holds`` are the open hold gates; ``others`` are any other live blocker (an open bead or a
    gate of another kind). ``closed`` is whether the report itself is already closed."""

    holds: tuple[str, ...] = ()
    others: tuple[str, ...] = ()
    closed: bool = False


def blockers_of(row: Mapping[str, Any]) -> Blockers:
    """Read a bead's blockers from its ``bd show`` row.

    A blocker is a dependency of a blocking type whose bead is not closed; a hold is such a
    blocker that is an ``intake-hold:`` gate. A row without ``dependencies`` has no blockers."""
    holds: list[str] = []
    others: list[str] = []
    dependencies = row.get("dependencies")
    for dependency in dependencies if isinstance(dependencies, list) else ():
        if not isinstance(dependency, Mapping):
            continue
        if str(dependency.get("dependency_type") or "") not in BLOCKING_EDGE_TYPES:
            continue
        if str(dependency.get("status") or "") == "closed":
            continue
        identifier = str(dependency.get("id") or "")
        (holds if is_hold_gate(dependency) else others).append(identifier)
    return Blockers(tuple(holds), tuple(others), str(row.get("status") or "") == "closed")


@dataclass(frozen=True)
class HoldGate:
    """One open ``intake-hold:`` gate and the beads it blocks (none: the gate is orphaned)."""

    gate: str
    blocks: tuple[str, ...] = ()


class HoldFindingKind(StrEnum):
    #: An open bound report with no open hold: it would be offered as work. Repair opens one.
    MISSING = "missing"
    #: An open hold on a report that is already closed (a crash between the close and the
    #: resolve). Harmless, because a closed bead is never ready. Repair resolves the gate.
    UNRESOLVED = "unresolved"
    #: An open hold that blocks no bound report: nothing, or a bead intake never bound. Reported
    #: only; a person decides what the gate guards.
    ORPHANED = "orphaned"


@dataclass(frozen=True)
class HoldFinding:
    """One inconsistency between the bound reports and the open holds."""

    kind: HoldFindingKind
    report: str = ""
    gate: str = ""
    detail: str = ""

    @property
    def repairable(self) -> bool:
        return self.kind is not HoldFindingKind.ORPHANED


@dataclass(frozen=True)
class HoldSweepRequest:
    """Sweep the holds. ``repair`` also fixes every repairable finding (as ``actor``)."""

    actor: str = ""
    repair: bool = False


@dataclass(frozen=True)
class HoldSweep:
    """The findings of one sweep, the ones repaired, and the repairs that failed."""

    findings: tuple[HoldFinding, ...] = ()
    repaired: tuple[HoldFinding, ...] = ()
    failed: tuple[tuple[HoldFinding, str], ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings


def hold_findings(
    reports: Iterable[tuple[str, bool]], holds: Sequence[HoldGate]
) -> tuple[HoldFinding, ...]:
    """Compare bound reports with open holds. ``reports`` are ``(id, closed)``; ``holds`` the open
    hold gates. Deterministic order: reports sorted by id, then orphaned gates by id."""
    reports = tuple(reports)
    by_report: dict[str, list[str]] = {}
    for hold in holds:
        for report in hold.blocks:
            by_report.setdefault(report, []).append(hold.gate)
    known = {report for report, _closed in reports}
    findings: list[HoldFinding] = []
    for report, closed in sorted(reports):
        gates = sorted(by_report.get(report, ()))
        if closed:
            findings.extend(
                HoldFinding(
                    HoldFindingKind.UNRESOLVED,
                    report,
                    gate,
                    f"hold {gate} is still open on closed report {report}",
                )
                for gate in gates
            )
        elif not gates:
            findings.append(
                HoldFinding(
                    HoldFindingKind.MISSING,
                    report,
                    "",
                    f"open bound report {report} has no open intake-hold (it could be offered "
                    "as work)",
                )
            )
    findings.extend(
        HoldFinding(
            HoldFindingKind.ORPHANED,
            "",
            hold.gate,
            f"hold {hold.gate} blocks "
            + (
                "no bead"
                if not hold.blocks
                else f"{', '.join(sorted(hold.blocks))}, not a bound report"
            ),
        )
        for hold in sorted(holds, key=lambda h: h.gate)
        if not known.intersection(hold.blocks)
    )
    return tuple(findings)


__all__ = [
    "BLOCKING_EDGE_TYPES",
    "INTAKE_HOLD_GATE_TYPE",
    "INTAKE_HOLD_MARKER",
    "INTAKE_HOLD_REASON",
    "Blockers",
    "HoldFinding",
    "HoldFindingKind",
    "HoldGate",
    "HoldSweep",
    "HoldSweepRequest",
    "blockers_of",
    "hold_findings",
    "is_hold_gate",
]
