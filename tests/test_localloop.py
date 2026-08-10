"""The `local` work-runtime tier (bh-c6dk.5) — process supervision, the CANCEL ladder, the pass.

These tests use REAL processes for everything about process supervision, because that is the
only way to prove the thing the bead is actually about. bh-a7so.2 §3 measured a supervisor that
believed it had cancelled while a live, spending, worktree-mutating agent kept running as an
init-reparented orphan; a mocked `Process` would happily "prove" a kill that never reached the
process tree. So :func:`test_naive_terminate_leaks_a_grandchild` reproduces the failure mode and
:func:`test_cancel_reaps_the_whole_process_group` proves the fix against the same script.

`bd` is faked (a `FakeBd` shaped after `tests/test_work_next.py`'s, for the same reason: bd's
real behavior on the paths under test is a return code and a JSON blob). The REAL-bd properties
— an open human gate holding a bead back, restart-without-double-claim, `bd reclaim` as backstop
— live in `tests/test_localloop_int.py`, where a mocked `bd` could not prove them.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beadhive import bd as bd_mod
from beadhive import localloop, seatrun, work_next

STUB_SEAT = Path(__file__).parent / "fixtures" / "stub_seat.py"


def async_test(fn):
    """Run an `async def` test on a fresh event loop.

    A local three-liner rather than a `pytest-asyncio` dependency: this is the only async test
    module in the suite, every test here wants its OWN loop (several spawn real process groups
    and must not inherit another test's reaped children), and `asyncio.run` gives exactly that
    with nothing to configure and no plugin whose default-loop-scope semantics change under it.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---- helpers ---------------------------------------------------------------------------------


def _instructions(tmp_path: Path, name: str, *directives: str) -> Path:
    path = tmp_path / f"{name}.md"
    path.write_text("\n".join(directives) + "\n")
    return path


def _stub_argv(tmp_path: Path, instructions: Path, *, bead="b1", session="sess-1", stream=True):
    argv = [
        sys.executable,
        str(STUB_SEAT),
        "--workspace",
        str(tmp_path),
        "--bead",
        bead,
        "--instructions",
        str(instructions),
        "--session_id",
        session,
    ]
    if stream:
        argv += ["--input-format", "stream-json"]
    return argv


async def _spawn(
    argv, *, bead="b1", role="developer", action="dispatch", session="sess-1", cwd=None
):
    return await localloop.spawn_seat(
        argv, bead_id=bead, role=role, action=action, session_id=session, cwd=cwd
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _await_seat_ready(seat, *, startup: float = 1.0) -> None:
    """Give a freshly spawned seat time to install its signal handlers before signalling it.

    Not a fudge: a process cannot catch SIGTERM before its interpreter has started, and a signal
    that arrives first kills it outright (exit -15, no envelope). The real seat binary has the
    same startup window — it is why `work.dispatch.envelope_grace` is generous and why the
    scheduler holds the pipe rather than assuming a prompt reply.
    """
    while seat.age() < startup and not seat.finished:
        await asyncio.sleep(0.05)


async def _await_file(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


#: A parent that forks a grandchild which OUTLIVES it, then exits cleanly on SIGTERM without
#: touching the grandchild. This is bh-a7so.2 §3's shape in miniature: the supervisor's signal
#: reaches the direct child, the grandchild reparents to init and keeps running.
_ORPHAN_MAKER = """\
import os, signal, sys, time
pidfile = sys.argv[1]
pid = os.fork()
if pid == 0:                      # the grandchild: ignores SIGTERM, outlives its parent
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.05)
    os._exit(0)
open(pidfile, "w").write(str(pid))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # exits WITHOUT killing the grandchild
while True:
    time.sleep(0.05)
"""


# ---- HARD REQUIREMENT 1: process-group termination -------------------------------------------


@async_test
async def test_naive_terminate_leaks_a_grandchild(tmp_path):
    """The measured failure mode, reproduced: `proc.terminate()` signals the DIRECT CHILD only.

    This is the control for the next test. Without it, "the group kill works" is unfalsifiable —
    a script whose grandchild happened to die on its own would pass either way.
    """
    script = tmp_path / "orphan.py"
    script.write_text(_ORPHAN_MAKER)
    pidfile = tmp_path / "grandchild.pid"
    seat = await _spawn([sys.executable, str(script), str(pidfile)])
    grandchild = int(await _await_file(pidfile))

    seat.proc.terminate()  # the naive path the ADR sketch implied
    assert await seat.wait_exit(10), "the direct child must have died — only the grandchild leaks"

    assert _pid_alive(grandchild), (
        "the grandchild must still be alive — this test exists to prove the naive kill leaks, "
        "and if it ever stops leaking the group-kill test below is no longer proving anything"
    )
    # Do not leave it spending: reap through the group, which is exactly the fix.
    await localloop.reap_group(seat, grace=5.0)
    assert not _pid_alive(grandchild)


@async_test
async def test_cancel_reaps_the_whole_process_group(tmp_path):
    """The fix: SIGTERM the child, then `os.killpg` the group — no PPID-1 orphan survives."""
    script = tmp_path / "orphan.py"
    script.write_text(_ORPHAN_MAKER)
    pidfile = tmp_path / "grandchild.pid"
    seat = await _spawn([sys.executable, str(script), str(pidfile)])
    grandchild = int(await _await_file(pidfile))
    assert seat.pgid == seat.pid, "start_new_session=True must make the child its own group leader"

    result = await localloop.cancel(
        seat, rungs=(localloop.RUNG_SIGNAL,), envelope_grace=2.0, terminate_grace=5.0
    )

    assert result.reap.group_gone is True
    assert not _pid_alive(grandchild), "an orphaned grandchild survived the cancel"
    assert result.reap.sent_sigterm is True, "the group SIGTERM is what caught the grandchild"
    # SIGKILL was needed here because the grandchild ignores SIGTERM — and the reaper POLLED
    # rather than assuming the first signal worked.
    assert result.reap.sent_sigkill is True


@async_test
async def test_spawn_puts_each_seat_in_its_own_session(tmp_path):
    inst = _instructions(tmp_path, "i", "STUB_STATUS=done")
    seat = await _spawn(_stub_argv(tmp_path, inst))
    assert os.getsid(seat.pid) == seat.pid != os.getsid(os.getpid())
    await seat.collect()


# ---- HARD REQUIREMENT: never SIGINT ----------------------------------------------------------


def test_send_signal_refuses_sigint():
    """A SIGINT-cancelled run exits 0, colliding head-on with the contract's `0 = done`
    (bh-a7so.7 §4). Enforced as a raise, so it cannot be reintroduced by a plausible-looking
    edit that a code review waves through."""
    with pytest.raises(localloop.ForbiddenSignal, match="SIGINT"):
        localloop.send_signal(os.getpid(), signal.SIGINT, group=False)
    with pytest.raises(localloop.ForbiddenSignal):
        localloop.send_signal(os.getpgid(0), signal.SIGINT, group=True)


@async_test
async def test_cancel_never_sends_sigint(tmp_path):
    """Every signal the ladder sends is recorded on the seat; SIGINT appears in none of them."""
    inst = _instructions(tmp_path, "hang", "STUB_HANG=true")
    seat = await _spawn(_stub_argv(tmp_path, inst))
    await _await_seat_ready(seat)
    result = await localloop.cancel(
        seat, rungs=(localloop.RUNG_SIGNAL,), envelope_grace=5.0, terminate_grace=5.0
    )
    assert result.signals, "the signal rung must have signalled something"
    assert not any("SIGINT" in s for s in result.signals)
    assert result.exit_code == 143, "SIGTERM exits 143, which lands in 'did not complete'"


# ---- HARD REQUIREMENT 2: hold the pipe, read the envelope, THEN reap -------------------------


@async_test
async def test_signal_rung_yields_a_priced_envelope_not_zero_bytes(tmp_path):
    """Signal the child while the reader is still alive and the envelope arrives (bh-a7so.7 §4,
    correcting bh-a7so.2's "a killed run emits zero bytes"). A cancelled run must be
    attributable: session id, cost, and a machine-readable terminal_reason."""
    inst = _instructions(tmp_path, "hang", "STUB_HANG=true")
    seat = await _spawn(_stub_argv(tmp_path, inst, session="sess-kill"))
    await _await_seat_ready(seat)
    result = await localloop.cancel(
        seat, rungs=(localloop.RUNG_SIGNAL,), envelope_grace=5.0, terminate_grace=5.0
    )

    assert result.stdout.strip(), "zero bytes means the pipe was dropped before the envelope"
    assert result.priced is True
    assert result.session_id == "sess-kill"
    assert result.cost_usd > 0
    assert result.classification.outcome is seatrun.RunOutcome.INCOMPLETE
    assert result.classification.envelope.terminal_reason == "sigterm"


@async_test
async def test_cooperative_rung_gets_a_clean_handoff_seatrun(tmp_path):
    """Rung 1: the wrap-up trigger over stream-json stdin. The seat finishes, acks, and exits 0
    with a real `SeatRun` — so a cooperative cancel is a `handoff` result, not a failure."""
    inst = _instructions(tmp_path, "hang", "STUB_HANG=true")
    seat = await _spawn(_stub_argv(tmp_path, inst, session="sess-coop"))
    result = await localloop.cancel(seat, cooperative_grace=10.0, envelope_grace=5.0)

    assert result.rung == localloop.RUNG_COOPERATIVE
    assert result.exit_code == 0
    assert result.classification.outcome is seatrun.RunOutcome.HANDOFF
    assert result.session_id == "sess-coop"
    assert json.loads(result.stdout)["interrupt_ack"] is True
    assert result.reap.group_gone is True, "the reaper still runs — it is the floor, not the path"


@async_test
async def test_hard_rung_is_out_of_band_and_still_priced(tmp_path):
    """Rung 2: `control_request` interrupt. Out-of-band, so it cannot be declined; leaves the
    tree dirty and comes back as a priced envelope rather than a clean outcome."""
    inst = _instructions(tmp_path, "hang", "STUB_HANG=true")
    seat = await _spawn(_stub_argv(tmp_path, inst, session="sess-hard"))
    result = await localloop.cancel(
        seat, rungs=(localloop.RUNG_HARD, localloop.RUNG_SIGNAL), hard_grace=10.0
    )

    assert result.rung == localloop.RUNG_HARD
    assert result.exit_code == 1
    assert result.priced is True
    assert result.classification.envelope.terminal_reason == "control_request_interrupt"


def test_the_ladder_is_the_documented_order():
    assert localloop.CANCEL_LADDER == ("cooperative", "hard", "signal")


# ---- the caps (pure) --------------------------------------------------------------------------


def test_admit_bounds_concurrency():
    caps = localloop.Caps(max_concurrency=2)
    assert localloop.admit(caps, in_flight=0).allowed is True
    assert localloop.admit(caps, in_flight=1).allowed is True
    denied = localloop.admit(caps, in_flight=2)
    assert (denied.allowed, denied.reason) == (False, localloop.ADMIT_AT_CONCURRENCY_CAP)
    assert denied.detail, "a deny reason is SURFACED, never silent (the bh-h2yc failure mode)"


def test_admit_puts_a_lost_lease_ahead_of_a_free_slot():
    """Work that cannot be landed must not be started no matter how much room there is."""
    verdict = localloop.admit(localloop.Caps(max_concurrency=8), in_flight=0, lease_held=False)
    assert (verdict.allowed, verdict.reason) == (False, localloop.ADMIT_LEASE_LOST)


def test_admit_refuses_while_halted():
    verdict = localloop.admit(localloop.Caps(), in_flight=0, halted=True)
    assert (verdict.allowed, verdict.reason) == (False, localloop.ADMIT_HALTED)


def test_admission_reason_set_is_closed():
    with pytest.raises(ValueError, match="unknown admission reason"):
        localloop.Admission(False, "made-up")


def test_wall_time_cap_is_pure_and_disableable():
    caps = localloop.Caps(max_run_seconds=60)
    assert localloop.over_wall_time(caps, 59) is False
    assert localloop.over_wall_time(caps, 60) is True
    assert localloop.over_wall_time(localloop.Caps(max_run_seconds=0), 10**9) is False


def test_admit_is_the_seam_a_caps_module_plugs_into():
    """bh-e7r9q's `dispatch_caps.check_admission` is not in this tree and must not be imported;
    the loop takes an `admit=` callable so wiring it later is a constructor argument."""
    calls = []

    def stub_admit(caps, *, in_flight, lease_held=True, halted=False):
        calls.append((in_flight, lease_held, halted))
        return localloop.Admission(False, localloop.ADMIT_AT_CONCURRENCY_CAP, "stubbed")

    loop = localloop.LocalLoop(hive_dir=Path("."), epic="e", actor="disp/x", admit=stub_admit)
    assert loop._admit is stub_admit
    assert stub_admit(loop.caps, in_flight=0).reason == localloop.ADMIT_AT_CONCURRENCY_CAP
    assert calls


# ---- failure causes go to beads ---------------------------------------------------------------


class FakeBd:
    """Records `bd` invocations and answers the handful of reads the loop performs."""

    def __init__(self, *, epic_status="in_progress", children=(), events=None, ready=()):
        self.epic_status = epic_status
        self.children = [dict(c) for c in children]
        self.events = {k: list(v) for k, v in (events or {}).items()}
        self.ready = list(ready)
        self.calls: list[list[str]] = []
        self.list_args: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        args = list(cmd[1:])
        while args and args[0] in ("-C", "--actor"):
            args = args[2:]
        args = [a for a in args if a != "--json"]
        self.calls.append(args)
        return self._dispatch(args)

    def _dispatch(self, args):
        sub = args[0] if args else ""
        if sub == "gate":
            return _CP(0, json.dumps({"checked": 0, "resolved": 0, "escalated": 0, "errors": 0}))
        if sub == "reclaim":
            return _CP(0, json.dumps({"count": 0, "reclaimed": []}))
        if sub == "heartbeat":
            return _CP(0, json.dumps({"id": args[1], "status": "heartbeat"}))
        if sub == "show":
            return _CP(0, json.dumps({"id": args[1], "status": self.epic_status}))
        if sub == "list" and "--parent" in args:
            self.list_args.append(args)
            parent = args[args.index("--parent") + 1]
            if parent == "epic-1":
                return _CP(0, json.dumps(self.children))
            return _CP(0, json.dumps(self.events.get(parent, [])))
        return _CP(0, "")

    def written_states(self):
        return [(c[1], c[2]) for c in self.calls if c and c[0] == "set-state"]


class _CP:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def fakebd(monkeypatch):
    def install(fake):
        monkeypatch.setattr(bd_mod, "_run", fake)
        return fake

    return install


def test_record_cause_writes_an_event_bead_via_set_state(tmp_path, fakebd):
    fake = fakebd(FakeBd())
    assert localloop.record_cause(tmp_path, "b1", localloop.CAUSE_FAILED, reason="boom") is True
    assert fake.written_states() == [("b1", "dispatch=failed")]
    call = [c for c in fake.calls if c[0] == "set-state"][0]
    assert "--reason" in call and "boom" in call


def test_record_cause_rejects_a_value_outside_the_closed_set(tmp_path, fakebd):
    fakebd(FakeBd())
    with pytest.raises(ValueError, match="closed"):
        localloop.record_cause(tmp_path, "b1", "whatever-i-like", reason="x")


def test_a_done_run_writes_nothing_write_on_failure_not_on_attempt():
    """Event beads are permanent and this hive has no compaction tier, so a successful dispatch
    must add no rows at all. Only bounces, stalls and escalations are recorded."""
    done = seatrun.classify_run(0, json.dumps({"outcome": {"status": "done"}, "session_id": "s"}))
    assert localloop.LocalLoop._cause_for(done) == ""


@pytest.mark.parametrize(
    ("status", "cause"),
    [("blocked", localloop.CAUSE_BLOCKED), ("handoff", localloop.CAUSE_HANDOFF)],
)
def test_a_judgment_result_records_its_cause(status, cause):
    cls = seatrun.classify_run(0, json.dumps({"outcome": {"status": status}, "session_id": "s"}))
    assert localloop.LocalLoop._cause_for(cls) == cause


def test_an_unparseable_run_records_failure():
    assert localloop.LocalLoop._cause_for(seatrun.classify_run(1, "")) == localloop.CAUSE_FAILED


def test_a_bead_mismatch_is_a_failure_even_when_the_status_says_done():
    cls = seatrun.classify_run(
        0,
        json.dumps({"outcome": {"status": "done", "bead_id": "other"}, "session_id": "s"}),
        bead="b1",
    )
    assert localloop.LocalLoop._cause_for(cls) == localloop.CAUSE_MISMATCH


# ---- the pass ----------------------------------------------------------------------------------


def _child(bead_id, **kw):
    return {"id": bead_id, "status": "open", "issue_type": "task", "labels": [], **kw}


def _loop(tmp_path, **kw):
    kw.setdefault("hive_dir", tmp_path)
    kw.setdefault("epic", "epic-1")
    kw.setdefault("actor", "disp/loop")
    kw.setdefault("seat_command", f"{sys.executable} {STUB_SEAT}")
    kw.setdefault("instructions", None)
    if kw["instructions"] is None:
        inst = tmp_path / "brief.md"
        inst.write_text("STUB_STATUS=done\n")
        kw["instructions"] = lambda _a, _b, _r: str(inst)
    return localloop.LocalLoop(**kw)


@async_test
async def test_a_pass_dispatches_through_the_atomic_claim_verb(tmp_path, fakebd):
    """The decision table says there is dispatchable room; `bh work next` says WHICH bead this
    loop actually holds. The loop must never re-derive the pick-then-claim race itself."""
    fakebd(FakeBd(children=[_child("b1"), _child("b2")]))
    claimed = iter([localloop.ClaimResult(claimed="b1", worktree=str(tmp_path))])
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=1),
        claim=lambda: next(claimed, localloop.ClaimResult(reason="empty_queue")),
    )
    report = await loop.run_pass()

    assert report.decision.action == "dispatch"
    assert report.dispatched == ("b1",)
    assert set(loop.in_flight) == {"b1"}
    await loop.shutdown()


@async_test
async def test_the_loop_never_spawns_two_processes_for_one_bead(tmp_path, fakebd):
    fakebd(FakeBd(children=[_child("b1")]))
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=4),
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=str(tmp_path)),
    )
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    first = loop.in_flight["b1"].pid
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    assert loop.in_flight["b1"].pid == first
    assert report.dispatched == ("b1",)
    await loop.shutdown()


