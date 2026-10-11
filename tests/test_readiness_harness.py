"""Unit coverage for the readiness helpers in tests/harness/readiness.py."""

from __future__ import annotations

import subprocess
import sys

import pytest

from harness.readiness import ReadyFifo, wait_pid_gone


def test_fifo_delivers_lines_from_a_real_child(tmp_path):
    fifo = ReadyFifo(tmp_path / "ready.fifo")
    code = "import sys; open(sys.argv[1], 'w').write('4242\\n')"
    subprocess.run([sys.executable, "-c", code, str(fifo.path)], check=True, timeout=30)
    assert fifo.wait_pid() == 4242


def test_fifo_times_out_loudly_when_nothing_writes(tmp_path):
    fifo = ReadyFifo(tmp_path / "ready.fifo")
    with pytest.raises(AssertionError, match="readiness line"):
        fifo.wait(timeout=0.2)


def test_wait_pid_gone_blocks_until_exit():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert not wait_pid_gone(proc.pid, 0.2)
    proc.kill()
    proc.wait()
    assert wait_pid_gone(proc.pid, 5)
