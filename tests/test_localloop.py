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
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import bd as bd_mod
from beadhive import config, localloop, seatrun, state, work_next
from beadhive.complexity import ComplexityTier
from beadhive.model_routing import ModelBlockedVerdict, ModelSelection

STUB_SEAT = Path(__file__).parent / "fixtures" / "stub_seat.py"


def _model_selection(
    model="anthropic/claude-sonnet-4-5", *, role="developer", harness="claude", warnings=()
):
    return ModelSelection(
        required_tier=ComplexityTier.COMPLEX,
        preferred_model=None,
        selected_model=model,
        policy="loose",
        role=role,
        harness=harness,
        endpoint=None,
        availability_source="explicit_configuration",
        selection_reason="least-overpowered available model covering the required complexity",
        warnings=tuple(warnings),
    )


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


def test_orphan_discovery_ignores_a_matching_bead_outside_its_hive_scope():
    """The unit selection must retain the cross-hive reap fence proven end to end elsewhere."""
    ps_output = "\n".join(
        (
            "101 101 seat --workspace /hives/other --bead bh-1 --session_id other",
            "202 202 seat --workspace /hives/ours --bead bh-1 --session_id ours",
        )
    )

    found = localloop.find_orphan_seats(["bh-1"], scope="/hives/ours", ps_output=ps_output)

    assert [(pid, pgid) for pid, pgid, _argv in found] == [(202, 202)]


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


async def _finish(seat) -> None:
    """Wait until the seat is genuinely reaped, not merely done writing.

    EOF on stdout and `returncode` becoming non-None are separate events: the pipe closes when
    the process execs out, the return code appears when the child watcher reaps it a moment
    later. A test that harvested on EOF alone would flake, and `_harvest` is right to skip a run
    that has not been reaped yet — it will take it on the next pass.
    """
    await seat.collect()
    assert await seat.wait_exit(10), "the seat should have exited"


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
    """`admit` is the ONE admission decision core (`dispatch_caps.py` was a second one with zero
    production callers and has been deleted). The loop still takes an `admit=` callable, so a
    later richer governor — `work.dispatch.budget`, bh-3yoh — is a constructor argument rather
    than a change to the loop body."""
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
            # Answer a CHILD show from the child rows (the escalation latch reads labels off the
            # bead); fall back to the epic's own row otherwise.
            row = next((c for c in self.children if c.get("id") == args[1]), None)
            if row is not None:
                return _CP(0, json.dumps(row))
            return _CP(0, json.dumps({"id": args[1], "status": self.epic_status}))
        if sub == "list" and "--parent" in args:
            self.list_args.append(args)
            parent = args[args.index("--parent") + 1]
            if parent == "epic-1":
                return _CP(0, json.dumps(self.children))
            return _CP(0, json.dumps(self.events.get(parent, [])))
        if sub == "ready":
            # `bd` is the authority on blocking, INCLUDING dependencies outside the molecule that
            # the decision table cannot see (bh-sh6yt). `ready` defaulting to empty would make
            # every claimable-set assertion vacuously true, so tests that care pass it explicitly.
            return _CP(0, json.dumps([dict(r) for r in self.ready]))
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
    assert fake.written_states() == [("b1", "dispatch=run_failed")]
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


def _ws(tmp_path, bead):
    """A stand-in for the bead's own managed worktree — DISTINCT from the main clone.

    Not cosmetic: the loop refuses to spawn a `developer` seat whose workspace resolves to the
    main clone, because that is the integration branch and a developer seat commits. A fixture
    that handed both the same path would be asserting against a configuration the loop is
    required to reject."""
    path = tmp_path / "wt" / bead
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _loop(tmp_path, **kw):
    kw.setdefault("hive_dir", tmp_path)
    kw.setdefault("workspace_for", lambda bead: _ws(tmp_path, bead))
    kw.setdefault("epic", "epic-1")
    kw.setdefault("actor", "disp/loop")
    kw.setdefault("seat_command", f"{sys.executable} {STUB_SEAT}")
    # Existing loop-mechanics tests isolate process behavior from routing. Routing-specific tests
    # below inject a concrete shared decision explicitly.
    kw.setdefault("routing", lambda _bead, _role: None)
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
    claimed = iter([localloop.ClaimResult(claimed="b1", worktree=_ws(tmp_path, "b1"))])
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
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=_ws(tmp_path, "b1")),
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
        claim=lambda: localloop.ClaimResult(
            claimed=(b := next(beads, "")), worktree=_ws(tmp_path, b or "none")
        ),
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
    await _finish(seat)

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
    await _finish(loop.in_flight["b1"])
    harvest = localloop.PassReport()
    await loop._harvest(harvest)

    assert harvest.harvested == (("b1", "blocked"),)
    assert harvest.causes == (("b1", localloop.CAUSE_BLOCKED),)
    assert fake.written_states() == [("b1", "dispatch=run_blocked")]
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
    assert ("b1", "dispatch=run_cancelled") in fake.written_states()
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
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=_ws(tmp_path, "b1")),
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
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=_ws(tmp_path, "b1")),
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
        return localloop.ClaimResult(claimed=f"b{claims}", worktree=_ws(tmp_path, "b1"))

    loop = _loop(tmp_path, caps=localloop.Caps(max_concurrency=2), lease=keeper, claim=claim)
    report = await loop.run_pass()

    assert report.lease.held is False
    assert loop.halted is True
    assert report.dispatched == (), "never keep spawning seats whose work cannot be landed"
    assert ("epic-1", "dispatch=run_lease_lost") in fake.written_states()


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
    # The cause AND the latch: `escalation=raised` is what makes the second identical escalation
    # a no-op (see the next test), so both writes are part of the contract.
    assert fake.written_states() == [("b1", "dispatch=escalated"), ("b1", "escalation=raised")]


@async_test
async def test_escalate_is_a_no_op_while_an_escalation_is_already_open(tmp_path, fakebd):
    """The loop-breaker escalates, halts, exits 1 — and the picker respawns it. Without a latch
    that is one PERMANENT event bead per cycle (~8,640/day at the default 10s poll) in a hive
    where `bd compact`/`bd flatten` are forbidden until bh-3vs6c lands. The second escalation
    carries no information a human does not already have, so it must not be written."""
    fake = fakebd(
        FakeBd(children=[_child("b1", status="in_progress", labels=["escalation:raised"])])
    )
    loop = _loop(tmp_path)
    report = localloop.PassReport()
    decision = work_next.Decision(
        "deadlock-escalate", "escalate", beads=("b1",), reason="deadlock", detail="stuck"
    )
    await loop._act(decision, report, room=2)

    assert loop.halted is True, "the halt still happens — only the WRITE is suppressed"
    assert fake.written_states() == []
    assert report.causes == ()


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

    def reads():
        return len([c for c in fake.calls if c[0] in ("show", "list")])

    await loop.run_pass()  # pass 1 also runs the one-off startup orphan scan
    before = reads()
    await loop.run_pass()
    steady = reads() - before
    await loop.run_pass()
    assert reads() - before == steady * 2, "each steady-state pass re-reads the same world"
    assert steady > 0


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


# ---- dry-run: decide-only (bh-3xl60) --------------------------------------------------------


@async_test
async def test_act_refuses_to_run_under_dry_run(tmp_path, fakebd):
    """THE ENFORCED BACKSTOP, proved rather than assumed (bh-bwcxx's lesson): even called
    directly — bypassing `run_pass`'s own control flow, which never reaches this in dry_run —
    `_act` refuses to claim/spawn/write rather than silently doing it."""
    fakebd(FakeBd(children=[_child("b1")]))
    loop = _loop(tmp_path, dry_run=True)
    report = localloop.PassReport(dry_run=True)
    decision = work_next.Decision(
        "dispatch-up-to-budget", "dispatch", beads=("b1",), reason="", detail="d"
    )
    with pytest.raises(RuntimeError, match="dry-run"):
        await loop._act(decision, report, room=1)


