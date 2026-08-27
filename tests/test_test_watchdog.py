"""Executable contract for the bounded parallel-gate watchdog."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-only")
def test_timeout_kills_stubborn_descendant_after_leader_exits() -> None:
    grandchild = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "signal.signal(signal.SIGUSR1, signal.SIG_IGN); "
        "time.sleep(300)"
    )
    leader = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable, '-c', {grandchild!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(child.pid, flush=True); "
        "time.sleep(300)"
    )
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
            leader,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
    )
    grandchild_pid = int(result.stdout.strip())
    try:
        deadline = time.monotonic() + 2
        while _process_exists(grandchild_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result.returncode == 124
        assert not _process_exists(grandchild_pid)
    finally:
        if _process_exists(grandchild_pid):
            os.kill(grandchild_pid, signal.SIGKILL)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
