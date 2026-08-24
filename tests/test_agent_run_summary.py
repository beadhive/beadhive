"""The `AgentRunSummary` projection contract (bh-6eu2c.1) — `docs/design/agent-run-summary-
projection-contract.md` is the rationale; this covers the one piece of real branching logic
the contract module ships: `seat_harvested.outcome` -> `state`, over every wire value
`beadhive.seatrun.RunOutcome` actually emits, plus the fixed `seat_cancelled` mapping and the
join-key field spellings the type carries unchanged from `dispatch_log.py`.
"""

from __future__ import annotations

from beadhive.agent_run_summary import (
    SEAT_CANCELLED_STATE,
    AgentRunState,
    AgentRunSummary,
    state_for_seat_harvested,
)
from beadhive.seatrun import RunOutcome


def test_seat_harvested_clean_outcomes_all_map_to_finished():
    for outcome in (RunOutcome.DONE, RunOutcome.BLOCKED, RunOutcome.HANDOFF):
        assert state_for_seat_harvested(str(outcome)) == AgentRunState.FINISHED


def test_seat_harvested_incomplete_maps_to_failed():
    assert state_for_seat_harvested(str(RunOutcome.INCOMPLETE)) == AgentRunState.FAILED


def test_seat_harvested_unrecognized_outcome_is_unknown_not_a_guess():
    assert state_for_seat_harvested("some-future-outcome") == AgentRunState.UNKNOWN


def test_seat_cancelled_maps_to_failed():
    assert SEAT_CANCELLED_STATE == AgentRunState.FAILED


def test_join_keys_carry_dispatch_log_field_values_unchanged():
    # `bead` / `session_id` are dispatch_log.py's own spellings (verified against bh-jksq's
    # epic notes — bh-jksq.1 has not landed). The contract stores them verbatim, no
    # translation layer, so a consumer can join both streams on plain equality.
    record = {"bead": "bh-abc123", "outcome": "done", "exit_code": 0, "session_id": "s-1"}
    summary = AgentRunSummary(
        bead=record["bead"],
        session_id=record["session_id"],
        state=state_for_seat_harvested(record["outcome"]),
    )
    assert summary.bead == record["bead"]
    assert summary.session_id == record["session_id"]
    assert summary.state == AgentRunState.FINISHED


def test_waiting_entry_has_no_session_yet():
    # A `waiting` entry (dispatch_pass.denied, bead not in in_flight) is bead-keyed only — no
    # seat has been spawned for this attempt, so session_id is empty until seat_spawned lands.
    summary = AgentRunSummary(bead="bh-xyz", session_id="", state=AgentRunState.WAITING)
    assert summary.session_id == ""
    assert summary.owner_seat is None