@async_test
async def test_a_dry_pass_never_calls_a_mutating_bd_verb(tmp_path, fakebd):
    """THE HARD REQUIREMENT: a dry pass provably mutates nothing — not just "no exception". The
    decision itself is real (what WOULD dispatch is visible), but nothing was claimed, nothing
    is in flight, and every `bd` call the loop made is one of the read-only verbs; `reclaim` /
    `heartbeat` / `set-state` / `update` / `claim` never appear."""
    fake = fakebd(FakeBd(children=[_child("b1"), _child("b2")]))
    loop = _loop(tmp_path, dry_run=True, caps=localloop.Caps(max_concurrency=2))

    report = await loop.run_pass()

    assert report.decision.action == "dispatch", "the decision itself is still real"
    assert report.decision.beads, "what WOULD dispatch is visible on the decision"
    assert report.dispatched == (), "but nothing was actually dispatched"
    assert loop.in_flight == {}
    verbs = {c[0] for c in fake.calls if c}
    # `ready` joined the allowed set with the claimable-set read (bh-sh6yt) — it is a pure query,
    # and it is what lets `--dry-run` name the beads it could really claim rather than only the
    # budget bound. Anything NOT on this list is a write and fails the pass.
    assert verbs <= {"gate", "show", "list", "ready"}, f"a dry pass wrote through: {fake.calls}"
    assert fake.written_states() == []


@async_test
async def test_a_dry_pass_forwards_dry_run_to_gate_check(tmp_path, fakebd):
    """`bd gate check --dry-run` is the real read-only evaluation `coordination.gate_check`
    already exposes — reused verbatim rather than reimplemented."""
    fake = fakebd(FakeBd(children=[]))
    loop = _loop(tmp_path, dry_run=True)
    await loop.run_pass()
    gate_calls = [c for c in fake.calls if c and c[0] == "gate"]
    assert gate_calls and "--dry-run" in gate_calls[0]


@async_test
async def test_a_dry_pass_report_is_stamped_dry_run_true(tmp_path, fakebd):
    """LOUD on the record itself (bh-3xl60), not only in a banner — a `dispatch_pass` read a
    week later must never be mistaken for a real pass."""
    fakebd(FakeBd(children=[]))
    loop = _loop(tmp_path, dry_run=True)
    report = await loop.run_pass()
    assert report.dry_run is True
    assert report.as_dict()["dry_run"] is True


def test_a_real_pass_report_is_not_stamped_dry_run():
    assert localloop.PassReport().dry_run is False
    assert localloop.PassReport().as_dict()["dry_run"] is False


@async_test
async def test_a_dry_pass_skips_reclaim_and_the_startup_orphan_scan(tmp_path, fakebd):
    """`bd reclaim` has no read-only mode of its own and reverts stale claims — a write — so a
    decide-only pass skips it outright rather than run it "for real"; same for the once-per-
    startup orphan reap, which sends real signals to real processes."""
    fake = fakebd(FakeBd(children=[]))
    loop = _loop(tmp_path, dry_run=True)
    report = await loop.run_pass()
    assert report.reclaimed == ()
    assert report.orphans_reaped == ()
    assert not any(c and c[0] == "reclaim" for c in fake.calls)


@async_test
async def test_a_dry_pass_still_checks_the_lease(tmp_path, fakebd):
    """It must still hold the lease check: a dry pass that skipped it would report what a loop
    WOULD do in a state it could not legally be in — but a dry pass never has anything in
    flight, so the check must be a pure READ (never renews)."""
    fakebd(FakeBd(children=[]))
    calls = []

    class _DenyingLease:
        def renew(self, *, active):
            calls.append(active)
            return localloop.LeaseStatus(held=False, renewed=False, detail="held elsewhere")

    loop = _loop(tmp_path, dry_run=True, lease=_DenyingLease())
    report = await loop.run_pass()
    assert calls == [False], "a dry pass never has anything in flight to make renewal active"
    assert report.lease.held is False
    assert loop.halted is True


# ---- molecule scope: the loop claims inside its own epic (bh-sh6yt, P0) ----------------------
#
# The loop delegates the claim to `bh work next` on purpose — re-deriving the pick-then-claim
# race in-process is what an unattended driver must not do. Before `--epic` existed, delegating
# the claim also delegated the SCOPE: a loop pointed at a two-bead molecule spawned live seats
# for beads in other molecules and flipped them to in_progress.


def test_the_default_claim_scopes_bh_work_next_to_this_loop_s_epic(tmp_path, monkeypatch):
    """The scope must ride on the DEFAULT claim. Every loop test below injects `claim=`, so a
    regression here would be invisible to all of them — this is the only test that exercises the
    wiring an operator actually runs."""
    seen = {}

    def _capture(hive_dir, actor, **kw):
        seen.update(hive_dir=hive_dir, actor=actor, **kw)
        return localloop.ClaimResult(reason="empty_queue")

    monkeypatch.setattr(localloop, "bh_work_next", _capture)
    loop = _loop(tmp_path, epic="epic-1")

    loop._claim()

    assert seen["epic"] == "epic-1", "the loop must bound WHICH bead it takes, not only how many"


def test_bh_work_next_passes_epic_through_to_the_verb(tmp_path, monkeypatch):
    """...and the scope must reach the subprocess argv, not stop at the Python signature."""
    argvs = []

    def _run(argv, **_kw):
        argvs.append(list(argv))
        return _CP(0, json.dumps({"status": "declined", "reason": "empty_queue"}))

    monkeypatch.setattr("beadhive.run.run", _run)

    localloop.bh_work_next(tmp_path, "disp/loop", epic="epic-1")

    assert argvs[0][:5] == ["bh", "work", "next", "--json", "--epic"]
    assert argvs[0][5] == "epic-1"


def test_bh_work_next_omits_epic_when_unscoped(tmp_path, monkeypatch):
    """An unscoped call must not grow an empty `--epic ''`, which would filter to nothing."""
    argvs = []

    def _run(argv, **_kw):
        argvs.append(list(argv))
        return _CP(0, json.dumps({"status": "declined", "reason": "empty_queue"}))

    monkeypatch.setattr("beadhive.run.run", _run)

    localloop.bh_work_next(tmp_path, "disp/loop")

    assert "--epic" not in argvs[0]


# ---- --dry-run reports what it could CLAIM, not just the count bound (bh-sh6yt) ---------------


@async_test
async def test_a_dry_pass_reports_the_claimable_set_not_only_the_count_bound(tmp_path, fakebd):
    """`decision.beads` is the budget bound and legitimately names beads `bd` reports as BLOCKED
    by an out-of-molecule dependency (`work_next._ready` defers to `bd ready` rather than invent
    a deadlock). Read as a dispatch plan it misleads — so a dry pass reports both, labelled."""
    fakebd(
        FakeBd(
            children=[_child("b1"), _child("b2")],
            ready=[{"id": "b1"}],  # b2 is blocked by something outside this molecule
        )
    )
    loop = _loop(tmp_path, dry_run=True, caps=localloop.Caps(max_concurrency=2))

    report = await loop.run_pass()

    assert report.decision.action == "dispatch"
    assert set(report.decision.beads) == {"b1", "b2"}, "the count bound is unchanged"
    assert report.claimable == ("b1",), "only what `bd` agrees is ready could really be claimed"
    assert report.as_dict()["claimable"] == ["b1"]


