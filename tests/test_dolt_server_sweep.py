"""bh-7wp2y — the session-scoped backstop that reaps dolt sql-servers a teardown never reached.

`harness.world.reap_dolt_server` is a fixture finalizer, and a finalizer does not run when the
pytest process is killed mid-suite: six consecutive sessions each left the same slow real-server
tests running, against tmp dirs pytest had already deleted. So this tests the backstop that lives
OUTSIDE any test's lifecycle, and — more importantly — tests that it cannot reach anything it
should not.

Uses a stand-in `dolt` executable (a shell script that sleeps) rather than a real dolt: the sweep
selects on argv and on whether the `--config` path still exists, and neither of those needs a real
server. No `bd`, no dolt, no ports.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.world import orphaned_dolt_servers, sweep_orphaned_dolt_servers


@pytest.fixture
def fake_dolt(tmp_path):
    """A `dolt` on disk that sleeps, so it can be found by argv and killed like the real one."""
    binary = tmp_path / "bin" / "dolt"
    binary.parent.mkdir(parents=True)
    # NOT `exec sleep`: exec replaces the process image, and with it the argv the sweep selects on.
    binary.write_text("#!/bin/sh\nsleep 120\n")
    binary.chmod(0o755)
    started: list[subprocess.Popen] = []

    def _start(config_path):
        proc = subprocess.Popen([str(binary), "sql-server", "--config", str(config_path)])
        started.append(proc)
        _wait_until_visible(proc.pid)
        return proc

    yield _start
    for proc in started:
        with contextlib.suppress(OSError):
            os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _wait_until_visible(pid: int, timeout: float = 5.0) -> None:
    """`ps` can lag a fresh fork by a beat; poll rather than sleep a guessed constant."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = subprocess.run(["ps", "-p", str(pid)], capture_output=True, text=True)
        if str(pid) in (res.stdout or ""):
            return
        time.sleep(0.05)
    raise TimeoutError(f"pid {pid} never appeared in ps")


def _config(tmp_path, name: str):
    """A server dir holding a config file, shaped like the one bd writes."""
    server_dir = tmp_path / name
    server_dir.mkdir(parents=True)
    cfg = server_dir / "dolt-server-config.yaml"
    cfg.write_text("listener:\n  port: 3308\n")
    return cfg


def test_a_server_whose_config_dir_was_deleted_is_orphaned(tmp_path, fake_dolt):
    """The observed pathology exactly: a live server pointing at a pytest tmp dir that pytest's
    retention policy has already removed."""
    cfg = _config(tmp_path, "gone")
    proc = fake_dolt(cfg)
    shutil.rmtree(cfg.parent)

    found = orphaned_dolt_servers(tmp_path)

    assert proc.pid in [pid for pid, _cfg in found]


def test_a_server_whose_config_still_exists_is_left_alone(tmp_path, fake_dolt):
    """This is what keeps the sweep off a run currently IN FLIGHT — a live session's config is
    still on disk, so it is never a candidate. The limit is deliberate, not incidental."""
    cfg = _config(tmp_path, "live")
    proc = fake_dolt(cfg)

    assert proc.pid not in [pid for pid, _cfg in orphaned_dolt_servers(tmp_path)]


def test_a_server_outside_the_tmp_root_is_never_a_candidate(tmp_path, fake_dolt):
    """The operator's own servers (~/.beads/shared-server, ~/.beadhive/cache/<hive>/.beads) live
    outside the pytest tmp tree. A `pkill -f "dolt sql-server"` would take them with it; scoping
    to the caller's own root is what makes this safe to run unattended at session start."""
    outside = tmp_path / "outside"
    cfg = _config(outside, "operators-real-server")
    proc = fake_dolt(cfg)
    shutil.rmtree(cfg.parent)

    found = orphaned_dolt_servers(tmp_path / "pytest-root")

    assert found == []
    assert proc.poll() is None  # still running, untouched