@async_test
async def test_concurrency_is_bounded_by_the_config_knob(tmp_path, fakebd):
    fakebd(FakeBd(children=[_child("b1"), _child("b2"), _child("b3")]))
    beads = iter(["b1", "b2", "b3"])
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=2),
        claim=lambda: localloop.ClaimResult(claimed=next(beads, ""), worktree=str(tmp_path)),
        instructions=lambda a, b, r: str(_instructions(tmp_path, f"{b}", "STUB_HANG=true")),
    )
    await loop.run_pass()
    assert len(loop.in_flight) == 2, "the third bead must wait for a slot"
    await loop.shutdown()


@async_test
async def test_a_finished_run_is_harvested_and_its_group_reaped(tmp_path, fakebd):
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    loop = _loop(tmp_path, caps=localloop.Caps(max_concurrency=1))
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    seat = loop.in_flight["b1"]
    await seat.collect()

    harvest = localloop.PassReport()
    await loop._harvest(harvest)
    assert harvest.harvested == (("b1", "done"),)
    assert "b1" not in loop.in_flight
    assert localloop.group_alive(seat.pgid) is False
    assert fake.written_states() == [], "a done run writes nothing"


@async_test
async def test_a_failed_run_writes_its_cause_to_the_bead(tmp_path, fakebd):
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=1),
        instructions=lambda a, b, r: str(
            _instructions(tmp_path, "blocked", "STUB_STATUS=blocked", "STUB_SUMMARY=needs a call")
        ),
    )
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    await loop.in_flight["b1"].collect()
    harvest = localloop.PassReport()
    await loop._harvest(harvest)

    assert harvest.harvested == (("b1", "blocked"),)
    assert harvest.causes == (("b1", localloop.CAUSE_BLOCKED),)
    assert fake.written_states() == [("b1", "dispatch=blocked")]
    reason = [c for c in fake.calls if c[0] == "set-state"][0]
    assert "needs a call" in " ".join(reason), "the seat's own summary is the recorded cause"