@async_test
async def test_a_real_pass_does_not_pay_for_the_claimable_read(tmp_path, fakebd):
    """On a live pass the claim verb answers this authoritatively, so an extra `bd ready` per
    pass buys nothing."""
    fake = fakebd(FakeBd(children=[_child("b1")], ready=[{"id": "b1"}]))
    loop = _loop(
        tmp_path,
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=_ws(tmp_path, "b1")),
    )

    report = await loop.run_pass()

    assert report.claimable == ()
    assert not any(c and c[0] == "ready" for c in fake.calls)
    await loop.shutdown()


@async_test
async def test_claimable_is_empty_for_an_action_that_names_beads_already_held(tmp_path, fakebd):
    """Only `dispatch` involves a claim that could be allowed or refused. Every other action
    names beads the loop already holds, where "claimable" is a category error."""
    fakebd(FakeBd(children=[_child("b1", status="in_progress")], ready=[{"id": "b1"}]))
    loop = _loop(tmp_path, dry_run=True)

    report = await loop.run_pass()

    assert report.decision.action != "dispatch"
    assert report.claimable == ()


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
    with localloop.LocalRuntime(seat_command=f"{sys.executable} {STUB_SEAT}") as rt:
        assert isinstance(rt, runtime_mod.Runtime)

        handle = rt.schedule(
            "b1", "developer", workspace=str(tmp_path), instructions=str(inst), session_id="s1"
        )
        again = rt.schedule(
            "b1", "developer", workspace=str(tmp_path), instructions=str(inst), session_id="s2"
        )
        assert again.session_id == handle.session_id == "s1", (
            "a second schedule is not a second run"
        )

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            outcome = rt.observe(handle)
            if outcome.status != "running":
                break
            time.sleep(0.05)
        assert outcome.status == "done"


@pytest.mark.parametrize(
    ("harness", "canonical_model", "argv_model"),
    [
        ("claude", "anthropic/claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("opencode", "openai/gpt-5", "openai/gpt-5"),
    ],
)
def test_local_runtime_translates_canonical_model_only_at_launch_and_reports_routing(
    tmp_path, harness, canonical_model, argv_model
):
    inst = _instructions(tmp_path, "routing", "STUB_STATUS=done")
    with localloop.LocalRuntime(
        seat_command=f"{sys.executable} {STUB_SEAT}", harness=harness
    ) as rt:
        decision = _model_selection(canonical_model, harness=harness)
        handle = rt.schedule(
            "b1",
            "developer",
            workspace=str(tmp_path),
            instructions=str(inst),
            session_id="routing-1",
            decision=decision,
        )
        seat = rt._runs["b1"]
        model_index = seat.argv.index("--model")
        assert seat.argv[model_index + 1] == argv_model
        assert seat.routing["selected_model"] == canonical_model
        assert "launch_model" not in seat.routing

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            outcome = rt.observe(handle)
            if outcome.status != "running":
                break
            time.sleep(0.05)
        assert outcome.status == "done"
        assert outcome.routing["complexity"] == "COMPLEX"
        assert outcome.routing["selected_model"] == canonical_model
        assert "launch_model" not in outcome.routing


def test_local_runtime_close_is_idempotent_and_joins_its_loop(tmp_path):
    inst = _instructions(tmp_path, "close", "STUB_HANG=true")
    rt = localloop.LocalRuntime(
        seat_command=f"{sys.executable} {STUB_SEAT}", terminate_grace=0.2, envelope_grace=0.2
    )
    rt.schedule("b1", "developer", workspace=str(tmp_path), instructions=str(inst), session_id="s")
    thread = rt._thread
    seat = rt._runs["b1"]

    rt.close()
    rt.close()

    assert thread is not None and not thread.is_alive()
    assert not localloop.group_alive(seat.pgid)
    assert rt._loop is None
    assert rt._thread is None


def test_local_runtime_close_is_bounded_and_reaps_when_shutdown_stalls(tmp_path, monkeypatch):
    inst = _instructions(tmp_path, "stalled-close", "STUB_HANG=true")
    rt = localloop.LocalRuntime(
        seat_command=f"{sys.executable} {STUB_SEAT}", terminate_grace=0.1, envelope_grace=0.0
    )
    rt.schedule("b1", "developer", workspace=str(tmp_path), instructions=str(inst), session_id="s")
    thread = rt._thread
    seat = rt._runs["b1"]

    async def never_finishes():
        await asyncio.Event().wait()

    monkeypatch.setattr(rt, "_shutdown_runs", never_finishes)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="cleanup timed out"):
        rt.close()

    assert time.monotonic() - started < 2.5
    assert thread is not None and not thread.is_alive()
    assert not localloop.group_alive(seat.pgid)


def test_local_runtime_shutdown_attempts_every_seat_and_rejects_a_surviving_group(monkeypatch):
    rt = localloop.LocalRuntime()
    first = SimpleNamespace(finished=True, bead_id="first", pgid=101)
    second = SimpleNamespace(finished=True, bead_id="second", pgid=202)
    rt._runs = {"first": first, "second": second}
    attempted = []

    async def collect(_seat, _grace):
        return ""

    async def reap(seat, *, grace):
        attempted.append(seat.bead_id)
        if seat is first:
            raise PermissionError("first cleanup denied")
        return localloop.ReapResult(group_gone=False)

    monkeypatch.setattr(localloop, "_collect_with_timeout", collect)
    monkeypatch.setattr(localloop, "reap_group", reap)

    with pytest.raises(RuntimeError, match=r"first.*cleanup failed.*second.*survived cleanup"):
        asyncio.run(rt._shutdown_runs())

    assert attempted == ["first", "first", "second"]
    assert rt._runs == {}


def test_local_runtime_strict_block_prevents_launch_with_full_remediation(tmp_path):
    rt = localloop.LocalRuntime(seat_command=f"{sys.executable} {STUB_SEAT}")
    blocked = ModelBlockedVerdict(
        required_tier=ComplexityTier.REASONING,
        preferred_model="openai/missing",
        policy="strict",
        role="developer",
        harness="claude",
        availability_source="gateway_live",
        reason="preferred model openai/missing is unavailable",
        remediation="configure an available route",
    )
    with pytest.raises(
        ValueError,
        match=r"b1.*REASONING.*openai/missing.*gateway_live.*configure an available route",
    ):
        rt.schedule(
            "b1",
            "developer",
            workspace=str(tmp_path),
            instructions=str(tmp_path),
            session_id="blocked",
            decision=blocked,
        )
    assert "b1" not in rt._runs


def test_routing_telemetry_records_decision_without_endpoint_details():
    decision = replace(
        _model_selection(),
        preferred_model="anthropic/claude-sonnet-4-5",
        endpoint="https://gateway.example/private-tenant-token/v1",
        availability_source="gateway_live",
    )
    attrs = localloop.LocalLoop._routing_attributes(decision)
    assert attrs == {
        "bh.routing.required_complexity": "COMPLEX",
        "bh.routing.preferred_model": "anthropic/claude-sonnet-4-5",
        "bh.routing.selected_model": "anthropic/claude-sonnet-4-5",
        "bh.routing.availability_source": "gateway_live",
        "bh.routing.selection_reason": decision.selection_reason,
        "bh.routing.endpoint_source": "gateway",
    }
    assert "private-tenant-token" not in repr(attrs)


@pytest.mark.parametrize("role", ["developer", "dispatcher"])
@async_test
async def test_poll_loop_launches_shared_decision_for_leaf_and_nested_dispatcher(
    tmp_path, fakebd, role
):
    fakebd(FakeBd(children=[_child("b1")]))
    decision = _model_selection(role=role, warnings=("loose fallback is visible",))
    loop = _loop(tmp_path, routing=lambda _bead, _role: decision)
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role=role, report=report)

    seat = loop.in_flight["b1"]
    model_index = seat.argv.index("--model")
    assert seat.argv[model_index + 1] == "claude-sonnet-4-5"
    assert report.as_dict()["routing"]["b1"]["selected_model"] == decision.selected_model
    assert report.as_dict()["routing"]["b1"]["warnings"] == ["loose fallback is visible"]
    assert "launch_model" not in report.as_dict()["routing"]["b1"]
    await loop.shutdown()


