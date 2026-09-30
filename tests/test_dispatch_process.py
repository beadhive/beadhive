"""Foreground process dispatch: fencing and actual POSIX signal delivery."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from beadhive import dispatch_hive_run as dhr


def test_fence_is_rechecked_before_each_spawn(tmp_path):
    checks = iter([True, True, False])
    spawned = []
    driver = dhr.HiveDispatchRun(
        hive_dir=tmp_path,
        hive="fixture/hive",
        actor="dev/test",
        sink_path=tmp_path / "dispatch.jsonl",
        pick=lambda: ["one", "two"],
        eligible=lambda: next(checks),
    )

    async def spawn(epic):
        spawned.append(epic)
        return dhr._Child(epic, type("Proc", (), {"returncode": None})())

    driver._spawn = spawn
    asyncio.run(driver.run_pass())
    assert spawned == ["one"]


def test_stopping_never_picks_new_work(tmp_path):
    def pick():
        raise AssertionError("draining dispatcher queried new work")

    driver = dhr.HiveDispatchRun(
        hive_dir=tmp_path,
        hive="fixture/hive",
        actor="dev/test",
        sink_path=tmp_path / "dispatch.jsonl",
        pick=pick,
    )
    driver._on_sigterm()
    assert asyncio.run(driver.run_pass()).idle


def test_process_adapter_fails_closed_without_u3():
    assert dhr.process_eligible("fixture/hive", cfg={}) is False


def test_sigterm_checkpoints_child_and_exits_zero(tmp_path):
    """Send real SIGTERM to the picker; its child saves a checkpoint before exit."""
    ready = tmp_path / "ready"
    checkpoint = tmp_path / "checkpoint"
    code = '''
import asyncio
from pathlib import Path
import sys
from beadhive.dispatch_hive_run import HiveDispatchRun, _Child

root = Path(sys.argv[1])
child_code = """
import signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def stop(*args):
    (root / 'checkpoint').write_text('saved')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
(root / 'ready').write_text('ready')
while True: time.sleep(0.05)
"""
class Driver(HiveDispatchRun):
    async def _spawn(self, epic):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-c', child_code, str(root), start_new_session=True)
        return _Child(epic, proc)

driver = Driver(hive_dir=root, hive='fixture/hive', actor='dev/test',
                sink_path=root / 'sink', pick=lambda: ['one'], eligible=lambda: True,
                poll_interval=300, drain_timeout=2)
asyncio.run(driver.run())
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(dhr.__file__).resolve().parents[1])
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], env=env)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.02)
        assert ready.exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
        assert checkpoint.read_text() == "saved"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_drain_deadline_fences_unresponsive_child_and_reports_failure(tmp_path):
    import pytest

    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(300)",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await proc.stdout.readline()
        driver = dhr.HiveDispatchRun(
            hive_dir=tmp_path,
            hive="fixture/hive",
            actor="dev/test",
            sink_path=tmp_path / "dispatch.jsonl",
            drain_timeout=0.05,
        )
        driver.children["one"] = dhr._Child("one", proc)
        with pytest.raises(RuntimeError, match="checkpoint completion unverified"):
            await driver.shutdown()
        assert proc.returncode == -signal.SIGKILL

    asyncio.run(scenario())
