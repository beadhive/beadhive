"""LocalLoop drives every seat through the registered runtime (bh-03rvd, B1 step 2).

The loop keeps admission, routing, the workspace and every lifecycle write; the runtime starts,
observes, cancels and notifies seats. These tests pin that split with a REGISTERED fake runtime
(resolved through ``work.runtime`` exactly as a third-party one would be), so they prove the
loop's own behaviour — what it schedules, when it observes, which causes it writes, how it
degrades when an optional capability is missing — with no process anywhere. Time is an
injected clock, never a sleep. The real-process side (LocalRuntime as the default runtime, parity
with the pre-change spawn path) lives in ``tests/test_localloop.py`` and
``tests/test_local_runtime_parity.py``.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from beadhive import bd as bd_mod
from beadhive import beads_routing, localloop, otel, runtime, seatrun, work, work_dispatch
from beadhive.complexity import ComplexityTier
from beadhive.model_routing import ModelSelection
from beadhive.runtime import RoleHandle, RoleOutcome, ScheduleRefused

SRC = Path(localloop.__file__).parent
STUB_SEAT = Path(__file__).parent / "fixtures" / "stub_seat.py"


@pytest.fixture(autouse=True)
def _bd_cli_fallback_route(monkeypatch):
    """Drive the bd CLI route against the fake below, as ``tests/test_localloop.py`` does."""
    monkeypatch.setattr(
        beads_routing, "configured_route", lambda *_a, **_k: beads_routing.ROUTE_API_CLI_FALLBACK
    )


@pytest.fixture
def clean_registry():
    saved = dict(runtime._RUNTIMES)
    yield
    runtime._RUNTIMES.clear()
    runtime._RUNTIMES.update(saved)


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---- a fake bd and a fake runtime ----------------------------------------------------------------


class _CP:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeBd:
    """The handful of `bd` reads and writes one pass makes (shaped after test_localloop's)."""

    def __init__(self, children=()):
        self.children = [dict(c) for c in children]
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        args = list(cmd[1:])
        while args and args[0] in ("-C", "--actor"):
            args = args[2:]
        args = [a for a in args if a != "--json"]
        self.calls.append(args)
        sub = args[0] if args else ""
        if sub == "gate" and args[1:2] == ["list"]:
            return _CP(0, "[]")
        if sub == "gate":
            return _CP(0, json.dumps({"checked": 0, "resolved": 0, "escalated": 0, "errors": 0}))
        if sub == "reclaim":
            return _CP(0, json.dumps({"count": 0, "reclaimed": []}))
        if sub == "heartbeat":
            return _CP(0, json.dumps({"id": args[1], "status": "heartbeat"}))
        if sub == "show":
            return _CP(0, json.dumps({"id": args[1], "status": "in_progress"}))
        if sub == "list" and "--parent" in args:
            parent = args[args.index("--parent") + 1]
            return _CP(0, json.dumps(self.children if parent == "epic-1" else []))
        if sub == "ready":
            return _CP(0, "[]")
        return _CP(0, "")

    def written_states(self):
        return [(c[1], c[2]) for c in self.calls if c and c[0] == "set-state"]

    def set_state_reason(self, bead):
        call = next(c for c in self.calls if c[:2] == ["set-state", bead])
        return call[call.index("--reason") + 1]

    def released(self, bead):
        return any(c[:2] == ["update", bead] and "--status" in c for c in self.calls)


@pytest.fixture
def fakebd(monkeypatch):
    def install(fake):
        monkeypatch.setattr(bd_mod, "_run", fake)
        return fake

    return install


class FakeRuntime:
    """Only the three protocol verbs — no cancel, no notify, no close, not adoptable — and a
    ``schedule`` that declares neither ``decision`` nor ``action``: passing either would raise
    ``TypeError``, so every test below also proves the loop asks the signature first."""

    name = "fake"

    def __init__(self):
        self.scheduled: list[dict] = []
        self.observed: list[RoleHandle] = []
        self.gates: list = []
        self.script: dict[str, list[RoleOutcome]] = {}

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        self.scheduled.append(
            {
                "bead": bead_id,
                "role": role,
                "workspace": workspace,
                "instructions": instructions,
                "session_id": session_id,
                "model": model,
            }
        )
        return RoleHandle(bead_id=bead_id, session_id=session_id)

    def observe(self, handle):
        self.observed.append(handle)
        queue = self.script.get(handle.bead_id) or []
        return queue.pop(0) if queue else RoleOutcome(status="running")

    def on_gate_resolved(self, resolution):
        self.gates.append(resolution)


class CancellableFake(FakeRuntime):
    def __init__(self, outcome):
        super().__init__()
        self.cancel_outcome = outcome
        self.cancelled: list[RoleHandle] = []

    def cancel(self, handle):
        self.cancelled.append(handle)
        return self.cancel_outcome


class NotifiableFake(FakeRuntime):
    def __init__(self, live):
        super().__init__()
        self.live = set(live)
        self.notes: list[tuple[str, str]] = []

    def notify(self, handle, message):
        self.notes.append((handle.bead_id, message))
        return handle.bead_id in self.live


class KwargsFake(FakeRuntime):
    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None, **kw):
        handle = super().schedule(
            bead_id,
            role,
            workspace=workspace,
            instructions=instructions,
            session_id=session_id,
            model=model,
        )
        self.scheduled[-1]["extras"] = kw
        return handle