def test_the_sweep_actually_kills_what_it_reports(tmp_path, fake_dolt):
    """The backstop's whole job. Reports what it reaped so a run says out loud that it cleaned up
    after a previous one, rather than doing it silently."""
    cfg = _config(tmp_path, "gone")
    proc = fake_dolt(cfg)
    shutil.rmtree(cfg.parent)

    killed = sweep_orphaned_dolt_servers(tmp_path)

    assert proc.pid in [pid for pid, _cfg in killed]
    proc.wait(timeout=10)
    assert proc.poll() is not None


def test_a_narrow_COLUMNS_does_not_hide_the_orphan(tmp_path, fake_dolt, monkeypatch):
    """`ps` truncates each command line to $COLUMNS even when its output is a pipe, and pytest
    sets COLUMNS in its xdist workers — so the `--config <path>` this sweep keys on fell off the
    end of the line and the sweep found nothing while reporting success. Caught by these tests
    passing serially and failing under `-n auto`; pinned here because a backstop that silently
    no-ops is worse than no backstop."""
    monkeypatch.setenv("COLUMNS", "80")
    cfg = _config(tmp_path, "gone-under-a-narrow-terminal")
    proc = fake_dolt(cfg)
    shutil.rmtree(cfg.parent)

    assert proc.pid in [pid for pid, _cfg in orphaned_dolt_servers(tmp_path)]


def test_the_sweep_is_a_no_op_when_there_is_nothing_to_reap(tmp_path):
    """Runs at every session start, so the empty case is the common one."""
    assert sweep_orphaned_dolt_servers(tmp_path) == []


def test_xdist_controller_reaps_prior_session_orphan_at_start(tmp_path, fake_dolt):
    """The plugin hook runs on the controller before xdist workers start."""
    cfg = _config(tmp_path, "prior-session")
    proc = fake_dolt(cfg)
    shutil.rmtree(cfg.parent)
    repo = Path(__file__).parents[1]

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "-q",
            "-s",
            "tests/unit/test_pure_module_independence.py::test_operation_catalog_import_has_no_ambient_or_runtime_dependencies",
            "--basetemp",
            str(tmp_path / "current-session"),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dolt sweep (session start): reaped 1 orphaned sql-server(s)" in result.stdout
    proc.wait(timeout=10)


def test_xdist_controller_reaps_worker_orphan_at_end(tmp_path, fake_dolt):
    """The plugin hook runs on the controller after xdist workers have stopped."""
    repo = Path(__file__).parents[1]
    pid_path = tmp_path / "worker-orphan.pid"
    child_test = tmp_path / "test_create_worker_orphan.py"
    child_test.write_text(
        """\
import os
import shutil
import subprocess
import time
from pathlib import Path

def test_create_worker_orphan(tmp_path):
    cfg = tmp_path / "server" / "dolt-server-config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("listener:\\n  port: 3308\\n")
    proc = subprocess.Popen(
        [os.environ["BH_TEST_FAKE_DOLT"], "sql-server", "--config", str(cfg)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        seen = subprocess.run(["ps", "-p", str(proc.pid)], capture_output=True, text=True)
        if str(proc.pid) in seen.stdout:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"worker orphan {proc.pid} never appeared in ps")
    Path(os.environ["BH_TEST_ORPHAN_PID"]).write_text(str(proc.pid))
    shutil.rmtree(cfg.parent)
"""
    )
    environment = dict(os.environ)
    environment["BH_TEST_FAKE_DOLT"] = str(tmp_path / "bin" / "dolt")
    environment["BH_TEST_ORPHAN_PID"] = str(pid_path)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(repo / "tests"), str(repo / "src"), environment.get("PYTHONPATH", "")]
    )

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "stateful_fixtures",
                "-n",
                "2",
                "-q",
                "-s",
                str(child_test),
                "--basetemp",
                str(tmp_path / "current-session"),
            ],
            cwd=repo,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "dolt sweep (session end): reaped 1 orphaned sql-server(s)" in result.stdout
    finally:
        if pid_path.is_file():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
