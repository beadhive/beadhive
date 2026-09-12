"""Canonical, provider-independent contracts for externally managed seats."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SeatContract:
    seat: str
    version: str
    instructions: str

    @property
    def digest(self) -> str:
        payload = f"beadhive-seat-contract\0{self.seat}\0{self.version}\0{self.instructions}"
        return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


_CONTRACTS = {
    "developer": SeatContract(
        "developer",
        "1",
        "You are the Beadhive developer seat. Implement only the assigned bead in its managed "
        "worktree, validate it, and submit it for review. Never approve or merge your own work.",
    ),
    "dispatcher": SeatContract(
        "dispatcher",
        "1",
        "You are the Beadhive dispatcher seat. Deliver the approved epic by scheduling and "
        "coordinating its beads on the epic container. Do not replace developer or merger "
        "authority.",
    ),
    "planner": SeatContract(
        "planner",
        "1",
        "You are the Beadhive planner seat. Turn the human's idea into decision records and a "
        "gated bead molecule. Planning produces no product code and never performs integration "
        "work.",
    ),
}

# Existing role launches outside the managed developer/dispatcher/planner matrix retain a
# canonical contract too. They are deliberately not advertised by the Herdr managed-launch
# surface, but resolving them must remain deterministic for ``bh role`` compatibility.
for _seat, _duty in {
    "reviewer": (
        "Review submitted work against its acceptance criteria; approve or request changes."
    ),
    "merger": (
        "Serialize approved integration while preserving history and keeping the target green."
    ),
    "analyst": (
        "Research the assigned question and return evidence without taking lifecycle authority."
    ),
    "warden": "Evaluate security and policy evidence without performing integration work.",
    "supervisor": "Govern factory policy and delegate control-plane execution.",
    "director": "Route fleet work and triage intake without implementing integration beads.",
    "custodian": "Maintain factory configuration, provisioning, and safe cleanup.",
    "controller": "Observe and report factory telemetry without mutating integration work.",
}.items():
    _CONTRACTS[_seat] = SeatContract(_seat, "1", f"You are the Beadhive {_seat} seat. {_duty}")


def seat_contract(seat: str) -> SeatContract:
    """Return the one authoritative versioned contract, failing closed."""

    try:
        return _CONTRACTS[seat]
    except KeyError as exc:
        raise ValueError(f"seat {seat!r} has no managed launch contract") from exc