@async_test
async def test_the_wall_time_cap_cancels_through_the_ladder_and_prices_the_run(tmp_path, fakebd):
    """A capped-out run is cancelled, not merely abandoned — and it still comes back attributable
    because the cap goes through the same ladder as any other cancel."""
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=1, max_run_seconds=0.01),
        instructions=lambda a, b, r: str(_instructions(tmp_path, "hang", "STUB_HANG=true")),
    )
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    seat = loop.in_flight["b1"]
    await asyncio.sleep(0.05)

    capped = localloop.PassReport()
    await loop._enforce_wall_time(capped)
    assert capped.cancelled and capped.cancelled[0][0] == "b1"
    assert capped.causes == (("b1", localloop.CAUSE_CANCELLED),)
    assert localloop.group_alive(seat.pgid) is False
    assert ("b1", "dispatch=cancelled") in fake.written_states()
    # the claim is released NOW, not left to age out over the 5-minute lease TTL
    assert any(c[:2] == ["update", "b1"] and "--status" in c for c in fake.calls)


@async_test
async def test_a_cancelled_run_releases_its_claim_without_waiting_out_the_ttl(tmp_path, fakebd):
    """`bd reclaim` is the BACKSTOP for a holder that died, not the cancellation path."""
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    loop = _loop(
        tmp_path,
        instructions=lambda a, b, r: str(_instructions(tmp_path, "hang", "STUB_HANG=true")),
    )
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    await loop.shutdown()
    releases = [c for c in fake.calls if c[:2] == ["update", "b1"]]
    assert releases, "shutdown must release the claim rather than leave it to the reaper"
    assert "open" in releases[0] and "--assignee" in releases[0]


