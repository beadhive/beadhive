"""Merge-slot crash-safety + stale-holder reclaim (bh-62ex), and the bh-ytbb.4 host_id migration:
the holder token's `host` field is the stable `host.host_id()` UUID, not `socket.gethostname()`
— a machine rename must not orphan its own slot, and a reused hostname must not let reclaim
steal a live slot from a different machine.

A merge killed mid-run must not leak the hive's exclusive slot and wedge every retry. Two guards:
a signal handler that releases the slot before the process dies, and an acquire that reclaims a
holder whose owning process is gone (or that blew a generous TTL).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import uuid
from collections import namedtuple

import pytest

from beadhive import host
from beadhive import work_group as wg

_CP = namedtuple("CP", "returncode stdout stderr")


@pytest.fixture(autouse=True)
def _minted_host_identity():
    """Merge-slot holder tokens key on `host.host_id()` (bh-ytbb.4) — mint `host.yaml` up
    front, same as `bh config init` does in real use, since the shared `_sandbox_bh_home`
    fixture (tests/conftest.py) only seeds `config.yaml`."""
    host.mint_if_needed()


def _this_host() -> str:
    return host.host_id()


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
    assert fields["host"] == _this_host()
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
    token = f"dev/dead|host={_this_host()}|pid={proc.pid}|ts={2**31}"
    assert wg._holder_is_stale(token) is True


def test_holder_cross_host_uses_ttl():
    """A holder on another host can't be pid-probed → fall back to the TTL backstop."""
    stale = "dev/x|host=other-host-id|pid=1|ts=1"  # ancient ts
    fresh = f"dev/x|host=other-host-id|pid=1|ts={2**31}"  # far-future ts
    assert wg._holder_is_stale(stale) is True
    assert wg._holder_is_stale(fresh) is False


def test_legacy_or_empty_holder_never_reclaimed():
    """A bare/legacy holder carries no liveness info → conservatively never stolen."""
    assert wg._holder_is_stale("dev/legacy") is False
    assert wg._holder_is_stale("") is False
    assert wg._holder_is_stale(None) is False


# ---- bh-ytbb.4: host_id migration regressions --------------------------------


def test_writer_reader_agreement_pid_fast_path_is_reached(monkeypatch):
    """REQUIRED bh-ytbb.4 regression: a holder token `_slot_holder` (the WRITER) stamps must be
    recognized as SAME-HOST by `_holder_is_stale`'s reader comparison. Proven via a
    DISTINGUISHING pid-liveness outcome (forced dead) rather than merely checking a boolean a
    broken TTL-fallback path could also produce for a freshly-acquired token — a
    freshly-acquired token is "not stale" via EITHER the correct same-host pid check OR the
    cross-host TTL fallback (fresh ts), so that boolean alone can't tell a real writer/reader
    agreement from a silent one where they never actually match. This FAILS if either side is
    still on `socket.gethostname()` while the other emits `host.host_id()`: the token's host
    would never equal the reader's comparison value, this would misclassify as cross-host, and
    the (fresh-ts) TTL fallback would say "not stale" instead of the pid-verdict below."""
    monkeypatch.setattr(wg, "_pid_alive", lambda pid: False)  # only matters if REACHED
    token = wg._slot_holder("dev/agreement")  # WRITER: host=host.host_id()
    assert wg._holder_is_stale(token) is True  # only true via the reached same-host pid branch


def test_pid_liveness_fast_path_is_reached_not_merely_correct(monkeypatch):
    """Regression: assert the same-host pid-liveness branch is actually REACHED (spied), not
    just that the final outcome happens to be right — a silent degradation to TTL-only reaping
    (e.g. a writer/reader host mismatch) would give the SAME boolean for a live token but never
    call `_pid_alive` at all."""
    calls = []
    real_pid_alive = wg._pid_alive

    def _spy(pid):
        calls.append(pid)
        return real_pid_alive(pid)

    monkeypatch.setattr(wg, "_pid_alive", _spy)
    token = wg._slot_holder("dev/reached")
    wg._holder_is_stale(token)
    assert calls == [os.getpid()]  # the same-host pid-liveness branch actually ran


def test_merge_slot_ownership_survives_a_machine_rename(monkeypatch):
    """Regression: renaming the machine (its `socket.gethostname()` value changing) between
    acquire and a later staleness check must NOT orphan a slot this process still holds — the
    pid path must still fire, not fall back to the TTL-only cross-host path. Before bh-ytbb.4,
    the holder token embedded `socket.gethostname()`; a hostname change made a live holder look
    cross-host and same-host liveness silently degraded to TTL."""
    token = wg._slot_holder("dev/renamed")  # acquired under the "old" name
    monkeypatch.setattr(socket, "gethostname", lambda: "a-totally-renamed-machine")
    # host_id() is unaffected by the "rename" (unlike the old gethostname()-keyed token) — force
    # a distinguishing dead-pid outcome so a True verdict can only come from the pid branch.
    monkeypatch.setattr(wg, "_pid_alive", lambda pid: False)
    assert wg._holder_is_stale(token) is True


def test_reused_hostname_on_different_host_id_is_not_the_local_holder(monkeypatch):
    """Regression: a DIFFERENT machine's holder token, sharing THIS host's hostname (VM clone,
    cloud image, reused label) but a distinct `host_id`, must never be treated as this process's
    own live slot — even when the token's pid number happens to be alive on this host. Before
    bh-ytbb.4, the comparison used `socket.gethostname()`; a reused hostname made a live
    cross-host holder falsely look same-host, and reclaim could steal it."""

    def _boom(pid):
        raise AssertionError("pid liveness must never be probed for a different host_id")

    monkeypatch.setattr(wg, "_pid_alive", _boom)
    other_host_id = str(uuid.uuid4())  # a different machine's host_id
    stale_ts = 1  # ancient — so a correct cross-host TTL reclaim is the only way to get True
    token = f"dev/elsewhere|host={other_host_id}|pid={os.getpid()}|ts={stale_ts}"
    assert wg._holder_is_stale(token) is True  # reclaimed via the TTL backstop, never pid-probed


# ---- acquire + reclaim ------------------------------------------------------


def test_acquire_reclaims_orphaned_slot():
    """First acquire fails (slot held), the holder is a dead-pid orphan → release + retry wins."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    orphan = f"dev/dead|host={_this_host()}|pid={proc.pid}|ts={2**31}"
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
