"""The `AgentRunSummary` projection contract over `dispatch_log.py` records (bh-6eu2c.1).

`docs/design/agent-run-summary-projection-contract.md` is the full rationale — the mapping
table for all five record types `dispatch_log.py` emits today, the `seat_cancelled` and
`dispatch_pass`/`waiting` decisions, and the join-key statement against `bh-jksq`'s epic notes.
This module is the typed shape that document defines, for bh-6eu2c.2 (the reader) and bh-6eu2c.3
(the CLI) to import rather than re-derive.

**This module does not read the sink.** It has no dependency on `dispatch_log.tail_records` or
any stateful, cross-record correlation (session promotion via `in_flight`, `waiting` derivation
from `denied`, freshness from file mtime) — all of that requires tracking state across multiple
passes and belongs to the reader (bh-6eu2c.2), not the contract. What lives here is the state
enum, the two single-record mapping rules that ARE pure functions of one record
(`state_for_seat_harvested` and `SEAT_CANCELLED_STATE`), and the `AgentRunSummary` /
`Freshness` dataclasses themselves.

**Producer boundary — restated from the design doc, not just cited.** `AgentRunSummary` is
built entirely from `beadhive.dispatch_log`, a per-hive, host-local, non-git-synced execution
record. Per `docs/design/work-runtime-tiers-adr.md` Decision 1, this producer is NEVER
authoritative about whether a bead is claimed, blocked, approved, or done — `state` describes
what one seat process did, not what state the bead is in. A `finished` run's bead can still be
`blocked` in beads; a consumer that wants bead lifecycle truth reads bead state, not this type.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


#: Verbatim, unwidened copy of `beadhive-ui/packages/operator-contract/src/types.ts`'s
#: `AgentRunSummary['state']` union — a read-only reference this hive does not own, hence no
#: seventh value even for `seat_cancelled` (see the design doc's "named upstream gap").
class AgentRunState(StrEnum):
    STARTING = "starting"
    ACTIVE = "active"
    WAITING = "waiting"
    FINISHED = "finished"
    FAILED = "failed"
    UNKNOWN = "unknown"


#: `seat_cancelled` always means a dispatcher-initiated termination (the CANCEL ladder ran
#: against this seat) — never a run completing on its own. See the design doc's "seat_cancelled
#: -> failed" section for why this overloads `failed` rather than widening the enum, and the
#: named follow-up (a possible `beadhive-ui` upstream bead) that overload leaves open.
SEAT_CANCELLED_STATE = AgentRunState.FAILED

#: `seat_harvested.outcome` -> `AgentRunSummary.state`, keyed off the wire values
#: `beadhive.seatrun.RunOutcome` actually emits today (`done` / `blocked` / `handoff` /
#: `incomplete`) rather than the `success`/`failure` pair the epic's design sketch assumed.
#: `done` / `blocked` / `handoff` are all clean, intentional exits (EXIT_DONE/BLOCKED/HANDOFF —
#: 0/10/11 — with a parsed `SeatRun`) and all map to `finished`; only `incomplete` (no
#: parseable `SeatRun` — killed, crashed, or no envelope) is a real `failed`.
_HARVESTED_OUTCOME_STATE: dict[str, AgentRunState] = {
    "done": AgentRunState.FINISHED,
    "blocked": AgentRunState.FINISHED,
    "handoff": AgentRunState.FINISHED,
    "incomplete": AgentRunState.FAILED,
}


def state_for_seat_harvested(outcome: str) -> AgentRunState:
    """Map a `seat_harvested` record's `outcome` field to `AgentRunSummary.state`.

    Defensive on an outcome this contract has not seen (the wire taxonomy is a closed set today,
    per `beadhive.seatrun.RunOutcome`, but a reader must never silently coerce an unrecognized
    value into a specific terminal state) -> `unknown` rather than a guess.
    """
    return _HARVESTED_OUTCOME_STATE.get(outcome, AgentRunState.UNKNOWN)


#: Mirrors `beadhive-ui`'s `Freshness` shape. Per the design doc, there is no remote authority
#: to resync a host-local sink against, so `state` defaults to `"unknown"` — only a reader that
#: can positively confirm colocation with the sink's writer (bh-6eu2c.2) is entitled to report
#: `"fresh"` / `"stale"`.
@dataclass(frozen=True)
class Freshness:
    state: str = "unknown"
    as_of: float | None = None
    expires_at: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class AgentRunSummary:
    """One agent run, projected from `dispatch_log.py` records for a single hive.

    ``bead`` and ``session_id`` preserve the exact spellings and values in `dispatch_log.py`.
    After the landed stream contract and run-journal writer truth are considered, only ``bead``
    is a cross-source join: ``session_id`` identifies this dispatch seat process and MUST NOT be
    treated as the journal's outer ``run_id`` or its provider continuation. ``session_id`` is
    ``""`` for a ``waiting`` entry because no seat has been spawned yet.
    """

    bead: str
    session_id: str
    state: AgentRunState
    owner_seat: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    ended_at: float | None = None
    freshness: Freshness = Freshness()