def _child(bead_id, **kw):
    return {
        "id": bead_id,
        "parent": "epic-1",
        "status": "open",
        "issue_type": "task",
        "labels": [],
        **kw,
    }


def _ws(tmp_path, bead):
    path = tmp_path / "wt" / bead
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _loop(tmp_path, rt, **kw):
    kw.setdefault("hive_dir", tmp_path)
    kw.setdefault("workspace_for", lambda bead: _ws(tmp_path, bead))
    kw.setdefault("epic", "epic-1")
    kw.setdefault("actor", "disp/loop")
    kw.setdefault("routing", lambda _bead, _role: None)
    kw.setdefault("instructions", lambda _a, bead, _r: f"brief-{bead}.md")
    return localloop.LocalLoop(runtime=rt, **kw)


def _registered(clean_registry, rt, name="fake-loop"):
    """Resolve *rt* the way `bh work loop` does: register it, then `loop_runtime(cfg, entry)`."""
    runtime.register_runtime(name, lambda cfg, entry: rt)
    resolved = localloop.loop_runtime({"work": {"runtime": name}}, None)
    assert resolved is rt
    return resolved


def _blocked_outcome():
    """A terminal outcome exactly as a real seat's stdout classifies — so the expected cause and
    reason are the ones the loop wrote before bh-03rvd for the same run."""
    cls = seatrun.classify_run(
        0,
        json.dumps(
            {"outcome": {"status": "blocked", "summary": "need a human"}, "session_id": "p-b1"}
        ),
        bead="b1",
    )
    return RoleOutcome.from_classification(cls, exit_code=0)


# ---- schedule once, observe later, causes as today -----------------------------------------------


