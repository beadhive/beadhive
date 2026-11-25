"""The bead-state snapshot the kit diffs around a turn."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["BeadState"]


@dataclass(frozen=True)
class BeadState:
    """The lifecycle surface the ADR reserves to BH: claim, status, labels, metadata."""

    status: str
    assignee: str
    labels: frozenset[str]
    metadata: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)  # gate id -> status of the gates on the bead
