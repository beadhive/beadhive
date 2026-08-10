"""beadhive.seatrun — the role-binary contract as bh parses/classifies it (bh-c6dk.2).

Amendment 2 §1 of docs/design/work-runtime-tiers-adr.md is the specification. These tests cover
the beadhive-side parsing layer only: `SeatRun`/`RoleOutcome` parsing, the (exit code, stdout) ->
`Classification` helper that is the ONLY place any tier is meant to consult, `--workspace`
validation, envelope survival on a killed run, and the already-advanced no-op check. Baking
authority / provider / credentials is baml-harness's build-time scope and is not tested here.
"""

from __future__ import annotations

import json
import os

import pytest

from beadhive import seatrun


# ---- parse_seat_run ----


def test_parse_seat_run_clean_completion():
    line = json.dumps(
        {
            "outcome": {
                "status": "done",
                "summary": "did the thing",
                "bead_id": "bh-abc",
                "next_action": None,
            },
            "session_id": "s-1",
            "cost_usd": 0.53,
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "packs": [],
        }
    )
    run = seatrun.parse_seat_run(line)
    assert run.outcome.status == "done"
    assert run.outcome.bead_id == "bh-abc"
    assert run.session_id == "s-1"
    assert run.cost_usd == 0.53


def test_parse_seat_run_blocked_status_from_the_spike_transcript():
    # bh-a7so.1 Evidence 4 — a real observed line, both status=blocked runs exited 0.
    line = (
        '{"outcome":{"status":"blocked","summary":"lint blocked submit","bead_id":"bh-1a05",'
        '"next_action":"escalate"},"session_id":"c649a0af-3415-46c4-be24-85ce1a918981",'
        '"cost_usd":0.5336196000000001,"usage":{"input_tokens":14,"output_tokens":2310,'
        '"cache_creation_input_tokens":64855,"cache_read_input_tokens":365992},"packs":[]}'
    )
    run = seatrun.parse_seat_run(line)
    assert run.outcome.status == "blocked"
    assert run.outcome.bead_id == "bh-1a05"


@pytest.mark.parametrize(
    "stdout",
    ["", "   ", "not json", "{}", '{"outcome": {}}', '{"outcome": {"status": "bogus"}}'],
)
def test_parse_seat_run_rejects_malformed_input(stdout):
    with pytest.raises(seatrun.SeatRunParseError):
        seatrun.parse_seat_run(stdout)


def test_parse_seat_run_rejects_multiple_lines():
    line = json.dumps({"outcome": {"status": "done"}, "session_id": "s"})
    with pytest.raises(seatrun.SeatRunParseError):
        seatrun.parse_seat_run(line + "\n" + line)


# ---- parse_envelope ----


def test_parse_envelope_recognizes_killed_run_shape():
    payload = {
        "session_id": "s-2",
        "cost_usd": 0.02,
        "usage": {"input_tokens": 5},
        "terminal_reason": "sigterm",
    }
    env = seatrun.parse_envelope(json.dumps(payload))
    assert env is not None
    assert env.session_id == "s-2"
    assert env.terminal_reason == "sigterm"


def test_parse_envelope_returns_none_for_a_full_seat_run():
    line = json.dumps({"outcome": {"status": "done"}, "session_id": "s"})
    assert seatrun.parse_envelope(line) is None


def test_parse_envelope_returns_none_for_garbage():
    assert seatrun.parse_envelope("") is None
    assert seatrun.parse_envelope("not json") is None


# ---- classify_run — the single helper every tier is meant to consult ----


def test_classify_run_prefers_stdout_status_when_exit_is_zero():
    line = json.dumps({"outcome": {"status": "done", "summary": "ok"}, "session_id": "s"})
    result = seatrun.classify_run(0, line)
    assert result.outcome is seatrun.RunOutcome.DONE
    assert result.seat_run is not None


def test_classify_run_blocked_status_survives_exit_zero():
    # The exact bh-a7so.1 finding: status=blocked, exit=0. classify_run must not read the 0 as
    # success — it must report BLOCKED, taken from stdout.
    line = json.dumps({"outcome": {"status": "blocked", "summary": "lint"}, "session_id": "s"})
    result = seatrun.classify_run(0, line)
    assert result.outcome is seatrun.RunOutcome.BLOCKED