@async_test
async def test_a_pass_schedules_each_seat_once_then_observes_it_on_later_passes(
    tmp_path, fakebd, clean_registry
):
    fake = fakebd(FakeBd(children=[_child("b1"), _child("b2")]))
    rt = _registered(clean_registry, FakeRuntime())
    claims = iter(
        [
            localloop.ClaimResult(claimed="b1", worktree=_ws(tmp_path, "b1")),
            localloop.ClaimResult(claimed="b2", worktree=_ws(tmp_path, "b2")),
        ]
    )
    loop = _loop(
        tmp_path,
        rt,
        caps=localloop.Caps(max_concurrency=2),
        claim=lambda: next(claims, localloop.ClaimResult(reason="empty_queue")),
    )

    first = await loop.run_pass()
    assert first.dispatched == ("b1", "b2")
    assert [s["bead"] for s in rt.scheduled] == ["b1", "b2"], "one schedule() per seat"
    assert rt.observed == [], "nothing was in flight to observe at the top of pass 1"
    assert {b: loop.in_flight[b].handle for b in loop.in_flight} == {
        s["bead"]: RoleHandle(s["bead"], s["session_id"]) for s in rt.scheduled
    }
    assert all(s["session_id"].startswith("provider-") for s in rt.scheduled)
    assert rt.scheduled[0]["workspace"] == _ws(tmp_path, "b1")
    assert rt.scheduled[0]["instructions"] == "brief-b1.md"

    second = await loop.run_pass()
    assert len(rt.scheduled) == 2, "an in-flight seat is never scheduled twice"
    assert [h.bead_id for h in rt.observed] == ["b1", "b2"], "observed on the later pass"
    assert second.harvested == () and second.in_flight == ("b1", "b2")
    assert second.heartbeats == ("b1", "b2")

    rt.script = {"b1": [_blocked_outcome()], "b2": [RoleOutcome(status="done")]}
    third = await loop.run_pass()
    assert len(rt.scheduled) == 2
    assert third.harvested == (("b1", "blocked"), ("b2", "done"))
    assert third.causes == (("b1", localloop.CAUSE_BLOCKED),), "a done run writes nothing"
    assert fake.written_states() == [("b1", "dispatch=run_blocked")]
    assert fake.set_state_reason("b1") == (
        "exit 0; action dispatch; session p-b1; cost 0.0; need a human"
    )
    assert loop.in_flight == {}
    await loop.shutdown()


@async_test
async def test_a_failed_outcome_is_harvested_under_its_classifier_label(tmp_path, fakebd):
    """`PassReport.harvested` keeps the classifier's spelling (`incomplete`) for `failed`."""
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    rt = FakeRuntime()
    loop = _loop(tmp_path, rt)
    await loop._spawn_for("b1", action="resume", role="developer", report=localloop.PassReport())
    rt.script = {"b1": [RoleOutcome(status="failed", detail="no SeatRun", exit_code=2)]}

    report = localloop.PassReport()
    await loop._harvest(report)

    assert report.harvested == (("b1", "incomplete"),)
    assert report.causes == (("b1", localloop.CAUSE_FAILED),)
    assert fake.set_state_reason("b1") == "exit 2; action resume; no SeatRun"


@async_test
async def test_an_unknown_terminal_status_is_a_failure_not_a_crash(tmp_path, fakebd):
    fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    rt = FakeRuntime()
    loop = _loop(tmp_path, rt)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())
    rt.script = {"b1": [RoleOutcome(status="evaporated")]}

    report = localloop.PassReport()
    await loop._harvest(report)

    assert report.harvested == (("b1", "evaporated"),)
    assert report.causes == (("b1", localloop.CAUSE_FAILED),)


# ---- the ValueError contract ---------------------------------------------------------------------


class _Refusing(FakeRuntime):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        raise self.exc


@async_test
async def test_a_bare_value_error_from_schedule_is_a_failure(tmp_path, fakebd):
    fake = fakebd(FakeBd(children=[_child("b1")]))
    rt = _Refusing(ValueError("no capacity in pool east"))
    loop = _loop(tmp_path, rt)
    report = localloop.PassReport()

    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)

    assert loop.in_flight == {} and report.dispatched == ()
    assert report.causes == (("b1", localloop.CAUSE_FAILED),)
    assert fake.set_state_reason("b1") == (
        "runtime fake refused to schedule b1: no capacity in pool east"
    )


@async_test
async def test_a_schedule_refused_maps_its_status_reason_and_evidence(tmp_path, fakebd):
    fake = fakebd(FakeBd(children=[_child("b1")]))
    payload = {"bead": "b1", "status": "blocked", "reason": "quota"}
    rt = _Refusing(
        ScheduleRefused("cannot schedule b1", status="blocked", reason="quota", payload=payload)
    )
    loop = _loop(tmp_path, rt)
    report = localloop.PassReport()

    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)

    assert report.causes == (("b1", localloop.CAUSE_BLOCKED),)
    assert fake.set_state_reason("b1") == "quota"
    assert report.as_dict()["routing"] == {"b1": payload}
    assert loop.in_flight == {}


# ---- optional extras: passed only to a runtime that declares them --------------------------------


