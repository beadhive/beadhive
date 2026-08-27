"""Executable contract for the bounded parallel-gate watchdog."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "scripts" / "test-watchdog.py"


def test_timeout_dumps_python_and_process_diagnostics_then_fails() -> None:
    child = (
        "import faulthandler,signal,time; "
        "faulthandler.register(signal.SIGUSR1, all_threads=True); "
        "time.sleep(300)"
    )
    started_at = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            str(WATCHDOG),
            "--timeout",
            "0.3",
            "--grace",
            "0.2",
            "--",
            sys.executable,
            "-u",
            "-c",
            child,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
    )
    elapsed = time.monotonic() - started_at
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 124
    assert elapsed < 3
    assert "TEST WATCHDOG TIMEOUT" in diagnostics
    assert "child process diagnostics" in diagnostics
    assert "Current thread" in diagnostics


def test_child_failure_is_preserved_instead_of_turning_green() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(WATCHDOG),
            "--timeout",
            "5",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(17)",
        ],
        cwd=ROOT,
        timeout=5,
    )
    assert result.returncode == 17
