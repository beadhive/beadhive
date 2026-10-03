"""Complete receive transaction serialization and bounded process cleanup."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beadhive.hq_git_broker import bounded_pack, receive_lock


def test_receive_lock_serializes_across_processes_and_times_out(tmp_path):
    source = str(Path(__file__).parents[1] / "src")
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; from beadhive.hq_git_broker import receive_lock; "
        "import sys;\nwith receive_lock(Path(sys.argv[1]), timeout=0.1): print('entered')",
        str(tmp_path),
    ]
    with receive_lock(tmp_path):
        result = subprocess.run(
            command, env={**os.environ, "PYTHONPATH": source}, capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "lock deadline exceeded" in result.stderr
    result = subprocess.run(
        command, env={**os.environ, "PYTHONPATH": source}, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "entered"


def test_stalled_pack_is_killed_reaped_and_lock_released(tmp_path):
    pidfile = tmp_path / "pid"
    command = [
        sys.executable,
        "-c",
        "import os,time,sys,subprocess; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(child.pid)); time.sleep(60)",
        str(pidfile),
    ]
    left, right = socket.socketpair()
    started = time.monotonic()
    try:
        with receive_lock(tmp_path):
            with pytest.raises(TimeoutError, match="transfer deadline"):
                bounded_pack(command, left, os.environ.copy(), timeout=0.2)
        with receive_lock(tmp_path, timeout=0.1):
            pass
        assert time.monotonic() - started < 2
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text().split()[0]), 0)
        child = Path("/proc") / pidfile.read_text().split()[1] / "stat"
        # An adopted zombie is terminated; no child can keep the lock/transfer alive.
        assert not child.exists() or child.read_text().split()[2] == "Z"
    finally:
        left.close()
        right.close()


def test_broker_requires_linux_credentials_before_startup(tmp_path, monkeypatch):
    from beadhive.hq_git_broker import serve

    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ValueError, match="requires Linux"):
        serve(tmp_path / "missing", tmp_path / "git.sock")