def _selection():
    return ModelSelection(
        required_tier=ComplexityTier.COMPLEX,
        preferred_model=None,
        selected_model="anthropic/claude-sonnet-4-5",
        policy="loose",
        role="developer",
        harness="claude",
        endpoint=None,
        availability_source="explicit_configuration",
        selection_reason="least-overpowered available model covering the required complexity",
    )


@async_test
async def test_decision_and_action_reach_only_a_runtime_that_declares_them(tmp_path, fakebd):
    fakebd(FakeBd(children=[_child("b1")]))
    selection = _selection()

    plain = FakeRuntime()
    await _loop(tmp_path, plain, routing=lambda _b, _r: selection)._spawn_for(
        "b1", action="resume", role="developer", report=localloop.PassReport()
    )
    assert plain.scheduled[0]["model"] == "anthropic/claude-sonnet-4-5"

    extended = KwargsFake()
    await _loop(tmp_path, extended, routing=lambda _b, _r: selection)._spawn_for(
        "b1", action="resume", role="developer", report=localloop.PassReport()
    )
    assert extended.scheduled[0]["extras"] == {"decision": selection, "action": "resume"}


def test_schedule_accepts_reads_the_signature_not_the_type():
    assert runtime.schedule_accepts(FakeRuntime(), "decision") is False
    assert runtime.schedule_accepts(KwargsFake(), "action") is True
    assert runtime.schedule_accepts(localloop.LocalRuntime(), "action") is True
    assert runtime.schedule_accepts(localloop.LocalRuntime(), "decision") is True
    assert runtime.schedule_accepts(object(), "decision") is False


# ---- the wall-time cap and shutdown, with and without cancel -------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@async_test
async def test_without_cancel_the_wall_time_cap_degrades_as_designed(tmp_path, fakebd, monkeypatch):
    """No cancel capability: the loop cannot stop the session and says so — cause `cancelled`,
    rung `unsupported`, the claim released, a warning naming the runtime — and never raises."""
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    warnings = []
    monkeypatch.setattr(localloop._LOG, "warning", lambda event, **kw: warnings.append((event, kw)))
    clock = Clock()
    rt = FakeRuntime()
    loop = _loop(tmp_path, rt, caps=localloop.Caps(max_run_seconds=10.0), clock=clock)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())
    handle = loop.in_flight["b1"].handle

    clock.now += 9.5
    early = localloop.PassReport()
    await loop._enforce_wall_time(early)
    assert early.cancelled == () and "b1" in loop.in_flight, "under the cap nothing happens"
    assert rt.observed == [], "and the runtime is not even asked"

    clock.now += 0.5
    capped = localloop.PassReport()
    await loop._enforce_wall_time(capped)

    assert rt.observed == [handle], "observed once, to confirm it is still running"
    assert capped.cancelled == (("b1", localloop.RUNG_UNSUPPORTED),)
    assert capped.causes == (("b1", localloop.CAUSE_CANCELLED),)
    assert fake.written_states() == [("b1", "dispatch=run_cancelled")]
    assert fake.set_state_reason("b1") == (
        "per-run wall-time cap 10.0s exceeded; cancelled at rung unsupported, "
        "session unknown, cost 0.0"
    )
    assert fake.released("b1"), "the claim is released, not left to age out over the TTL"
    assert loop.in_flight == {}
    (refusal,) = [kw for event, kw in warnings if event == "runtime_cannot_cancel"]
    assert (refusal["bead"], refusal["runtime"]) == ("b1", "fake")


@async_test
async def test_a_finished_run_past_the_cap_is_left_to_the_harvest(tmp_path, fakebd):
    fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    clock = Clock()
    rt = CancellableFake(RoleOutcome(status="handoff", cancel_rung="cooperative"))
    loop = _loop(tmp_path, rt, caps=localloop.Caps(max_run_seconds=1.0), clock=clock)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())
    rt.script = {"b1": [RoleOutcome(status="done")]}
    clock.now += 5

    report = localloop.PassReport()
    await loop._enforce_wall_time(report)

    assert report.cancelled == () and rt.cancelled == []
    assert "b1" in loop.in_flight


