"""Pure rules over the ``delivery`` and ``notified`` report dimensions (``bh-6wj9y`` R2, R3, R5).

Standard library only. These are the decisions the milestone triggers and the sender take; the
triggers and the sender themselves (the ``set_state`` writes, the merge and release hooks) are
later beads. Every rule here returns the value to write, or ``None`` for "write nothing", so a
re-run trigger never writes a second ``set-state`` (X2).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .models import BeadView
from .vocabulary import INTAKE_DIMENSIONS, NOTIFIED_DIM, delivery_of, is_rejected


class Verdict(StrEnum):
    """What a ``ReleaseVerifier`` established: only ``YES`` is proof of inclusion."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class CompletionPolicy(StrEnum):
    """``intake.external.completion_policy``: which milestone completes a report."""

    CLOSE_ON_MERGE = "close-on-merge"  # the default
    CLOSE_ON_RELEASE = "close-on-release"


#: The ``notified`` value a sent comment of each semantic event records; ``None`` for an event
#: that never notifies. Every event has an entry, so a new one cannot slip past it unnoticed.
NOTIFIED_BY_EVENT: dict[str, str | None] = {
    "reply": "replied",
    "merged": "merged",
    "released": "released",
    "corrected": "corrected",
    # the progress milestones (bh-dev42): a sent comment records the event's own value
    "started": "started",
    "blocked": "blocked",
    "deprioritised": "deprioritised",
    # the label projection (bh-tppoh): a labels-only record carries no comment, so it never tells
    # the reporter anything and never moves ``notified``
    "labels": None,
}
#: The ``notified`` values that tell the reporter about each ``delivery`` value (a later
#: milestone covers an earlier one: being told it was released covers being told it merged).
_COVERS = {
    "merged": frozenset({"merged", "released"}),
    "released": frozenset({"released"}),
    "reverted": frozenset({"corrected"}),
}


def notified_of(labels: Iterable[str] | None) -> str | None:
    """The ``notified`` value on a bead's label cache, or ``None``."""
    for label in labels or []:
        if label.startswith(f"{NOTIFIED_DIM}:"):
            value = label.split(":", 1)[1]
            if value in INTAKE_DIMENSIONS[NOTIFIED_DIM]:
                return value
    return None


def delivery_unowned(labels: Iterable[str] | None) -> bool:
    """True when a report's delivery is not its own (bh-5kpwc): a ``reject`` (a ``duplicate-of``
    ends in one too) fielded it, so no work is waited on, no milestone is drafted for it and no
    ``delivery`` or ``progress`` label is projected. A duplicate's delivery is its canonical
    report's, which already tracks the work."""
    return is_rejected(labels)


@dataclass(frozen=True)
class StrayDelivery:
    """One rejected report still carrying a ``delivery`` value (``delivery`` is that value)."""

    report: str
    delivery: str


@dataclass(frozen=True)
class DeliverySweepRequest:
    """Sweep for stray deliveries. ``repair`` also clears each one (as ``actor``)."""

    actor: str = ""
    repair: bool = False


@dataclass(frozen=True)
class DeliverySweep:
    """The reports a sweep found, the ones it cleared, and the clears that failed."""

    findings: tuple[StrayDelivery, ...] = ()
    cleared: tuple[StrayDelivery, ...] = ()
    failed: tuple[tuple[StrayDelivery, str], ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings


def stray_deliveries(reports: Iterable[BeadView]) -> tuple[StrayDelivery, ...]:
    """The bound reports (of any status) that were rejected, or closed as a duplicate, yet carry a
    ``delivery`` value, in id order: what a ``duplicate-of`` link wrote before bh-5kpwc."""
    found = (
        StrayDelivery(view.id, value)
        for view in reports
        if view.binding is not None
        and delivery_unowned(view.labels)
        and (value := delivery_of(view.labels)) is not None
    )
    return tuple(sorted(found, key=lambda item: item.report))


def delivery_after_merge(labels: Iterable[str] | None) -> str | None:
    """``pending`` or ``reverted`` move to ``merged`` when the tracked work merges; anything else
    (already ``merged``, ``released``, or never linked) writes nothing."""
    return "merged" if delivery_of(labels) in {"pending", "reverted"} else None


def delivery_after_release(labels: Iterable[str] | None, verdict: Verdict) -> str | None:
    """``merged`` moves to ``released`` only on a ``YES`` verdict; ``NO`` and ``UNKNOWN`` are
    reported as not verified and write nothing (never assumed)."""
    if delivery_of(labels) == "merged" and verdict is Verdict.YES:
        return "released"
    return None


def notification_behind(delivery: str | None, notified: str | None) -> bool:
    """True when the reporter has not been told about the current delivery milestone, for
    instance when its comment was abandoned or superseded and the report closed anyway."""
    covers = _COVERS.get(delivery or "")
    return covers is not None and notified not in covers  # pending: nothing to tell yet


def completion_event(policy: CompletionPolicy) -> str:
    """The milestone whose record completes a report under ``policy``."""
    return "merged" if policy is CompletionPolicy.CLOSE_ON_MERGE else "released"


def delivery_complete(policy: CompletionPolicy, delivery: str | None) -> bool:
    """Whether ``delivery`` has reached ``policy``'s milestone (``released`` covers a merge)."""
    if policy is CompletionPolicy.CLOSE_ON_MERGE:
        return delivery in {"merged", "released"}
    return delivery == "released"


__all__ = [
    "NOTIFIED_BY_EVENT",
    "CompletionPolicy",
    "DeliverySweep",
    "DeliverySweepRequest",
    "StrayDelivery",
    "Verdict",
    "completion_event",
    "delivery_after_merge",
    "delivery_after_release",
    "delivery_complete",
    "delivery_unowned",
    "notification_behind",
    "notified_of",
    "stray_deliveries",
]