# ---- sibling notification ----------------------------------------------------------------------


@async_test
async def test_siblings_are_notified_by_the_loop_not_by_the_child(tmp_path, fakebd):
    """The child has no topology (one bead, one worktree, by design) and no outbound channel to
    a sibling. The loop holds the in-flight map, so it writes to their stdin pipes directly
    (bh-a7so.7 §14)."""
    fakebd(FakeBd(children=[_child("b1"), _child("b2")]))
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=2),
        instructions=lambda a, b, r: str(_instructions(tmp_path, f"hang-{b}", "STUB_HANG=true")),
    )
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    await loop._spawn_for("b2", action="dispatch", role="developer", report=report)

    await _await_seat_ready(loop.in_flight["b2"])
    notified = await loop.notify_siblings("b1", "sibling b1 was cancelled")
    assert notified == ("b2",)
    # the stub treats any non-control line as the cooperative wrap-up, so b2 winds down cleanly.
    # Generous timeout on purpose: this test spawns two real interpreters and the suite runs
    # under `-n auto`, so a tight bound would measure machine load rather than the behavior.
    sibling = loop.in_flight["b2"]
    assert await sibling.wait_exit(60), "the notified sibling should have wrapped up and exited"
    assert sibling.proc.returncode == 0
    await loop.shutdown()