@async_test
async def test_poll_loop_strict_block_records_evidence_and_never_spawns(tmp_path, fakebd):
    fakebd(FakeBd(children=[_child("b1")]))
    blocked = ModelBlockedVerdict(
        required_tier=ComplexityTier.REASONING,
        preferred_model="openai/missing",
        policy="strict",
        role="developer",
        harness="claude",
        availability_source="gateway_live",
        reason="preferred model openai/missing is unavailable",
        remediation="configure an available route",
    )
    loop = _loop(tmp_path, routing=lambda _bead, _role: blocked)
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)

    assert loop.in_flight == {}
    assert report.dispatched == ()
    assert report.causes == (("b1", localloop.CAUSE_BLOCKED),)
    payload = report.as_dict()["routing"]["b1"]
    assert payload["complexity"] == "REASONING"
    assert payload["preferred_model"] == "openai/missing"
    assert payload["availability_source"] == "gateway_live"
    assert payload["remediation"] == "configure an available route"


@async_test
async def test_resume_reuses_original_launch_identity_without_refresh_or_double_spawn(
    tmp_path, fakebd
):
    fakebd(FakeBd(children=[_child("b1")]))
    decisions = iter([_model_selection(), _model_selection("anthropic/claude-opus-4-1")])
    calls = []

    def resolve(bead, role):
        calls.append((bead, role))
        return next(decisions)

    loop = _loop(tmp_path, routing=resolve)
    first = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=first)
    # An availability refresh while the first run is live cannot spawn or re-resolve.
    await loop._spawn_for("b1", action="dispatch", role="developer", report=first)
    assert len(calls) == 1

    deadline = time.monotonic() + 15
    while not loop.in_flight["b1"].finished and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    await loop._harvest(localloop.PassReport())
    resumed = localloop.PassReport()
    await loop._spawn_for("b1", action="resume", role="developer", report=resumed)
    assert len(calls) == 1
    seat = loop.in_flight["b1"]
    assert seat.argv[seat.argv.index("--model") + 1] == "claude-sonnet-4-5"
    await loop.shutdown()


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


# ---- a dispatched seat must be able to ACT (bh-xrg1f, P0) ------------------------------------
#
# A seat spawned with no `--bundle` resolves baml-harness's `bare_seat`: permission_mode "plan"
# plus a closed roster (`ask: ["Bash(*)"]`, a refusal under headless `-p`). Every seat the loop
# spawned was therefore default-closed — it could reason and never act, burning a full model
# turn per bead to produce `run_blocked` and then re-dispatching into the same denial. The
# packed seat CLI exposes no `--permission-mode`, so `--bundle` is the ONLY lever bh has.


def test_seat_argv_passes_the_bundle_before_the_contract_flags():
    argv = localloop.seat_argv(
        "bh-{role}",
        "developer",
        workspace="/w",
        bead="b1",
        instructions="/i.md",
        session_id="s1",
        bundle="/b.json",
    )
    assert argv[:3] == ("bh-developer", "--bundle", "/b.json")
    assert "--workspace" in argv, "the Amendment 2 contract flags still follow"


def test_seat_argv_without_a_bundle_is_byte_for_byte_what_it_always_was():
    """The stub seat and `seat_bundle: "-"` both spawn bare, and must keep working."""
    common = dict(workspace="/w", bead="b1", instructions="/i.md", session_id="s1")
    assert localloop.seat_argv("bh-{role}", "developer", **common) == localloop.seat_argv(
        "bh-{role}", "developer", bundle="", **common
    )


def test_a_bundle_named_in_the_command_template_wins():
    """The documented workaround while this was broken was
    `seat_command: "bh-{role} --bundle <path>"`. Passing ours too would hand the seat two
    `--bundle` flags to choose between."""
    argv = localloop.seat_argv(
        "bh-{role} --bundle /operators/own.json",
        "developer",
        workspace="/w",
        bead="b1",
        instructions="/i.md",
        session_id="s1",
        bundle="/bh/default.json",
    )
    assert argv.count("--bundle") == 1
    assert "/operators/own.json" in argv
    assert "/bh/default.json" not in argv


@async_test
async def test_the_loop_spawns_its_seats_with_the_configured_bundle(tmp_path, fakebd):
    """End of the wiring: what the loop actually spawns, not just what `seat_argv` can build."""
    fakebd(FakeBd(children=[_child("b1")]))
    spawned = []
    loop = _loop(
        tmp_path,
        seat_bundle="/bh/default.json",
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=_ws(tmp_path, "b1")),
    )
    real_spawn = localloop.spawn_seat

    async def _record(argv, **kw):
        spawned.append(list(argv))
        return await real_spawn(argv, **kw)

    localloop.spawn_seat = _record
    try:
        await loop.run_pass()
    finally:
        localloop.spawn_seat = real_spawn
    await loop.shutdown()

    assert spawned, "the pass must have spawned a seat"
    assert "--bundle" in spawned[0] and "/bh/default.json" in spawned[0]


# ---- bh's shipped seat bundle ----------------------------------------------------------------


def _shipped_bundle():
    return json.loads(config.asset("seat-bundle.json").read_text())


def test_the_shipped_bundle_covers_every_role_the_loop_can_spawn():
    """A missing seat is not a soft failure: `resolve_bundle_seat` PANICS on a role the bundle
    does not declare, so an unlisted role turns a working dispatch into a dead one."""
    declared = set(_shipped_bundle()["seats"])
    assert set(localloop.ROLE_FOR_ACTION.values()) <= declared


def test_every_shipped_seat_declares_a_non_empty_roster():
    """A seat that declares no permissions — or three empty buckets — falls back to
    `closed_rules()`, which is the very default this bundle exists to replace. Declaring a mode
    and nothing else would look fixed and behave exactly as broken."""
    for role, spec in _shipped_bundle()["seats"].items():
        perms = spec.get("permissions") or {}
        buckets = (perms.get("allow") or []) + (perms.get("ask") or []) + (perms.get("deny") or [])
        assert buckets, f"{role} would resolve back to closed_rules()"


def test_no_shipped_seat_asks_because_nobody_is_at_the_terminal():
    """Under headless `-p` an `ask` is a refusal, so an ask rule is a denial with a friendly
    name. This is the specific shape that made the original failure hard to read."""
    for role, spec in _shipped_bundle()["seats"].items():
        assert not (spec.get("permissions") or {}).get("ask"), f"{role} would stall on an ask"


def test_read_only_seats_are_read_only_by_DENY_not_by_mode():
    """Operator direction 2026-08-12: every seat runs `auto`, because `plan` also blocks tool
    calls a reviewer legitimately needs. That moves the reviewer/warden read-only property off
    the mode and onto the roster — and only `deny` can carry it, since Claude Code evaluates
    deny -> ask -> allow and `auto` safety-checks anything merely unlisted."""
    for role in ("reviewer", "warden"):
        deny = set((_shipped_bundle()["seats"][role].get("permissions") or {}).get("deny") or [])
        assert {"Edit", "Write"} <= deny, f"{role} could edit code"
        assert "Bash(git commit*)" in deny, f"{role} could commit"


def test_every_shipped_seat_runs_a_mode_that_can_be_dispatched_unattended():
    """`manual` prompts and `plan` forbids writes; both are refusals for an unattended seat.
    Operator direction is `auto` everywhere for now (see the read-only test above for how the
    reviewer keeps its property without `plan`)."""
    modes = {r: s.get("permission_mode") for r, s in _shipped_bundle()["seats"].items()}
    assert set(modes.values()) == {"auto"}, modes