def test_classify_run_never_treats_bare_exit_zero_as_success():
    # No SeatRun on stdout at all, but exit is 0 (should never happen upstream today, but the
    # helper must not fabricate a DONE from the exit code alone).
    result = seatrun.classify_run(0, "")
    assert result.outcome is seatrun.RunOutcome.INCOMPLETE


def test_classify_run_falls_back_to_envelope_when_stdout_is_a_killed_run():
    payload = {"session_id": "s-3", "cost_usd": 0.01, "usage": {}, "terminal_reason": "sigterm"}
    result = seatrun.classify_run(143, json.dumps(payload))
    assert result.outcome is seatrun.RunOutcome.INCOMPLETE
    assert result.envelope is not None
    assert result.envelope.terminal_reason == "sigterm"
    assert result.seat_run is None


def test_classify_run_incomplete_with_no_pricing_info_when_nothing_parses():
    result = seatrun.classify_run(1, "")
    assert result.outcome is seatrun.RunOutcome.INCOMPLETE
    assert result.envelope is None
    assert result.seat_run is None


def test_classify_run_flags_bead_id_mismatch_without_changing_outcome():
    line = json.dumps(
        {"outcome": {"status": "done", "summary": "ok", "bead_id": "bh-other"}, "session_id": "s"}
    )
    result = seatrun.classify_run(0, line, bead="bh-mine")
    assert result.outcome is seatrun.RunOutcome.DONE
    assert result.bead_id_mismatch is True


def test_classify_run_no_mismatch_when_bead_ids_agree():
    line = json.dumps(
        {"outcome": {"status": "done", "summary": "ok", "bead_id": "bh-mine"}, "session_id": "s"}
    )
    result = seatrun.classify_run(0, line, bead="bh-mine")
    assert result.bead_id_mismatch is False


def test_classify_run_taxonomy_exit_codes_agree_with_status_when_honored():
    # Not a claim that today's binary sends these codes (it doesn't) — this exercises the
    # cross-check path for a binary (e.g. the reference stub) that DOES honor the target
    # taxonomy, confirming exit code and status agree.
    for status, exit_code in [("done", 0), ("blocked", 10), ("handoff", 11)]:
        line = json.dumps({"outcome": {"status": status, "summary": "x"}, "session_id": "s"})
        result = seatrun.classify_run(exit_code, line)
        assert result.outcome.value == status


# ---- validate_workspace ----


def test_validate_workspace_rejects_empty():
    result = seatrun.validate_workspace("")
    assert not result.ok
    assert "required" in result.reason


def test_validate_workspace_rejects_nonexistent_path(tmp_path):
    missing = str(tmp_path / "does-not-exist-zzz")
    result = seatrun.validate_workspace(missing)
    assert not result.ok
    assert "no such path" in result.reason


def test_validate_workspace_rejects_a_file_not_a_directory(tmp_path):
    f = tmp_path / "a-file"
    f.write_text("hello")
    result = seatrun.validate_workspace(str(f))
    assert not result.ok
    assert "not a directory" in result.reason


def test_validate_workspace_accepts_plain_directory(tmp_path):
    result = seatrun.validate_workspace(str(tmp_path))
    assert result.ok
    assert result.is_git_worktree is False


def test_validate_workspace_recognizes_git_worktree(tmp_path):
    os.makedirs(tmp_path / ".git")
    result = seatrun.validate_workspace(str(tmp_path))
    assert result.ok
    assert result.is_git_worktree is True


# ---- already_advanced — the INVARIANT row, made mechanical ----


@pytest.mark.parametrize("status", ["open", "in_progress"])
def test_already_advanced_false_for_dispatchable_states(status):
    assert seatrun.already_advanced(status) is False


@pytest.mark.parametrize(
    "status", ["review:pending", "review:approved", "review:changes-requested", "merged", "closed"]
)
def test_already_advanced_true_for_states_past_dispatch(status):
    assert seatrun.already_advanced(status) is True