# ---- the host lease ------------------------------------------------------------------------------


class RecordingKeeper:
    def __init__(self, *, held=True):
        self.held = held
        self.active_flags: list[bool] = []

    def renew(self, *, active):
        self.active_flags.append(active)
        return localloop.LeaseStatus(
            held=self.held, renewed=active and self.held, detail="recording keeper"
        )


@async_test
async def test_the_lease_is_renewed_while_workers_are_active_and_not_when_idle(tmp_path, fakebd):
    """Observed 2026-08-10, not theorised: a seat run outlived the 30-minute TTL and every submit
    then refused on a stale fence. An idle host letting its lease lapse IS the intended handoff,
    so renewal is conditioned on having work in flight."""
    fakebd(FakeBd(children=[_child("b1")]))
    keeper = RecordingKeeper()
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=1),
        lease=keeper,
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=str(tmp_path)),
        instructions=lambda a, b, r: str(_instructions(tmp_path, "hang", "STUB_HANG=true")),
    )
    await loop.run_pass()  # idle at the top of pass 1: nothing to renew for
    assert keeper.active_flags == [False]
    await loop.run_pass()  # b1 is in flight now: renew
    assert keeper.active_flags == [False, True]
    await loop.shutdown()


@async_test
async def test_a_seat_outliving_the_renew_interval_keeps_the_lease_alive(tmp_path, fakebd):
    """Drive a run past several renewal opportunities and assert the lease never lapses — the
    exact failure this requirement was written from."""
    fakebd(FakeBd(children=[_child("b1")]))
    keeper = RecordingKeeper()
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=1),
        poll_interval=0,
        lease=keeper,
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=str(tmp_path)),
        instructions=lambda a, b, r: str(_instructions(tmp_path, "hang", "STUB_HANG=true")),
    )
    reports = await loop.run(max_passes=4)
    renewals = [r for r in reports if r.lease.renewed]
    assert len(renewals) >= 3, "every pass with a seat in flight must renew"
    assert all(r.lease.held for r in reports)


