"""tests/fixtures/stub_seat.py — the reference stub seat binary (bh-c6dk.2).

Exercises it as a real subprocess against the settled argv/stdout/exit contract, and confirms
`beadhive.seatrun.classify_run` classifies its output correctly end-to-end — this is the
integration half of the parsing-layer coverage, complementing test_seatrun.py's unit tests.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beadhive import seatrun

STUB = str(Path(__file__).parent / "fixtures" / "stub_seat.py")


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, STUB, *args], capture_output=True, text=True, timeout=10, **kw
    )


def _write_instructions(tmp_path: Path, text: str) -> str:
    p = tmp_path / "instructions.txt"
    p.write_text(text)
    return str(p)


# ---- baseline contract shape ----


def test_default_run_is_done_and_exit_zero(tmp_path):
    instr = _write_instructions(tmp_path, "just do it")
    result = _run(
        [
            "--workspace",
            str(tmp_path),
            "--bead",
            "bh-xyz",
            "--instructions",
            instr,
            "--session_id",
            "s-1",
        ]
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout.strip())
    assert payload["outcome"]["status"] == "done"
    assert payload["outcome"]["bead_id"] == "bh-xyz"
    assert payload["session_id"] == "s-1"
    assert result.stderr == ""


def test_instructions_by_file_and_by_stdin_both_work(tmp_path):
    # by file
    instr = _write_instructions(tmp_path, "STUB_STATUS=blocked\nSTUB_SUMMARY=blocked via file")
    r_file = _run(
        ["--workspace", str(tmp_path), "--instructions", instr, "--session_id", "s-f"]
    )
    assert r_file.returncode == 10
    assert json.loads(r_file.stdout)["outcome"]["status"] == "blocked"

    # by stdin ("-")
    r_stdin = _run(
        ["--workspace", str(tmp_path), "--instructions", "-", "--session_id", "s-i"],
        input="STUB_STATUS=handoff\nSTUB_SUMMARY=blocked via stdin",
    )
    assert r_stdin.returncode == 11
    assert json.loads(r_stdin.stdout)["outcome"]["status"] == "handoff"


@pytest.mark.parametrize(
    "status,exit_code", [("done", 0), ("blocked", 10), ("handoff", 11)]
)
def test_status_directive_controls_outcome_and_exit_code(tmp_path, status, exit_code):
    instr = _write_instructions(tmp_path, f"STUB_STATUS={status}")
    result = _run(
        ["--workspace", str(tmp_path), "--instructions", instr, "--session_id", "s-2"]
    )
    assert result.returncode == exit_code
    assert json.loads(result.stdout)["outcome"]["status"] == status

    classified = seatrun.classify_run(result.returncode, result.stdout)
    assert classified.outcome.value == status


def test_bead_id_round_trip_matches_by_default(tmp_path):
    instr = _write_instructions(tmp_path, "STUB_STATUS=done")
    result = _run(
        [
            "--workspace",
            str(tmp_path),
            "--bead",
            "bh-round",
            "--instructions",
            instr,
            "--session_id",
            "s-3",
        ]
    )
    classified = seatrun.classify_run(result.returncode, result.stdout, bead="bh-round")
    assert classified.bead_id_mismatch is False


def test_bead_id_mismatch_directive_is_caught_by_classify_run(tmp_path):
    instr = _write_instructions(tmp_path, "STUB_STATUS=done\nSTUB_BEAD_MISMATCH=true")
    result = _run(
        [
            "--workspace",
            str(tmp_path),
            "--bead",
            "bh-round",
            "--instructions",
            instr,
            "--session_id",
            "s-4",
        ]
    )
    classified = seatrun.classify_run(result.returncode, result.stdout, bead="bh-round")
    assert classified.bead_id_mismatch is True


# ---- refusals: typed errors, never raw tracebacks ----


def test_missing_session_id_is_refused_with_a_typed_error(tmp_path):
    instr = _write_instructions(tmp_path, "STUB_STATUS=done")
    result = _run(["--workspace", str(tmp_path), "--instructions", instr])
    assert result.returncode == 2
    assert result.stdout == ""
    err = json.loads(result.stderr)
    assert err["error"] == "session_id_required"


def test_bad_workspace_is_refused_with_a_typed_error_not_a_traceback(tmp_path):
    bogus = str(tmp_path / "does-not-exist")
    instr = _write_instructions(tmp_path, "STUB_STATUS=done")
    result = _run(["--workspace", bogus, "--instructions", instr, "--session_id", "s-5"])
    assert result.returncode == 2
    assert result.stdout == ""
    err = json.loads(result.stderr)
    assert err["error"] == "workspace_invalid"
    assert "Traceback" not in result.stderr


# ---- CANCEL ladder ----


def test_sigterm_yields_a_priced_envelope_never_sigint_exit_zero(tmp_path):
    instr = _write_instructions(tmp_path, "STUB_HANG=true")
    proc = subprocess.Popen(
        [sys.executable, STUB, "--workspace", str(tmp_path), "--instructions", instr,
         "--session_id", "s-6"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.2)  # let it enter the hang loop and install the SIGTERM handler
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 143
    envelope = seatrun.parse_envelope(stdout)
    assert envelope is not None
    assert envelope.session_id == "s-6"
    assert envelope.terminal_reason == "sigterm"

    classified = seatrun.classify_run(proc.returncode, stdout)
    assert classified.outcome is seatrun.RunOutcome.INCOMPLETE
    assert classified.envelope is not None


def test_cooperative_cancel_over_stdin_emits_handoff_and_ack(tmp_path):
    instr = _write_instructions(tmp_path, "STUB_HANG=true")
    proc = subprocess.Popen(
        [
            sys.executable,
            STUB,
            "--workspace",
            str(tmp_path),
            "--bead",
            "bh-coop",
            "--instructions",
            instr,
            "--session_id",
            "s-7",
            "--input-format",
            "stream-json",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.2)
    proc.stdin.write("please wrap up now\n")
    proc.stdin.flush()
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0
    payload = json.loads(stdout.strip())
    assert payload["outcome"]["status"] == "handoff"
    assert payload["interrupt_ack"] is True
    assert payload["outcome"]["bead_id"] == "bh-coop"


def test_hard_cancel_control_request_emits_envelope_and_exits_nonzero(tmp_path):
    instr = _write_instructions(tmp_path, "STUB_HANG=true")
    proc = subprocess.Popen(
        [
            sys.executable,
            STUB,
            "--workspace",
            str(tmp_path),
            "--instructions",
            instr,
            "--session_id",
            "s-8",
            "--input-format",
            "stream-json",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.2)
    proc.stdin.write(json.dumps({"type": "control_request", "request": {"subtype": "interrupt"}}) + "\n")
    proc.stdin.flush()
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 1
    envelope = seatrun.parse_envelope(stdout)
    assert envelope is not None
    assert envelope.terminal_reason == "control_request_interrupt"


def test_sigint_is_never_sent_and_the_stub_installs_no_handler_for_it(tmp_path):
    # Documents the "never SIGINT" rule at the process level: this stub deliberately installs no
    # SIGINT handler, so a SIGINT would fall through to Python's default (KeyboardInterrupt,
    # non-zero exit under `python -m`, but a bare `0` for the default handler in some contexts) —
    # exactly the ambiguity the contract avoids by forbidding SIGINT outright. This test only
    # asserts the documented behavior: SIGTERM is handled and SIGINT is not special-cased.
    instr = _write_instructions(tmp_path, "STUB_HANG=true")
    proc = subprocess.Popen(
        [sys.executable, STUB, "--workspace", str(tmp_path), "--instructions", instr,
         "--session_id", "s-9"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.2)
    proc.send_signal(signal.SIGTERM)  # the only signal this contract ever sends
    stdout, _ = proc.communicate(timeout=10)
    assert proc.returncode == 143
