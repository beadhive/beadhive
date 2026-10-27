"""Pure rules over the ``delivery`` and ``notified`` report dimensions (``bh-6wj9y`` R2, R3, R5).

Standard library only. These are the decisions the milestone triggers and the sender take; the
triggers and the sender themselves (the ``set_state`` writes, the merge and release hooks) are
later beads. Every rule here returns the value to write, or ``None`` for "write nothing", so a
re-run trigger never writes a second ``set-state`` (X2).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from .vocabulary import INTAKE_DIMENSIONS, NOTIFIED_DIM, delivery_of


class Verdict(StrEnum):
    """What a ``ReleaseVerifier`` established: only ``YES`` is proof of inclusion."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class CompletionPolicy(StrEnum):
    """``intake.external.completion_policy``: which milestone completes a report."""

    CLOSE_ON_MERGE = "close-on-merge"  # the default
    CLOSE_ON_RELEASE = "close-on-release"


#: The ``notified`` value a sent comment of each semantic event records.
NOTIFIED_BY_EVENT = {
    "reply": "replied",
    "merged": "merged",
    "released": "released",
    "corrected": "corrected",
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
    "Verdict",
    "completion_event",
    "delivery_after_merge",
    "delivery_after_release",
    "delivery_complete",
    "notification_behind",
    "notified_of",
]
