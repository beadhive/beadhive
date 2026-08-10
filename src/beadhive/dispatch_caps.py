"""dispatch_caps — the pure decision core for the two in-process caps an unattended local-tier
loop enforces on itself: MAX SEATS IN FLIGHT (concurrency) and a PER-RUN WALL-TIME cap.

Mirrors the `schedule.py` / `molecule.py` pattern: a CLI-free module with no I/O, no subprocess,
no `bd` calls. Given the configured caps plus the loop's own in-flight state (a plain int count,
a plain elapsed-seconds float), it returns a :class:`CapDecision` — allow/deny plus a
machine-readable `reason` code and a human-readable `detail`. Nothing here reads a clock, spawns
a process, or touches disk; the caller supplies `in_flight` and `elapsed_seconds` already
measured, and acts on the decision.

**Deliberately not the budget governor.** Token-window enforcement (the consumption ledger, the
otel counters, the reserve fraction, anything denominated in currency) is out of scope and
belongs to `bh-3yoh` — that epic owns `work.dispatch.budget`. This module's config keys
(`work.dispatch.max_seats_in_flight`, `work.dispatch.max_run_wall_time_seconds`) live in the same
`work.dispatch.*` section but are a distinct, narrower concern: they bound what is running RIGHT
NOW, not what has been spent. See docs/design/loop-ownership-and-execution-memory-adr.md
Decision 2 — both caps are held ONLY in the loop's own process memory and are correctly volatile:
they reset when the process restarts because they describe current state, not history. Nothing
in this module writes to disk, and that is enforced by what this module simply does not do —
there is no file/socket/subprocess API imported here at all.

**Wall-time cancellation.** A wall-time breach does not itself cancel anything — that is the
existing three-rung CANCEL ladder (docs/design/work-runtime-tiers-adr.md Amendment 2 §5),
implemented by the scheduler that owns the seat's pipe (bh-c6dk.5). This module only produces
the decision + the machine-readable reason (`REASON_WALL_TIME_EXCEEDED`) that tells the caller
to walk the ladder. Per that ADR's standing requirement, the priced envelope must be read BEFORE
the process group is reaped — this module has no opinion on that ordering; it is the caller's
job, and the seam here is kept to a plain deny decision precisely so `.5` can call it without any
adaptation.

**Deny reasons must surface.** `bh-h2yc` records the failure mode this module exists to avoid:
work that stalls silently instead of handing off. A denied dispatch (or a wall-time breach) is
always a non-empty, machine-readable `reason` plus a `detail` string a human can read directly —
never a bare `False` a caller could mistake for "nothing happened."
"""

from __future__ import annotations

from dataclasses import dataclass

# Machine-readable reason codes. `REASON_OK` is returned on every allow so callers never have to
# treat an empty/None reason as "the happy path" — allow and deny are both explicit outcomes.
REASON_OK = "ok"
REASON_SEATS_AT_CAP = "seats_at_cap"
REASON_WALL_TIME_EXCEEDED = "wall_time_exceeded"


@dataclass(frozen=True)
class Caps:
    """The two in-process caps, read once from config (`work.dispatch.*`) by the caller and
    passed in — this module never reads config itself. `<= 0` means unlimited on both fields,
    matching how other unset numeric caps in this codebase already read (e.g. `backup.py`'s
    `cap_mb <= 0`)."""

    max_seats_in_flight: int = 0
    max_run_wall_time_seconds: int = 0


@dataclass(frozen=True)
class CapDecision:
    """One cap's verdict. `allowed=False` is never silent: `reason` is a stable machine-readable
    code (`REASON_*`) and `detail` is a human-readable sentence an operator can read as-is."""

    allowed: bool
    reason: str
    detail: str


def check_admission(caps: Caps, in_flight: int) -> CapDecision:
    """Whether the loop may start one more seat, given `in_flight` seats already running.

    `caps.max_seats_in_flight <= 0` reads as unlimited — always allow. Otherwise allow strictly
    below the cap (`in_flight < max_seats_in_flight`) and deny at or above it."""
    if caps.max_seats_in_flight <= 0:
        return CapDecision(
            True, REASON_OK, "no seat concurrency cap configured (max_seats_in_flight unset)"
        )
    if in_flight < caps.max_seats_in_flight:
        return CapDecision(
            True,
            REASON_OK,
            f"{in_flight}/{caps.max_seats_in_flight} seats in flight — under cap",
        )
    return CapDecision(
        False,
        REASON_SEATS_AT_CAP,
        f"{in_flight}/{caps.max_seats_in_flight} seats in flight — at cap, deny new dispatch",
    )


def check_wall_time(caps: Caps, elapsed_seconds: float) -> CapDecision:
    """Whether a running seat has breached the per-run wall-time cap, given how long it has
    been running (`elapsed_seconds`, measured by the caller — this module never reads a clock).

    `caps.max_run_wall_time_seconds <= 0` reads as unlimited — always allow (never breached).
    Otherwise allow strictly below the cap and deny (breach) at or above it. A breach means
    "cancel this seat through the CANCEL ladder" — see the module docstring for what this
    module does and does not do about that."""
    if caps.max_run_wall_time_seconds <= 0:
        return CapDecision(
            True, REASON_OK, "no wall-time cap configured (max_run_wall_time_seconds unset)"
        )
    if elapsed_seconds < caps.max_run_wall_time_seconds:
        return CapDecision(
            True,
            REASON_OK,
            f"{elapsed_seconds:.1f}s/{caps.max_run_wall_time_seconds}s elapsed — under cap",
        )
    return CapDecision(
        False,
        REASON_WALL_TIME_EXCEEDED,
        f"{elapsed_seconds:.1f}s elapsed >= {caps.max_run_wall_time_seconds}s cap — "
        "cancel through the CANCEL ladder (work-runtime-tiers-adr Amendment 2 §5)",
    )