def test_the_shipped_bundle_never_grants_the_two_history_destroying_verbs():
    """CLAUDE.md: `bd compact` / `bd flatten` are one-way and destroy the only record of when
    each lifecycle stage happened. No seat may reach them without a human."""
    for role, spec in _shipped_bundle()["seats"].items():
        deny = set((spec.get("permissions") or {}).get("deny") or [])
        assert {"Bash(bd compact*)", "Bash(bd flatten*)"} <= deny, role


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


# ---- …and the tripwire that proves it still catches a real escape (bh-ik08j) ----------------
#
# bh-yndxi fixed bh-ik08j's ROOT CAUSE by routing every `check-all` phase through the fence, so
# the tripwire watches a `$HOME` private to the run and no ambient process can trip it. That fix
# has one failure mode, and it is the dangerous one: a tripwire nothing can reach is
# indistinguishable from a tripwire that no longer works — both are permanently green. bh-ik08j
# makes that its own acceptance criterion ("a tripwire that no longer catches anything is not a
# fix"), so these tests exercise the guard against a REAL write rather than trusting that a
# green gate means an armed guard.


def _demo_module():
    """The demo script imported as a module, so `Tripwire` is the SHIPPED class and not a copy
    of it — a re-implementation here would keep passing after the real one was gutted."""
    import importlib.util

    demo = Path(__file__).resolve().parents[1] / "scripts" / "demo_local_loop.py"
    spec = importlib.util.spec_from_file_location("bh_demo_local_loop", demo)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_bd_init_timeout(demo, marker: str):
    return demo.BdInitTimedOut(
        demo.BD_INIT_TIMEOUT_SECONDS,
        f"stdout from {marker}",
        f"goroutine evidence from {marker}",
        -3,
    )


def _isolate_demo_in_process(demo, root: Path, monkeypatch):
    """Run the script's process-wide isolation while ensuring pytest restores every env key."""
    keys = (
        "BH_HOME",
        "BH_CONFIG",
        "BH_WORKTREES",
        "GIT_WORKSPACE",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "BEADS_SHARED_SERVER_DIR",
        "BH_SKIP_SETUP_CHECK",
        "GIT_PAGER",
        "NO_COLOR",
    )
    for key in keys:
        # Register the original value (including absence) with MonkeyPatch before `isolate`
        # writes os.environ directly.  Its teardown then restores the worker for later tests.
        monkeypatch.setenv(key, os.environ.get(key, ""))
    return demo.isolate(root)


def test_bd_init_first_timeout_discards_partial_state_and_retries_at_a_new_path(
    tmp_path, capsys, monkeypatch
):
    """A timed-out embedded store is never resumed; only its exact scratch workspace is gone."""
    demo = _demo_module()
    root = tmp_path / "scratch"
    _isolate_demo_in_process(demo, root, monkeypatch)
    preexisting = root / "workspace" / "sentinel"
    preexisting.write_text("this path existed before build_hive\n")
    lookalike = root / "workspace-bd-init-attempt-1-preexisting"
    lookalike.mkdir()
    (lookalike / "sentinel").write_text("not allocated by this invocation\n")
    keep = root / "keep-this-sibling"
    keep.write_text("not part of either init attempt\n")
    attempts = []

    def init_runner(main):
        attempts.append(main)
        if len(attempts) == 1:
            partial = main / ".beads" / "embeddeddolt"
            partial.mkdir(parents=True)
            (partial / "partial-state").write_text("must never be reused\n")
            raise _synthetic_bd_init_timeout(demo, "attempt one")

    main = demo.build_hive(root, init_runner=init_runner)

    assert len(attempts) == 2
    first_workspace, retry_workspace = (main.parents[2] for main in attempts)
    assert first_workspace != retry_workspace
    assert first_workspace.name.startswith("workspace-bd-init-attempt-1-")
    assert retry_workspace.name.startswith("workspace-bd-init-attempt-2-")
    assert not first_workspace.exists()
    assert main == attempts[1]
    assert main.exists()
    assert Path(os.environ["GIT_WORKSPACE"]) == retry_workspace
    assert preexisting.read_text() == "this path existed before build_hive\n"
    assert (lookalike / "sentinel").read_text() == "not allocated by this invocation\n"
    assert keep.read_text() == "not part of either init attempt\n"
    assert "attempt one" in (root / "bd-init-timeout-attempt-1.log").read_text()
    captured = capsys.readouterr()
    assert "partial scratch hive will not be reused" in captured.err
    assert "retrying bd init once in a distinct fresh scratch workspace" in captured.out


def test_bd_init_second_timeout_is_a_hard_failure_and_keeps_no_partial_workspace(
    tmp_path, capsys, monkeypatch
):
    """The retry is bounded too: two timeouts fail non-zero instead of accepting a timeout."""
    demo = _demo_module()
    root = tmp_path / "scratch"
    _isolate_demo_in_process(demo, root, monkeypatch)
    keep = root / "keep-this-sibling"
    keep.write_text("outside the narrow cleanup targets\n")
    attempts = []

    def always_times_out(main):
        attempts.append(main)
        partial = main / ".beads" / "metadata.json"
        partial.parent.mkdir(parents=True)
        partial.write_text("partial\n")
        raise _synthetic_bd_init_timeout(demo, f"attempt {len(attempts)}")

    with pytest.raises(SystemExit) as exc:
        demo.build_hive(root, init_runner=always_times_out)

    assert "timed out on both fresh attempts" in str(exc.value)
    assert len(attempts) == 2
    assert attempts[0] != attempts[1]
    assert all(not main.parents[2].exists() for main in attempts)
    assert (root / "workspace").is_dir(), "the pre-existing default path is not an attempt"
    assert keep.read_text() == "outside the narrow cleanup targets\n"
    assert (root / "bd-init-timeout-attempt-1.log").is_file()
    assert "attempt 2" in (root / "bd-init-timeout-attempt-2.log").read_text()
    assert capsys.readouterr().err.count("BD INIT TIMEOUT") == 2


def test_bd_init_cleanup_refuses_a_broad_or_unexpected_target(tmp_path):
    """The timeout recovery helper cannot be repurposed to recursively remove the demo root."""
    demo = _demo_module()
    root = tmp_path / "scratch"
    root.mkdir()
    stat = root.stat()
    broad = demo.FreshWorkspace(path=root, device=stat.st_dev, inode=stat.st_ino)
    with pytest.raises(RuntimeError, match="refusing to discard"):
        demo._discard_timed_out_workspace(root, broad)
    assert root.is_dir()


def test_bd_init_cleanup_refuses_a_replaced_workspace_inode(tmp_path):
    """Even an allocated name is not authority after that exact directory inode is replaced."""
    demo = _demo_module()
    root = tmp_path / "scratch"
    root.mkdir()
    owned = demo._fresh_bd_init_workspace(root, 1)
    moved = root / "original-owned-directory"
    owned.path.rename(moved)
    owned.path.mkdir()
    sentinel = owned.path / "pre-existing-sentinel"
    sentinel.write_text("replacement must survive\n")

    with pytest.raises(RuntimeError, match="refusing to discard"):
        demo._discard_timed_out_workspace(root, owned)

    assert sentinel.read_text() == "replacement must survive\n"
    assert moved.is_dir()