@async_test
async def test_a_cancellable_runtime_reports_its_rung_in_the_pass_and_the_reason(tmp_path, fakebd):
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    clock = Clock()
    outcome = RoleOutcome(status="failed", session_id="prov-x", cost_usd=0.5, cancel_rung="hard")
    rt = CancellableFake(outcome)
    loop = _loop(tmp_path, rt, caps=localloop.Caps(max_run_seconds=10.0), clock=clock)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())
    handle = loop.in_flight["b1"].handle
    clock.now += 10

    report = localloop.PassReport()
    await loop._enforce_wall_time(report)

    assert rt.cancelled == [handle]
    assert report.cancelled == (("b1", "hard"),)
    assert fake.set_state_reason("b1") == (
        "per-run wall-time cap 10.0s exceeded; cancelled at rung hard, session prov-x, cost 0.5"
    )
    assert fake.released("b1")


@async_test
async def test_shutdown_without_cancel_still_writes_the_cause_and_releases(tmp_path, fakebd):
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    loop = _loop(tmp_path, FakeRuntime())
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())

    await loop.shutdown()

    assert loop.in_flight == {}
    assert fake.set_state_reason("b1") == "loop shutting down (rung unsupported, session unknown)"
    assert fake.released("b1")


@async_test
async def test_shutdown_through_cancel_quotes_the_runtimes_rung_and_session(tmp_path, fakebd):
    fake = fakebd(FakeBd(children=[_child("b1", status="in_progress")]))
    rt = CancellableFake(RoleOutcome(status="handoff", session_id="prov-y", cancel_rung="signal"))
    loop = _loop(tmp_path, rt)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())

    await loop.shutdown()

    assert fake.set_state_reason("b1") == "loop shutting down (rung signal, session prov-y)"


# ---- sibling notification ------------------------------------------------------------------------


@async_test
async def test_siblings_are_notified_through_the_runtime(tmp_path, fakebd):
    fakebd(FakeBd(children=[_child("b1"), _child("b2"), _child("b3")]))
    rt = NotifiableFake(live={"b2"})
    loop = _loop(tmp_path, rt, caps=localloop.Caps(max_concurrency=3))
    for bead in ("b1", "b2", "b3"):
        await loop._spawn_for(
            bead, action="dispatch", role="developer", report=localloop.PassReport()
        )

    notified = await loop.notify_siblings("b1", "sibling b1 was cancelled")

    assert notified == ("b2",), "only a delivery the runtime confirmed counts"
    assert rt.notes == [("b2", "sibling b1 was cancelled"), ("b3", "sibling b1 was cancelled")]


@async_test
async def test_a_runtime_without_notify_notifies_nobody(tmp_path, fakebd):
    fakebd(FakeBd(children=[_child("b1"), _child("b2")]))
    loop = _loop(tmp_path, FakeRuntime(), caps=localloop.Caps(max_concurrency=2))
    for bead in ("b1", "b2"):
        await loop._spawn_for(
            bead, action="dispatch", role="developer", report=localloop.PassReport()
        )

    assert await loop.notify_siblings("b1", "anything") == ()


# ---- the orphan scan is for non-adoptable runtimes only ------------------------------------------


@async_test
async def test_an_adoptable_runtime_skips_the_startup_orphan_scan(tmp_path, fakebd, monkeypatch):
    fakebd(FakeBd(children=[_child("b1")]))

    def no_scan(*_a, **_k):
        raise AssertionError("an adoptable runtime's live sessions are not orphans")

    monkeypatch.setattr(localloop, "find_orphan_seats", no_scan)
    rt = FakeRuntime()
    rt.adoptable = True
    loop = _loop(tmp_path, rt, claim=lambda: localloop.ClaimResult(reason="empty_queue"))

    report = await loop.run_pass()

    assert report.orphans_reaped == ()


# ---- the default runtime, and the loop-start refusals --------------------------------------------


