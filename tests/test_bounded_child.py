"""`run.bounded` — a child that cannot outlive its caller (bh-toitp).

MEASURED 2026-08-07 on beadhive-factory: 31 live `bd -C ~/.beadhive/hq show <~50 ids> --json`
processes, 9.6 GB RSS, oldest 2h12m, second 2h08m, a wave of ten spawned ~10s apart and all
still alive an hour later — every one `ppid=1`, and every one older than ten minutes exited
cleanly on a plain SIGTERM. Nothing was ever signalling them.

These tests use REAL processes, for the same reason `tests/test_localloop.py` does: a mocked
`Popen` will happily "prove" a kill that never reached the process tree, and the whole bead is
about processes that outlived the thing that was supposed to bound them.

The two halves are tested separately because they fail separately:
  * a call that never finishes, with a live caller  -> the TIMEOUT reaps it;
  * a caller that is KILLED mid-call                -> PDEATHSIG reaps it, and nothing else can
    (bh is already dead; no timeout it installed can run).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from beadhive import run as run_mod

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="PDEATHSIG/killpg are Linux")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A reaped child of THIS process lingers as a zombie until waited on; a zombie is not alive.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return False


def _wait_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_a_wedged_child_is_terminated_at_its_bound():
    """The first acceptance criterion: a call that exceeds its timeout is TERMINATED and
    reported as a failure — not left running, not silently dropped."""
    started = time.monotonic()
    with pytest.raises(run_mod.ChildTimeout) as exc:
        run_mod.bounded([sys.executable, "-c", "import time; time.sleep(300)"], timeout=1.0)
    assert time.monotonic() - started < 30, "the bound did not bound anything"
    assert "exceeded 1s" in str(exc.value)
    assert "TERMINATED" in str(exc.value)


def test_the_failure_names_the_call(tmp_path):
    """`label` carries the hive and the verb. 'timed out' on its own put the last reader back to
    enumerating processes by hand with ps, which is how this bead was written."""
    with pytest.raises(run_mod.ChildTimeout) as exc:
        run_mod.bounded(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            timeout=0.5,
            label="hq bd show against /home/bees/.beadhive/hq",
        )
    assert "hq bd show against /home/bees/.beadhive/hq" in str(exc.value)


def test_the_whole_process_GROUP_is_reaped_not_just_the_child():
    """`bd` starts work of its own; signalling only the direct child leaves the grandchild —
    which is exactly the shape `tests/test_localloop.py` measured for seat cancellation
    (`test_naive_terminate_leaks_a_grandchild`). Here the child forks a grandchild that outlives
    it, and both must be gone."""
    script = (
        "import os,sys,time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(300)\n"
        "    os._exit(0)\n"
        "print(pid, flush=True)\n"  # newline-terminated: readline must not block on read(n)
        "time.sleep(300)\n"
    )
    # Run it manually first to learn the grandchild pid, then let `bounded` reap the group.
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        preexec_fn=run_mod._die_with_parent,
    )
    try:
        grandchild = int(proc.stdout.readline().strip())
        assert _alive(grandchild)
        run_mod._reap_group(proc)
        assert _wait_gone(grandchild), "the grandchild outlived the group reap"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_child_dies_when_its_caller_is_KILLED():
    """The measured leak, and the half no timeout can cover: `ppid=1`.

    A consumer's own `execFile(..., {timeout: 10_000})` SIGKILLed `bh` and left the `bd`
    grandchild running — the 10s spacing in the observed waves is exactly that timeout. bh is
    already dead at that point, so nothing it installed can run. PR_SET_PDEATHSIG is the kernel
    doing it instead.
    """
    # A parent that spawns a long-lived child through `bounded`'s own preexec, prints its pid,
    # and then sits. We SIGKILL the parent; the child must go with it.
    src_root = str(run_mod.__file__).rsplit("/beadhive/", 1)[0]
    parent_src = (
        "import subprocess,sys,time\n"
        f"sys.path.insert(0, {src_root!r})\n"
        "from beadhive import run as r\n"
        "p = subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'],"
        " preexec_fn=r._die_with_parent)\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(300)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_src], stdout=subprocess.PIPE, text=True)
    try:
        child_pid = int(parent.stdout.readline().strip())
        assert _alive(child_pid)
        parent.send_signal(signal.SIGKILL)  # cannot be trapped — userspace gets no say
        parent.wait(timeout=10)
        assert _wait_gone(child_pid), (
            "the child outlived a SIGKILLed parent — this is the ppid=1 leak, 31 processes and "
            "9.6 GB of it"
        )
    finally:
        if parent.poll() is None:
            parent.kill()


def test_a_child_that_finishes_in_time_is_untouched():
    """The negative arm. A bound that also breaks the ordinary call is not a bound, it is an
    outage — and it would be switched off within a week."""
    res = run_mod.bounded([sys.executable, "-c", "print('hello')"], timeout=30.0, capture=True)
    assert res.returncode == 0
    assert "hello" in res.stdout


def test_a_missing_binary_is_still_exit_127_not_a_raised_exception():
    """`bounded` keeps `run`'s contract on this exact point (bh-7m2h9): callers are written
    against a returncode, and a raise escaped as an unhandled crash from anything that merely
    READ through bd — `bh doctor` died with a traceback on the broken seat it exists to
    diagnose."""
    res = run_mod.bounded(["definitely-not-a-real-binary-bh"], timeout=5.0, capture=True)
    assert res.returncode == run_mod.MISSING_BINARY_EXIT
    assert run_mod.missing_binary(res) == "definitely-not-a-real-binary-bh"