def test_bd_init_ordinary_failure_is_not_retried_or_cleaned(tmp_path, monkeypatch):
    """Retry is timeout-only; an ordinary failure preserves its one fresh hive for diagnosis."""
    demo = _demo_module()
    root = tmp_path / "scratch"
    _isolate_demo_in_process(demo, root, monkeypatch)
    preexisting = root / "workspace" / "sentinel"
    preexisting.write_text("pre-existing default workspace\n")
    attempts = []

    def fails_normally(main):
        attempts.append(main)
        (main / "ordinary-failure-sentinel").write_text("preserve me\n")
        raise SystemExit("bd init exited 23")

    with pytest.raises(SystemExit, match="exited 23"):
        demo.build_hive(root, init_runner=fails_normally)

    assert len(attempts) == 1
    assert (attempts[0] / "ordinary-failure-sentinel").read_text() == "preserve me\n"
    assert preexisting.read_text() == "pre-existing default workspace\n"


def test_bd_init_unreaped_process_refuses_cleanup_and_retry(tmp_path, monkeypatch):
    """A bounded reap failure cannot race deletion or a second init against a live process."""
    demo = _demo_module()
    root = tmp_path / "scratch"
    _isolate_demo_in_process(demo, root, monkeypatch)
    attempts = []

    def cannot_reap(main):
        attempts.append(main)
        (main / "still-owned-by-process").write_text("preserve\n")
        raise demo.BdInitTimedOut(0.1, "", "", None, reaped=False)

    with pytest.raises(SystemExit, match="refusing to delete.*concurrent retry"):
        demo.build_hive(root, init_runner=cannot_reap)

    assert len(attempts) == 1
    assert (attempts[0] / "still-owned-by-process").read_text() == "preserve\n"