def test_the_default_runtime_is_a_local_runtime_built_from_the_loops_launch_arguments(tmp_path):
    loop = localloop.LocalLoop(
        hive_dir=tmp_path,
        epic="epic-1",
        actor="disp/loop",
        seat_command="bh-{role} --wrapped",
        seat_bundle="/bundles/b.json",
        envelope_grace=4.0,
        terminate_grace=6.0,
    )
    rt = loop.runtime
    assert isinstance(rt, localloop.LocalRuntime)
    assert (rt.seat_command, rt.seat_bundle, rt.envelope_grace, rt.terminate_grace) == (
        "bh-{role} --wrapped",
        "/bundles/b.json",
        4.0,
        6.0,
    )


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("claude", "Task-tool sub-agent fanout"),
        ("temporal", "bh-c6dk.4"),
        ("no-such-runtime", "no-such-runtime"),
    ],
)
def test_a_runtime_that_cannot_drive_the_loop_fails_at_start(value, needle):
    with pytest.raises(localloop.LoopRuntimeError, match=needle):
        localloop.loop_runtime({"work": {"runtime": value}}, None)


def test_a_factory_returning_a_non_runtime_fails_at_start(clean_registry):
    runtime.register_runtime("half-done", lambda cfg, entry: object())
    with pytest.raises(localloop.LoopRuntimeError, match="does not implement the Runtime"):
        localloop.loop_runtime({"work": {"runtime": "half-done"}}, None)


def test_the_hive_entry_selects_the_loop_runtime(clean_registry):
    host, hive = FakeRuntime(), FakeRuntime()
    runtime.register_runtime("host-rt", lambda cfg, entry: host)
    runtime.register_runtime("hive-rt", lambda cfg, entry: hive)
    cfg = {"work": {"runtime": "host-rt"}}
    assert localloop.loop_runtime(cfg, {"work": {"runtime": "hive-rt"}}) is hive
    assert localloop.loop_runtime(cfg, {}) is host


# ---- `bh work loop`: one instance for the gate hook and every seat -------------------------------


class _Captured:
    """Stands in for LocalLoop inside impl_loop and records what it was built with."""

    built: list[dict] = []

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)
        self.halted = False

    async def run(self, **_kw):
        return []


class _Config:
    """The getters `bh work loop` really uses (`work.config`, the consumer port) over an in-memory
    cfg — `impl_loop`'s ``api`` is the `work` module."""

    def __init__(self, cfg):
        self._cfg = cfg

    def load(self):
        return self._cfg

    def __getattr__(self, name):
        return getattr(work.config, name)


def _api(tmp_path, cfg, entry):
    return SimpleNamespace(
        config=_Config(cfg),
        guard=SimpleNamespace(guard_primary=lambda *_a, **_k: None),
        registry=SimpleNamespace(
            hive_dir_for=lambda _cfg, _hive: tmp_path,
            entry_for_dir=lambda _cfg, _main: entry,
        ),
        identity=SimpleNamespace(resolve_actor=lambda as_, default: as_ or default or "disp/x"),
        otel=SimpleNamespace(set_bead=lambda _epic: None),
        os=SimpleNamespace(environ={}),
        log=SimpleNamespace(add_file_sink=lambda _path: None),
        typer=typer,
        jsonout=SimpleNamespace(emit=lambda _payload: None),
        asyncio=asyncio,
    )


@pytest.fixture
def captured(monkeypatch):
    _Captured.built = []
    monkeypatch.setattr(localloop, "LocalLoop", _Captured)
    monkeypatch.setattr(
        localloop, "lease_keeper_for", lambda *_a, **_k: localloop.NullLeaseKeeper()
    )
    return _Captured.built


def _impl_loop(api, *, seat_binary=""):
    work_dispatch.impl_loop(api, "epic-1", "disp/x", "", 1, False, False, seat_binary)


def test_work_loop_hands_one_runtime_instance_to_the_gate_hook_and_the_loop(
    tmp_path, clean_registry, captured
):
    rt = FakeRuntime()
    runtime.register_runtime("hive-rt", lambda cfg, entry: rt)
    entry = {"work": {"runtime": "hive-rt"}}

    _impl_loop(_api(tmp_path, {"work": {"runtime": "local"}}, entry))

    (kwargs,) = captured
    assert kwargs["runtime"] is rt, "the per-hive entry's runtime, not the host default"
    hook = kwargs["on_gate_resolved"]
    assert hook.__self__ is rt and hook.__func__ is FakeRuntime.on_gate_resolved