@async_test
async def test_losing_the_lease_mid_flight_stops_dispatch_and_escalates(tmp_path, fakebd):
    """Handled explicitly rather than left to surface as a submit refusal — bh-tfapu leaves the
    epoch fence inoperable, so nothing else will stop this loop."""
    fake = fakebd(FakeBd(children=[_child("b1"), _child("b2")]))
    keeper = RecordingKeeper(held=False)
    claims = 0

    def claim():
        nonlocal claims
        claims += 1
        return localloop.ClaimResult(claimed=f"b{claims}", worktree=str(tmp_path))

    loop = _loop(tmp_path, caps=localloop.Caps(max_concurrency=2), lease=keeper, claim=claim)
    report = await loop.run_pass()

    assert report.lease.held is False
    assert loop.halted is True
    assert report.dispatched == (), "never keep spawning seats whose work cannot be landed"
    assert ("epic-1", "dispatch=lease-lost") in fake.written_states()


# ---- the decision table drives the seat ----------------------------------------------------------


@async_test
async def test_every_dispatchable_action_maps_to_exactly_one_seat_role():
    """A loop that improvised a role would be re-acquiring the judgement the table removes."""
    spawning = set(localloop.ROLE_FOR_ACTION)
    non_spawning = {"done", "halt", "wait", "escalate"}
    assert spawning | non_spawning == set(work_next.ACTIONS)
    assert not spawning & non_spawning


