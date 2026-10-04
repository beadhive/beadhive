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


def test_timeout_reports_process_diagnostics_without_signaling_unregistered_python() -> None:
    child = "import time; time.sleep(300)"
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
    assert "requesting registered pytest stacks from 0 process(es)" in diagnostics
    assert "no registered pytest stacks were captured" in diagnostics


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


@pytest.mark.skipif(os.name != "posix", reason="SIGUSR1 diagnostics are POSIX-only")
def test_xdist_timeout_reports_safe_active_tests_and_registered_stacks(tmp_path) -> None:
    secret = "must-not-appear-in-watchdog-output"
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import time\n"
        "import pytest\n\n"
        f"@pytest.mark.parametrize('token', [{secret!r}])\n"
        "def test_hangs(token):\n"
        "    time.sleep(300)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "tests"), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    result = subprocess.run(
        [
            sys.executable,
            str(WATCHDOG),
            "--timeout",
            "8",
            "--grace",
            "0.5",
            "--",
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "2",
            "-p",
            "harness.watchdog_diagnostics",
            str(probe),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 124
    assert "active pytest tests" in diagnostics
    assert "worker gw" in diagnostics
    assert "test_probe.py::test_hangs" in diagnostics
    assert "pytest stack diagnostics" in diagnostics
    assert "test_hangs" in diagnostics
    assert secret not in diagnostics


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


def test_report_root_hosts_owned_diagnostics_and_preserves_unrelated_files(tmp_path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    sentinel = reports / "retained.xml"
    sentinel.write_text("unchanged")
    child = (
        "import os; from pathlib import Path; "
        "root=Path(os.environ['BH_TEST_ACTIVE_DIR']); "
        "(root/'child.stack').write_text('diagnostic'); print(root, flush=True)"
    )
    result = subprocess.run(
        [sys.executable, str(WATCHDOG), "--timeout", "5", "--", sys.executable, "-c", child],
        env={**os.environ, "BH_TEST_REPORT_DIR": str(reports)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    diagnostic_root = Path(result.stdout.strip())
    assert diagnostic_root.parent == reports
    assert diagnostic_root.name.startswith("bh-test-watchdog-")
    assert not diagnostic_root.exists()
    assert sentinel.read_text() == "unchanged"
    assert list(reports.iterdir()) == [sentinel]


def test_report_root_timeout_cleans_only_owned_diagnostics(tmp_path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    sentinel = reports / "retained.xml"
    sentinel.write_text("unchanged")
    child = (
        "import os,time; from pathlib import Path; "
        "root=Path(os.environ['BH_TEST_ACTIVE_DIR']); "
        "(root/'child.stack').write_text('diagnostic'); time.sleep(300)"
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
            "-c",
            child,
        ],
        env={**os.environ, "BH_TEST_REPORT_DIR": str(reports)},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 124
    assert "TEST WATCHDOG TIMEOUT" in result.stderr
    assert sentinel.read_text() == "unchanged"
    assert list(reports.iterdir()) == [sentinel]


@pytest.mark.parametrize("kind", ["relative", "missing", "file", "symlink", "foreign"])
def test_invalid_or_foreign_report_root_fails_before_child_spawn(tmp_path, kind) -> None:
    marker = tmp_path / "spawned"
    reports = tmp_path / "reports"
    if kind == "relative":
        raw = "relative-report-root"
    elif kind == "missing":
        raw = str(reports)
    elif kind == "file":
        reports.write_text("retained")
        raw = str(reports)
    elif kind == "symlink":
        target = tmp_path / "foreign-target"
        target.mkdir()
        (target / "sentinel").write_text("retained")
        reports.symlink_to(target, target_is_directory=True)
        raw = str(reports)
    else:
        if not hasattr(os, "getuid") or Path("/").stat().st_uid == os.getuid():
            pytest.skip("requires an actual foreign-owned root")
        raw = "/"
    child = f"from pathlib import Path; Path({str(marker)!r}).write_text('spawned')"
    result = subprocess.run(
        [sys.executable, str(WATCHDOG), "--timeout", "5", "--", sys.executable, "-c", child],
        env={**os.environ, "BH_TEST_REPORT_DIR": raw},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not marker.exists()
    if kind == "file":
        assert reports.read_text() == "retained"
    elif kind == "symlink":
        assert (target / "sentinel").read_text() == "retained"
        assert list(target.iterdir()) == [target / "sentinel"]