def test_work_loop_default_is_a_local_runtime_carrying_seat_binary(tmp_path, captured):
    _impl_loop(_api(tmp_path, {}, {}), seat_binary="/bin/stub-seat")

    (kwargs,) = captured
    rt = kwargs["runtime"]
    assert isinstance(rt, localloop.LocalRuntime)
    assert (rt.seat_command, rt.seat_bundle) == ("/bin/stub-seat", "")
    assert kwargs["on_gate_resolved"].__self__ is rt


@pytest.mark.parametrize("value", ["claude", "temporal", "no-such-runtime"])
def test_work_loop_refuses_to_start_on_a_misconfigured_runtime(tmp_path, captured, capsys, value):
    with pytest.raises(typer.Exit) as exited:
        _impl_loop(_api(tmp_path, {"work": {"runtime": value}}, {}))

    assert exited.value.exit_code == 1
    assert "✗ runtime_unavailable:" in capsys.readouterr().err
    assert captured == [], "no loop is built, so nothing is claimed or spawned"


def test_work_loop_refuses_a_local_launch_override_for_a_third_party_runtime(
    tmp_path, clean_registry, captured, capsys
):
    runtime.register_runtime("hive-rt", lambda cfg, entry: FakeRuntime())

    with pytest.raises(typer.Exit):
        _impl_loop(_api(tmp_path, {"work": {"runtime": "hive-rt"}}, {}), seat_binary="/bin/x")

    assert "✗ runtime_conflict: --seat-binary" in capsys.readouterr().err
    assert captured == []


# ---- one spawn path ------------------------------------------------------------------------------


class _SpawnSeatCalls(ast.NodeVisitor):
    """Collect the enclosing scope of every ``spawn_seat(...)`` call in one module."""

    def __init__(self, module: str):
        self.module, self.scopes, self.found = module, [], set()

    def _scoped(self, node):
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    def visit_Call(self, node):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "spawn_seat":
            self.found.add((self.module, ".".join(self.scopes)))
        self.generic_visit(node)


def _spawn_seat_callers() -> set[tuple[str, str]]:
    """Every production call of ``spawn_seat`` under ``src/beadhive``, as (module, scope)."""
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "spawn_seat" not in text:  # a call needs the name; skip parsing everything else
            continue
        visitor = _SpawnSeatCalls(path.relative_to(SRC).as_posix())
        visitor.visit(ast.parse(text))
        found |= visitor.found
    return found


def test_spawn_seat_has_a_single_loop_side_caller_inside_local_runtime():
    """The operator decision: ONE spawn path. Inside the dispatch tier only
    ``LocalRuntime.schedule`` starts a seat — LocalLoop has no direct-spawn branch left.

    ``role_process._run_foreground`` is the other production caller and is deliberately listed,
    not hidden: it is the foreground ``bh role`` owner, i.e. the INSIDE of one seat (the role
    binary a runtime launches, forwarding its own packed child's output live), not a second
    scheduler. It never sees the loop, the in-flight map or a runtime handle."""
    assert _spawn_seat_callers() == {
        ("localloop.py", "LocalRuntime.schedule"),
        ("role_process.py", "_run_foreground"),
    }


# ---- telemetry alignment (bh-lhxy8 review item 7) ------------------------------------------------


def test_local_runtime_reports_no_system_without_a_routing_decision(tmp_path, monkeypatch):
    seen = []
    real = otel.record_agent_dispatch

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(otel, "record_agent_dispatch", spy)
    inst = tmp_path / "brief.md"
    inst.write_text("STUB_STATUS=done\n", encoding="utf-8")
    with localloop.LocalRuntime(seat_command=f"{sys.executable} {STUB_SEAT}") as rt:
        rt.schedule(
            "b1",
            "developer",
            workspace=_ws(tmp_path, "b1"),
            instructions=str(inst),
            session_id="p1",
        )
        rt.schedule(
            "b2",
            "developer",
            workspace=_ws(tmp_path, "b2"),
            instructions=str(inst),
            session_id="p2",
            decision=_selection(),
        )
    assert [(s["system"], s["model"]) for s in seen] == [
        ("", ""),
        ("claude", "anthropic/claude-sonnet-4-5"),
    ]