@async_test
async def test_an_escalate_decision_writes_the_cause_and_halts(tmp_path, fakebd):
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    loop = _loop(tmp_path)
    report = localloop.PassReport()
    decision = work_next.Decision(
        "deadlock-escalate", "escalate", beads=("b1",), reason="deadlock", detail="stuck"
    )
    await loop._act(decision, report, room=2)
    assert loop.halted is True
    assert fake.written_states() == [("b1", "dispatch=escalated")]


@async_test
async def test_a_done_decision_ends_the_loop(tmp_path, fakebd):
    fakebd(FakeBd(epic_status="closed", children=[]))
    loop = _loop(tmp_path)
    reports = await loop.run(max_passes=5)
    assert len(reports) == 1
    assert reports[0].decision.row == "done"
    assert loop.done is True


@async_test
async def test_the_molecule_is_re_derived_from_bd_every_pass(tmp_path, fakebd):
    """Nothing is cached between passes on purpose: it is what makes a restart a no-op, because
    a fresh process's first pass sees exactly what the dead process's next pass would have."""
    fake = fakebd(FakeBd(children=[_child("b1")]))
    loop = _loop(tmp_path, claim=lambda: localloop.ClaimResult(reason="empty_queue"))
    await loop.run_pass()
    reads = len([c for c in fake.calls if c[0] in ("show", "list")])
    await loop.run_pass()
    assert len([c for c in fake.calls if c[0] in ("show", "list")]) == reads * 2


@async_test
async def test_event_beads_are_read_with_all_because_they_are_created_closed(tmp_path, fakebd):
    """Measured while building the demo, not defensive: `bd list` hides closed issues by default
    and every state-change event bead is created CLOSED. Without `--all` the derived retry count
    is zero forever and the loop-breaker can never fire — a dispatcher that never gives up."""
    fake = fakebd(FakeBd(children=[_child("b1")]))
    loop = _loop(tmp_path, claim=lambda: localloop.ClaimResult(reason="empty_queue"))
    loop.load_molecule(budget=1)
    assert fake.list_args, "the molecule must be re-derived from bd"
    assert all("--all" in args for args in fake.list_args)
    assert all("--include-infra" in args for args in fake.list_args)


