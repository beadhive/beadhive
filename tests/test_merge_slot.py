"""Merge-slot crash-safety + stale-holder reclaim (bh-62ex).

A merge killed mid-run must not leak the rig's exclusive slot and wedge every retry. Two guards:
a signal handler that releases the slot before the process dies, and an acquire that reclaims a
holder whose owning process is gone (or that blew a generous TTL).
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
from collections import namedtuple

from beadhive import work_group as wg

_CP = namedtuple("CP", "returncode stdout stderr")
_HOST = socket.gethostname()


class _FakeBd:
    """A programmable stand-in for the `bd` seam: canned acquire return codes (popped per call)
    and a fixed `check --json` holder. Records every call's args."""

    def __init__(self, acquire_rcs, holder):
        self.acquire_rcs = list(acquire_rcs)
        self.holder = holder
        self.calls: list[tuple] = []

    def run(self, args, cwd, capture=False, **kw):
        self.calls.append(tuple(args))
        sub = args[1] if len(args) > 1 else ""
        if sub == "acquire":
            rc = self.acquire_rcs.pop(0) if self.acquire_rcs else 1
            return _CP(rc, "", "")
        if sub == "check":
            return _CP(0, json.dumps({"holder": self.holder}), "")
        return _CP(0, "", "")

    def did(self, *needles):
        return any(all(n in call for n in needles) for call in self.calls)


# ---- holder token parsing + staleness ---------------------------------------


def test_slot_holder_token_round_trips():
    token = wg._slot_holder("dev/cleanup-a")
    assert token.startswith("dev/cleanup-a|")
    fields = wg._parse_holder(token)
    assert fields["host"] == _HOST
    assert fields["pid"].isdigit()
    assert fields["ts"].isdigit()


def test_holder_alive_same_host_is_not_stale():
    """This very process holds the slot → never reclaimed."""
    token = wg._slot_holder("dev/live")  # embeds os.getpid(), which is obviously alive
    assert wg._holder_is_stale(token) is False


def test_holder_dead_pid_same_host_is_stale():
    """A same-host holder whose process has exited is an orphan → reclaimable."""
    proc = subprocess.Popen(["true"])
    proc.wait()  # reaped: os.kill(pid, 0) now raises ProcessLookupError
    token = f"dev/dead|host={_HOST}|pid={proc.pid}|ts={2**31}"
    assert wg._holder_is_stale(token) is True


def test_holder_cross_host_uses_ttl():
    """A holder on another host can't be pid-probed → fall back to the TTL backstop."""
    stale = "dev/x|host=other-box|pid=1|ts=1"  # ancient ts
    fresh = f"dev/x|host=other-box|pid=1|ts={2**31}"  # far-future ts
    assert wg._holder_is_stale(stale) is True
    assert wg._holder_is_stale(fresh) is False


def test_legacy_or_empty_holder_never_reclaimed():
    """A bare/legacy holder carries no liveness info → conservatively never stolen."""
    assert wg._holder_is_stale("dev/legacy") is False
    assert wg._holder_is_stale("") is False
    assert wg._holder_is_stale(None) is False


# ---- acquire + reclaim ------------------------------------------------------


def test_acquire_reclaims_orphaned_slot():
    """First acquire fails (slot held), the holder is a dead-pid orphan → release + retry wins."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    orphan = f"dev/dead|host={_HOST}|pid={proc.pid}|ts={2**31}"
    fake = _FakeBd(acquire_rcs=[1, 0], holder=orphan)

    assert wg._acquire_slot(fake, "/main", wg._slot_holder("dev/me")) is True
    assert fake.did("merge-slot", "release")  # reclaimed
    # acquire attempted twice (fail, then win after reclaim)
    assert sum(1 for c in fake.calls if "acquire" in c) == 2


def test_acquire_does_not_reclaim_live_holder():
    """A held slot whose holder is alive is NOT stolen — the acquire fails cleanly, no release."""
    fake = _FakeBd(acquire_rcs=[1], holder=wg._slot_holder("dev/live"))

    assert wg._acquire_slot(fake, "/main", wg._slot_holder("dev/me")) is False
    assert not fake.did("merge-slot", "release")
    assert sum(1 for c in fake.calls if "acquire" in c) == 1  # no retry


# ---- signal handler releases the slot ---------------------------------------


def test_signal_handler_releases_slot_then_restores(monkeypatch):
    """A SIGTERM mid-hold fires the release callback before the process is torn down; handlers are
    restored afterward."""
    released = []
    # neutralize the handler's self-re-raise so the test process survives
    monkeypatch.setattr(wg.os, "kill", lambda pid, sig: None)

    before = signal.getsignal(signal.SIGTERM)
    prev = wg._install_slot_signal_release(lambda: released.append(True))
    handler = signal.getsignal(signal.SIGTERM)
    assert handler is not before  # our handler is installed

    handler(signal.SIGTERM, None)  # simulate the signal delivery
    assert released == [True]

    wg._restore_signal_handlers(prev)
    assert signal.getsignal(signal.SIGTERM) is before