def test_bounded_bd_init_kills_its_group_and_never_blocks_on_an_escaped_pipe_holder(
    tmp_path, monkeypatch
):
    """Exercise the real deadline, group SIGKILL, bounded pipe drain, and direct-child reap."""
    demo = _demo_module()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_bd = bindir / "bd"
    fake_bd.write_text(
        """#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGQUIT, signal.SIG_IGN)
same_group = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGQUIT, signal.SIG_IGN); time.sleep(60)",
])
escaped = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGQUIT, signal.SIG_IGN); time.sleep(60)",
], start_new_session=True)
print(
    f"parent={os.getpid()} pgid={os.getpgrp()} same_group={same_group.pid} escaped={escaped.pid}",
    flush=True,
)
time.sleep(60)
"""
    )
    fake_bd.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(demo, "BD_INIT_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(demo, "BD_INIT_DIAGNOSTIC_GRACE_SECONDS", 0.15)
    monkeypatch.setattr(demo, "BD_INIT_KILL_DRAIN_SECONDS", 0.15)
    monkeypatch.setattr(demo, "BD_INIT_REAP_SECONDS", 0.5)
    main = tmp_path / "main"
    main.mkdir()
    started = time.monotonic()

    with pytest.raises(demo.BdInitTimedOut) as caught:
        demo._bounded_bd_init(main)

    elapsed = time.monotonic() - started
    error = caught.value
    assert elapsed >= 0.4, "the test must exercise the post-SIGKILL drain deadline"
    assert elapsed < 1.5, "an escaped inherited pipe must not make the final drain unbounded"
    assert error.reaped is True
    assert error.returncode == -signal.SIGKILL
    fields = dict(item.split("=", 1) for item in error.stdout.strip().split())
    parent = int(fields["parent"])
    pgid = int(fields["pgid"])
    same_group = int(fields["same_group"])
    escaped = int(fields["escaped"])
    assert parent == pgid

    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (_pid_alive(parent) or _pid_alive(same_group)):
            time.sleep(0.02)
        assert not _pid_alive(parent), "the direct bd child was not reaped"
        assert not _pid_alive(same_group), "a same-process-group bd descendant survived SIGKILL"
    finally:
        # The deliberately escaped process is the fixture that kept the inherited pipes open. It
        # is outside the helper's process-group contract, so the test owns and reaps it explicitly.
        try:
            os.killpg(escaped, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_the_tripwire_still_catches_a_real_escape(tmp_path, monkeypatch):
    """A deliberate write to a path OUTSIDE the demo's scratch root must still fail the run.

    This is the criterion that stops "make it green" from passing: bh-ik08j's four originally
    filed fix directions all WEAKENED the assertion (scope it, allowlist it, diff content, name
    the process), and the shipped fix instead removed the shared object the assertion races on.
    That is only a fix if the assertion still fires — proven here against a real file, not
    argued.
    """
    demo = _demo_module()
    watched = tmp_path / "beadhive"
    (watched / "hq" / "hives").mkdir(parents=True)
    (watched / "hq" / "hives" / "already-here.yaml").write_text("before\n")

    wire = demo.Tripwire("~/.beadhive", watched)
    # The escape: something inside the run writes outside its scratch root.
    (watched / "hq" / "hives" / "escaped.yaml").write_text("written by the demo\n")

    monkeypatch.setenv("BH_HERMETIC_FENCE", "1")
    with pytest.raises(SystemExit) as exc:
        wire.assert_untouched()
    message = str(exc.value)
    assert "ISOLATION VIOLATION" in message
    assert "hq/hives/escaped.yaml" in message


def test_an_untouched_tree_is_silent(tmp_path):
    """The other half of the same claim: the guard must not fire on nothing, or the test above
    would pass for a `raise` with no condition on it."""
    watched = tmp_path / "beadhive"
    watched.mkdir()
    (watched / "quiet.yaml").write_text("unchanged\n")
    _demo_module().Tripwire("~/.beadhive", watched).assert_untouched()  # must not raise


def test_a_violation_names_the_cause_not_only_the_paths(tmp_path, monkeypatch):
    """bh-ik08j AC5: 'isolation violation' sent a full session chasing the demo when the cause
    was elsewhere, twice. The message must say WHICH of the two worlds it is in — a fenced
    violation can only be the demo, an unfenced one may be any bh process on the box.
    """
    demo = _demo_module()
    watched = tmp_path / "beadhive"
    (watched / "hq" / "hives" / "github" / "beadhive").mkdir(parents=True)
    wire = demo.Tripwire("~/.beadhive", watched)
    (watched / "hq" / "hives" / "github" / "beadhive" / "beadhive.yaml").write_text("v62\n")

    monkeypatch.setenv("BH_HERMETIC_FENCE", "1")
    with pytest.raises(SystemExit) as fenced:
        wire.assert_untouched()
    assert "FENCED" in str(fenced.value)
    assert "THIS DEMO or a child of it" in str(fenced.value)

    monkeypatch.delenv("BH_HERMETIC_FENCE", raising=False)
    with pytest.raises(SystemExit) as unfenced:
        wire.assert_untouched()
    text = str(unfenced.value)
    assert "UNFENCED" in text
    # …and it names the MEASURED ambient writer for that path rather than blaming the demo.
    assert "bh doctor" in text
    assert "hive_schema.refresh" in text


def test_the_measured_cause_of_run_3_is_recorded_where_the_violation_prints_it():
    """bh-ik08j AC4: run 3's cause must be identified by measurement or explicitly recorded —
    'not quietly dropped because the symptom went away'. It WAS identified (a `bh doctor` run:
    `hive_schema.refresh` per registered hive plus a forced fleet-metadata read), and the record
    has to live where the next operator reading a violation will see it, not only in a bead.
    """
    demo = Path(__file__).resolve().parents[1] / "scripts" / "demo_local_loop.py"
    text = demo.read_text()
    assert "hive_schema.refresh" in text
    assert "TTL hypothesis is therefore DISPROVEN" in text


def test_a_mtime_only_rewrite_is_reported_as_one(tmp_path, monkeypatch):
    """Fix direction (c) survives as REPORTING rather than as an exemption: an idempotent
    rewrite that changed no bytes still trips the wire (inside the fence it is still the demo
    writing outside its root), but it says so, because "same size, mtime moved" and "this file
    grew" are different findings and the old message printed both as a bare path."""
    demo = _demo_module()
    watched = tmp_path / "beadhive"
    watched.mkdir()
    target = watched / "metadata.json"
    target.write_text("{}")
    wire = demo.Tripwire("~/.beadhive", watched)
    os.utime(target, (0, 0))

    monkeypatch.setenv("BH_HERMETIC_FENCE", "1")
    with pytest.raises(SystemExit) as exc:
        wire.assert_untouched()
    assert "idempotent rewrite" in str(exc.value)


# ---- the repo's own .beads/ is SHARED with the host, even fenced (bh-zq5is) -------------------
#
# The fence hides `~/.beadhive` behind a tmpfs but binds this repo's `.beads/` straight back in
# READ-ONLY, so a before/after diff of that one path races every other `bh`/`bd` process on the
# box — and it lost: `just check-all` (the pre-push gate) failed on `last-touched`'s mtime moving
# under an ambient `bh doctor` in another repo's session, blaming a demo that could not physically
# have written it. These pin the stronger guard that replaced it, so the next version bump (or
# anything else that happens to run while another seat is busy) cannot re-break the push gate.


def _fake_repo_with_beads(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".beads").mkdir(parents=True)
    (repo / ".beads" / "last-touched").write_text("1786766\n")
    return repo


def _scratch_world(tmp_path: Path, monkeypatch) -> Path:
    """A scratch root holding the four roots `verify_isolation` re-reads, plus a private HOME."""
    root = tmp_path / "scratch"
    for name in ("bh-home", "worktrees", "workspace"):
        (root / name).mkdir(parents=True)
    (tmp_path / "home").mkdir()
    for key, value in {
        "BH_HOME": str(root / "bh-home"),
        "BH_CONFIG": str(root / "bh-home" / "config.yaml"),
        "BH_WORKTREES": str(root / "worktrees"),
        "GIT_WORKSPACE": str(root / "workspace"),
        "HOME": str(tmp_path / "home"),
    }.items():
        monkeypatch.setenv(key, value)
    return root


def test_a_fenced_run_proves_the_beads_write_barrier_instead_of_diffing_a_shared_dir(
    tmp_path, monkeypatch
):
    """Fenced, the repo `.beads/` guard must be the kernel's, not a snapshot diff."""
    demo = _demo_module()
    repo = _fake_repo_with_beads(tmp_path)
    monkeypatch.setattr(demo, "REPO", repo)
    monkeypatch.setenv("BH_HERMETIC_FENCE", "1")
    root = _scratch_world(tmp_path, monkeypatch)
    (repo / ".beads").chmod(0o500)  # what `--ro-bind` gets you, without needing a mount
    try:
        wires = {wire.label: wire for wire in demo.verify_isolation(root)}
        assert isinstance(wires["repo .beads/"], demo.WriteBarrier)
        # …and an ambient HOST write to that path does not fail the run.
        os.utime(repo / ".beads" / "last-touched", (0, 0))
        for wire in wires.values():
            wire.assert_untouched()  # must not raise
        # …while the genuinely private path keeps its diff.
        assert isinstance(wires["~/.beadhive"], demo.Tripwire)
    finally:
        (repo / ".beads").chmod(0o700)


def test_an_unfenced_run_still_diffs_the_repo_beads(tmp_path, monkeypatch):
    """Unfenced there is no barrier to assert, so the ordinary tripwire has to stay armed."""
    demo = _demo_module()
    repo = _fake_repo_with_beads(tmp_path)
    monkeypatch.setattr(demo, "REPO", repo)
    monkeypatch.delenv("BH_HERMETIC_FENCE", raising=False)
    wires = {
        wire.label: wire for wire in demo.verify_isolation(_scratch_world(tmp_path, monkeypatch))
    }
    assert isinstance(wires["repo .beads/"], demo.Tripwire)
    (repo / ".beads" / "escaped.json").write_text("written by the demo\n")
    with pytest.raises(SystemExit, match="ISOLATION VIOLATION"):
        wires["repo .beads/"].assert_untouched()


def test_a_writable_beads_inside_the_fence_is_a_loud_failure(tmp_path, monkeypatch):
    """The barrier is only worth asserting if losing it fails: drop the `--ro-bind` and the demo
    can write the operator's bead store, which is the escape the whole guard is about."""
    demo = _demo_module()
    monkeypatch.setenv("BH_HERMETIC_FENCE", "1")
    beads = tmp_path / ".beads"
    beads.mkdir()
    with pytest.raises(SystemExit, match="is WRITABLE inside the fence"):
        demo.WriteBarrier("repo .beads/", beads)
    assert not (beads / demo.WriteBarrier.PROBE).exists(), "the probe file was left behind"


# ---- ONE closed set for the ONE `dispatch:` dimension ------------------------------------


def test_every_cause_the_loop_can_write_is_a_registered_dispatch_value():
    """THE regression this locks down: `localloop` used to keep its own seven-value
    `DISPATCH_CAUSES` tuple and `state.STATE_DIMENSIONS["dispatch"]` kept a different seven,
    and their INTERSECTION WAS EMPTY. Every label the loop emitted would have failed
    `bh label validate`, and `work.dispatch_cause_count` raised `ValueError` for every cause the
    loop writes — so the loop-breaker's read path could not count a single thing the loop wrote.

    Enumerated from the module rather than re-typed, so a NEW cause added to `localloop` without
    a matching registration fails here instead of in production.
    """
    registered = state.STATE_DIMENSIONS[state.DISPATCH_DIM]
    writable = {
        value
        for name, value in vars(localloop).items()
        if name.startswith("CAUSE_") and isinstance(value, str)
    }
    assert writable, "the CAUSE_* aliases went away — this test is no longer testing anything"
    assert writable <= registered, f"unregistered dispatch causes: {sorted(writable - registered)}"
    # …and the classifier's own outputs, which is where a new RunOutcome would leak in.
    for outcome in seatrun.RunOutcome:
        cause = localloop.LocalLoop._cause_for(
            seatrun.Classification(outcome=outcome, detail="", seat_run=None, envelope=None)
        )
        assert cause == "" or cause in registered


def test_both_writers_validate_against_the_same_set():
    """`localloop.record_cause` and `work.record_dispatch_failure` are the two write paths.
    Neither may accept a value the other (or the reader) would reject."""
    from beadhive import work as work_mod

    for cause in sorted(state.STATE_DIMENSIONS[state.DISPATCH_DIM]):
        # the read path must be able to COUNT every writable cause (it used to raise)
        assert work_mod.dispatch_cause_count([], cause) == 0

    with pytest.raises(ValueError):
        localloop.record_cause(Path("."), "b1", "not-a-registered-cause", reason="x")
    with pytest.raises(ValueError):
        work_mod.record_dispatch_failure("b1", "not-a-registered-cause", "x", Path("."))


def test_the_old_disjoint_spellings_are_gone():
    """The bare run-outcome spellings (`failed`/`blocked`/`cancelled`/`bead-mismatch`/
    `lease-lost`) are NOT registered: they read as properties of the bead rather than of one
    process's exit, and `blocked` in particular is indistinguishable from a blocked-bead state.
    They are `run_`-prefixed now, and the old spellings must not quietly work again."""
    registered = state.STATE_DIMENSIONS[state.DISPATCH_DIM]
    for gone in ("failed", "blocked", "handoff", "cancelled", "bead-mismatch", "lease-lost"):
        assert gone not in registered


# ---- SIGTERM: the only signal this process will ever get ----------------------------------


@async_test
async def test_a_sigterm_handler_is_installed_and_shuts_the_loop_down(tmp_path, fakebd):
    """`Restart=always` + `KillMode=control-group` means SIGTERM is how this process is ALWAYS
    stopped. Python's default disposition terminates the interpreter outright, so `run`'s
    `finally: await self.shutdown()` never ran: the priced envelope was never read, the cause
    was never written, and the claim aged out over the 5-minute TTL with the seat possibly still
    alive and spending."""
    fake = fakebd(FakeBd(children=[_child("b1")]))
    loop = _loop(
        tmp_path,
        caps=localloop.Caps(max_concurrency=1),
        poll_interval=30.0,  # long enough that only the signal can end the run
        claim=lambda: localloop.ClaimResult(claimed="b1", worktree=_ws(tmp_path, "b1")),
        instructions=lambda a, b, r: str(_instructions(tmp_path, f"{b}", "STUB_HANG=true")),
    )

    async def sigterm_soon():
        deadline = time.monotonic() + 15
        while not loop.in_flight and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert loop.in_flight, "the fixture never got a seat running — nothing to signal"
        os.kill(os.getpid(), signal.SIGTERM)

    signaller = asyncio.create_task(sigterm_soon())
    reports = await asyncio.wait_for(loop.run(), timeout=20)
    await signaller

    assert loop.stopping is True, "the handler must flip the stop flag, not kill the process"
    assert len(reports) >= 1
    assert loop.in_flight == {}, "shutdown must run: every seat cancelled and reaped"
    assert ("b1", "dispatch=run_cancelled") in fake.written_states(), "the cause must be written"
    assert any(c[:2] == ["update", "b1"] for c in fake.calls), (
        "and the claim released rather than left to age out over the TTL"
    )


def test_the_sigterm_handler_degrades_instead_of_refusing_to_start():
    """It cannot be installed off the main thread (a caller driving `run` from `LocalRuntime`'s
    private loop thread, or a test runner). That must be a debug line and a normal run, not a
    crash."""
    loop = localloop.LocalLoop(hive_dir=Path("."), epic="e", actor="disp/x")

    async def probe():
        return loop._install_sigterm_handler()

    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(asyncio.run(probe())))
    thread.start()
    thread.join()
    assert result == [False]


# ---- a developer seat is NEVER handed the main clone --------------------------------------


@async_test
async def test_a_developer_seat_is_refused_the_main_clone(tmp_path, fakebd):
    """`workspace_for` defaulted to `hive_dir` and `_act` routed `resume`/`review`/`merge`/
    `finish` through `_spawn_for` with no `workspace=`. `work.py::loop` never passes
    `workspace_for`, so in the only real deployment a resume handed a developer seat — which
    COMMITS — the integration branch.

    Still the BACKSTOP after bh-4kq1b: the spawn path now tries `_provision` first, and this
    hive is not in any registry, so provisioning cannot succeed and the refusal must still fire.
    """
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    loop = _loop(tmp_path, workspace_for=lambda _bead: str(tmp_path))  # == hive_dir
    report = localloop.PassReport()

    await loop._spawn_for("b1", action="resume", role="developer", report=report)

    assert loop.in_flight == {}, "nothing may be spawned into the main clone"
    assert report.dispatched == ()
    assert report.causes == (("b1", localloop.CAUSE_PROVISIONING_FAILED),)
    assert ("b1", "dispatch=provisioning_failed") in fake.written_states()


@async_test
async def test_non_developer_seats_may_still_run_in_the_main_clone(tmp_path, fakebd):
    """`start` is the one action that legitimately runs there — it CREATES the epic's container
    worktree, so it cannot already be standing in it — and it is a dispatcher seat, not a
    developer."""
    fakebd(FakeBd(children=[_child("epic-1", status="open", issue_type="epic")]))
    loop = _loop(tmp_path, workspace_for=lambda _bead: str(tmp_path))
    report = localloop.PassReport()

    await loop._spawn_for("epic-1", action="start", role="dispatcher", report=report)

    assert report.dispatched == ("epic-1",)
    await loop.shutdown()


@async_test
async def test_a_resume_with_no_worktree_yet_provisions_one_instead_of_dead_ending(
    tmp_path, fakebd
):
    """The legitimate seat the refusal used to eat (bh-4kq1b).

    `resume` is the only DEVELOPER action that arrives with no workspace on the envelope, so it
    depends on the bead's worktree DIRECTORY already existing. Usually it does — `submit` leaves
    it intact — but not on a second host that picked the epic up under `bh host lease`, and not
    in a clone where `$BH_WORKTREES` was reclaimed or `bh work abandon --rm` ran. There the seat
    had every right to run and was refused anyway, and because a `provisioning_failed` event
    carries none of the `changes-requested` markers `work_next.attempt_count` counts, the
    loop-breaker never tripped: the same pass re-fired every `poll_interval`, minting a
    PERMANENT event bead each time. Provisioning is the recovery, and it is what `bh work
    resume` itself does.
    """
    fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    provisioned = _ws(tmp_path, "b1")
    loop = _loop(tmp_path, workspace_for=lambda _bead: str(tmp_path))  # == hive_dir
    loop._provision = lambda bead: provisioned if bead == "b1" else ""
    report = localloop.PassReport()

    await loop._spawn_for("b1", action="resume", role="developer", report=report)

    assert report.dispatched == ("b1",)
    assert report.causes == (), "a provisionable seat is not a provisioning failure"
    assert loop.in_flight["b1"].role == "developer"
    await loop.shutdown()


def test_resolve_workspace_falls_back_to_the_clone_but_never_silently(tmp_path, caplog):
    """An unresolvable bead falls back to the main clone — and `_spawn_for`'s refusal is what
    keeps that fallback safe for a developer seat."""
    assert localloop.resolve_workspace(tmp_path, "nope-does-not-exist") == str(tmp_path)


# ---- --json streams, it does not buffer ----------------------------------------------------


@async_test
async def test_run_reports_each_pass_as_it_completes(tmp_path, fakebd):
    """`bh work loop --json` promises "one JSON pass report per line". Emitting after `run()`
    returned made it a batch: with `--passes 0` that is NOTHING until the molecule lands, hours
    later, while the report list grows unboundedly."""
    fakebd(FakeBd(epic_status="in_progress", children=[_child("b1", status="in_progress")]))
    loop = _loop(
        tmp_path,
        poll_interval=0.0,
        claim=lambda: localloop.ClaimResult(reason="empty_queue"),
    )
    seen: list[int] = []
    reports = await loop.run(max_passes=3, on_pass=lambda r: seen.append(r.number))

    assert seen == [1, 2, 3], "each pass must be handed over as it ends, in order"
    assert [r.number for r in reports] == seen


# ---- BAML_PROFILE_DIR is stamped on every spawn (bh-hum73) ----------------------------------


@async_test
async def test_spawn_sets_baml_profile_dir_and_keeps_the_env(tmp_path, monkeypatch):
    """Unset, BAML 0.16.0 profiles into `<cwd>/.baml/profiles/` — and cwd is the bead worktree.

    A real process, because the property is about what the CHILD sees: the variable points at a
    per-run path under bh's home AND the rest of bh's environment survived (`spawn_seat` builds
    the dict from `os.environ`; replacing rather than copying it would strip PATH/HOME from every
    seat).
    """
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.setenv("BH_CANARY", "inherited")
    argv = [
        sys.executable,
        "-c",
        "import json,os;print(json.dumps({k: os.environ.get(k) for k in "
        "('BAML_PROFILE_DIR', 'BH_CANARY')}))",
    ]
    seat = await _spawn(argv, session="sess-baml")
    seen = json.loads(await seat.collect(timeout=10))
    await seat.wait_exit(10)

    assert seen["BAML_PROFILE_DIR"] == str(tmp_path / "bhhome" / "baml-profiles" / "sess-baml")
    assert seen["BH_CANARY"] == "inherited", "the inherited environment must not be dropped"
    assert Path(seen["BAML_PROFILE_DIR"]).is_dir(), "the profile dir is created before the spawn"