# ---- shutdown ------------------------------------------------------------------------------------


@async_test
async def test_shutdown_terminates_children_through_the_group_and_unclaims(tmp_path, fakebd):
    """SIGTERM to the loop itself lands here: every child dies through its GROUP, each unclaims
    its bead, and nothing is left spending."""
    fake = fakebd(FakeBd(children=[_child("b1"), _child("b2")]))
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=2),
        instructions=lambda a, b, r: str(_instructions(tmp_path, f"hang-{b}", "STUB_HANG=true")),
    )
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    await loop._spawn_for("b2", action="dispatch", role="developer", report=report)
    pgids = [s.pgid for s in loop.in_flight.values()]

    await loop.shutdown()

    assert loop.in_flight == {}
    assert not any(localloop.group_alive(p) for p in pgids)
    assert {b for b, _ in fake.written_states()} == {"b1", "b2"}
    assert [c for c in fake.calls if c[:2] == ["update", "b1"]]


# ---- LocalRuntime (the protocol) --------------------------------------------------------------


def test_local_runtime_schedules_observes_and_is_idempotent(tmp_path):
    from beadhive import runtime as runtime_mod

    inst = _instructions(tmp_path, "i", "STUB_STATUS=done")
    rt = localloop.LocalRuntime(seat_command=f"{sys.executable} {STUB_SEAT}")
    assert isinstance(rt, runtime_mod.Runtime)

    handle = rt.schedule(
        "b1", "developer", workspace=str(tmp_path), instructions=str(inst), session_id="s1"
    )
    again = rt.schedule(
        "b1", "developer", workspace=str(tmp_path), instructions=str(inst), session_id="s2"
    )
    assert again.session_id == handle.session_id == "s1", "a second schedule is not a second run"

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        outcome = rt.observe(handle)
        if outcome.status != "running":
            break
        time.sleep(0.05)
    assert outcome.status == "done"


def test_local_runtime_rejects_a_bad_workspace_with_a_typed_error(tmp_path):
    rt = localloop.LocalRuntime(seat_command=f"{sys.executable} {STUB_SEAT}")
    with pytest.raises(ValueError, match="no such path"):
        rt.schedule(
            "b1",
            "developer",
            workspace=str(tmp_path / "nope"),
            instructions=str(tmp_path),
            session_id="s",
        )


def test_local_runtime_gate_hook_is_a_documented_noop():
    """Every tier gets asked; a poll loop that will notice anyway may legitimately do nothing."""
    localloop.LocalRuntime().on_gate_resolved("gate-1")


# ---- argv shape --------------------------------------------------------------------------------


def test_seat_argv_matches_the_settled_contract():
    argv = localloop.seat_argv(
        "bh-{role}",
        "developer",
        workspace="/w",
        bead="b1",
        instructions="/i.md",
        session_id="s1",
        model="opus",
    )
    assert argv[0] == "bh-developer"
    assert argv[1:9] == (
        "--workspace",
        "/w",
        "--bead",
        "b1",
        "--instructions",
        "/i.md",
        "--session_id",
        "s1",
    )
    assert "--model" in argv and "opus" in argv
    assert "--input-format" in argv, "rungs 1 and 2 need the stream-json channel"


def test_seat_argv_honors_a_multiword_command_template():
    argv = localloop.seat_argv(
        "uv run seat --role {role}",
        "merger",
        workspace="/w",
        bead="b",
        instructions="/i",
        session_id="s",
    )
    assert argv[:5] == ("uv", "run", "seat", "--role", "merger")


# ---- the demo is isolated ------------------------------------------------------------------------


def test_the_demo_refuses_to_touch_real_state(tmp_path):
    """The demo stands up a scratch hive in a temp dir and must NEVER reach `~/.beadhive`, a
    registered hive's `.beads/`, or this repo's own beads. That discipline is what made the
    bh-00cq spike evidence trustworthy, so it is asserted rather than trusted."""
    demo = Path(__file__).resolve().parents[1] / "scripts" / "demo_local_loop.py"
    assert demo.is_file()
    res = subprocess.run(
        [sys.executable, str(demo), "--check-isolation-only", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "isolation verified" in res.stdout
