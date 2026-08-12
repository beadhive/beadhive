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
import time

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


def test_the_sweep_is_a_no_op_when_there_is_nothing_to_reap(tmp_path):
    """Runs at every session start, so the empty case is the common one."""
    assert sweep_orphaned_dolt_servers(tmp_path) == []
