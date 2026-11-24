"""`GateResolution` — one review gate observed resolving (gas-city-runtime-seam-adr.md Decision 3).

A leaf module on purpose: `beadhive.runtime` re-exports it as part of the `Runtime` protocol,
while the local loop and `gate_watch` build it without importing the runtime seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GateResolution:
    """One review gate observed resolving, handed to `Runtime.on_gate_resolved`. These are the
    bounce and feedback points.

    `verdict` is `approved` or `changes_requested`. `feedback_ref` is `None` for an approval and,
    for a bounce, a pointer a runtime can resolve to the reviewer's feedback: the id of the gate
    that was resolved (its close reason carries the feedback text; the bead also holds it as a
    comment). A runtime may use the hook to wind a session down or keep it warm and attach the
    feedback. It must NOT re-dispatch: changes-requested work is re-scheduled by the loop's
    `resume-changes-requested` decision, which calls `schedule()` again."""

    gate_id: str
    bead_id: str
    verdict: Literal["approved", "changes_requested"]
    feedback_ref: str | None = None
