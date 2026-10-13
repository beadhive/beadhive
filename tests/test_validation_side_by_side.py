"""A gate's keys side by side through the broker: concurrency and the failure policy (bh-qhrl6).

Every port is a fake: a broker that admits a fixed number of jobs at once and blocks the rest, a
workspace whose ``execute`` the test scripts, and a manual clock. Concurrency is proven with a
``threading.Barrier`` every key must reach while the others are still running: a sequential
runner can never pass it. ``GENEROUS`` only bounds a wait so a broken runner fails instead of
hanging; no assertion depends on how long anything takes.
"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager

import pytest

from beadhive.validation_side_by_side import (
    GroupKey,
    KeyOutcome,
    group_priority,
    run_group,
    summary_lines,
)
from beadhive_validation_broker import (
    BrokerUnavailable,
    Capacity,
    Grant,
    GrantRefused,
    InMemoryBroker,
    JobHandle,
    LeaseLost,
    Owner,
    PriorityClass,
    Reservation,
)
from beadhive_validation_broker.contract import ManualClock

GENEROUS = 10.0
OWNER = Owner(pid=4242)


def _key(name: str, *, cpu: int = 1, required: bool = True) -> GroupKey:
    return GroupKey(name, f"just attest-{name}", Reservation(cpu, cpu, 0), 10.0, required)


class GatedBroker:
    """Admits at most ``capacity`` jobs at once in submission order; ``wait_grant`` blocks."""

    name = "gated"

    def __init__(self, capacity: int, *, refuse: frozenset[str] = frozenset(), root=None) -> None:
        self.capacity = capacity
        self.refuse = refuse
        self.root = root
        self.cond = threading.Condition()
        self.queue: list[str] = []
        self.running: set[str] = set()
        self.cancelled: dict[str, str] = {}
        self.reports: dict[str, int] = {}
        self.specs: dict[str, object] = {}

    def submit(self, job):
        with self.cond:
            if job.key in self.refuse:
                raise GrantRefused(f"{job.job_id}: too big")
            self.specs[job.job_id] = job
            self.queue.append(job.job_id)
            return JobHandle(job.job_id, self.name, job.job_id)

    def _admit(self) -> None:
        while self.queue and len(self.running) < self.capacity:
            self.running.add(self.queue.pop(0))

    def wait_grant(self, handle, timeout=None):
        with self.cond:
            assert self.cond.wait_for(
                lambda: (
                    (self._admit() or handle.job_id in self.running)
                    or handle.job_id in self.cancelled
                ),
                GENEROUS,
            ), f"{handle.job_id} never admitted"
            if handle.job_id in self.cancelled:
                raise LeaseLost(f"{handle.job_id}: cancelled")
            spec = self.specs[handle.job_id]
            cpu = spec.reservation.cpu_min
            return Grant(f"lease-{handle.job_id}", handle.job_id, Capacity(cpu, 0), root=self.root)

    def report(self, handle, outcome, usage):
        with self.cond:
            self.reports[handle.job_id] = outcome.exit_code
            self.running.discard(handle.job_id)
            self.cond.notify_all()

    def cancel(self, handle, reason):
        with self.cond:
            if handle.job_id in self.reports:
                return
            self.cancelled.setdefault(handle.job_id, reason)
            if handle.job_id in self.queue:
                self.queue.remove(handle.job_id)
            self.running.discard(handle.job_id)
            self.cond.notify_all()

    def status(self):  # pragma: no cover - unused by the runner
        raise NotImplementedError

    def events(self, since=0):  # pragma: no cover - unused by the runner
        return iter(())


class FakeWorkspace:
    """Counts setups/teardowns and runs each key through a scripted function."""

    def __init__(self, script=None, *, root=None, fail_setup: bool = False) -> None:
        self.script = script or (lambda key, grant: 0)
        self.root = root
        self.fail_setup = fail_setup
        self.setups = 0
        self.teardowns = 0
        self.executed: list[str] = []
        self.grants: dict[str, Grant] = {}
        self.lock = threading.Lock()

    @contextmanager
    def __call__(self, grant):
        self.setups += 1
        if self.fail_setup:
            raise RuntimeError("uv sync exited 1")
        try:
            yield self
        finally:
            self.teardowns += 1

    def execute(self, key, grant):
        with self.lock:
            self.executed.append(key.name)
            self.grants[key.name] = grant
        return self.script(key, grant)


def _run(broker, keys, workspace, **extra):
    reported: list[KeyOutcome] = []
    warnings: list[str] = []
    result = run_group(
        broker,
        keys,
        group="gate-1",
        priority=PriorityClass.CHECK_SUBMIT,
        owner=OWNER,
        workspace=workspace,
        report=reported.append,
        warn=warnings.append,
        **extra,
    )
    return result, reported, warnings


def test_granted_keys_run_concurrently_in_one_shared_workspace() -> None:
    keys = [_key("a"), _key("b"), _key("c")]
    rendezvous = threading.Barrier(len(keys), timeout=GENEROUS)

    def script(key, grant):
        rendezvous.wait()  # breaks (and fails the key) unless all three run at once
        return 0

    workspace = FakeWorkspace(script)
    result, reported, warnings = _run(GatedBroker(capacity=3), keys, workspace)

    assert {o.name: o.rc for o in result.outcomes.values()} == {"a": 0, "b": 0, "c": 0}
    assert sorted(workspace.executed) == ["a", "b", "c"]
    assert (workspace.setups, workspace.teardowns) == (1, 1)  # one per-tree setup job
    assert sorted(o.name for o in reported) == ["a", "b", "c"]
    assert result.fallback == {} and warnings == []


def test_every_key_is_one_job_of_one_gate_group_with_its_reservation() -> None:
    broker = GatedBroker(capacity=3)
    keys = [_key("a", cpu=2), _key("b", cpu=4)]
    _run(broker, keys, FakeWorkspace())

    specs = list(broker.specs.values())
    assert {spec.group for spec in specs} == {"gate-1"}
    assert [spec.key for spec in specs] == ["a", "b"]
    assert [spec.reservation.cpu_min for spec in specs] == [2, 4]
    assert [spec.command for spec in specs] == [("just attest-a",), ("just attest-b",)]
    assert {spec.owner for spec in specs} == {OWNER}
    assert {spec.priority for spec in specs} == {PriorityClass.CHECK_SUBMIT}
    assert broker.reports == {"gate-1/a": 0, "gate-1/b": 0}  # every lease released


def test_a_key_runs_with_its_own_grant() -> None:
    workspace = FakeWorkspace()
    _run(GatedBroker(capacity=2), [_key("a", cpu=2), _key("b", cpu=4)], workspace)
    assert workspace.grants["a"].env()["BH_GRANT_CPUS"] == "2"
    assert workspace.grants["b"].env()["BH_GRANT_CPUS"] == "4"


def test_the_group_finishes_after_a_red_key_by_default() -> None:
    """Failure policy: a red key never stops its siblings; every key gets its own verdict."""
    rcs = {"a": 1, "b": 0, "c": 75}
    broker = GatedBroker(capacity=1)  # strictly one after another: a is red before b starts
    result, reported, _ = _run(
        broker, [_key("a"), _key("b"), _key("c")], FakeWorkspace(lambda k, g: rcs[k.name])
    )
    assert {o.name: o.rc for o in result.outcomes.values()} == rcs
    assert sorted(o.name for o in reported) == ["a", "b", "c"]
    assert result.stopped_after == "" and broker.cancelled == {}


def test_fail_fast_starts_no_further_key_but_lets_running_ones_finish() -> None:
    broker = GatedBroker(capacity=2)
    b_started = threading.Event()
    a_reported = threading.Event()

    def script(key, grant):
        if key.name == "a":
            assert b_started.wait(GENEROUS)
            return 1
        if key.name == "b":
            b_started.set()
            assert a_reported.wait(GENEROUS)  # b is still running when a turns red
            return 0
        return 0  # c must never start

    reported: list[KeyOutcome] = []

    def report(outcome):
        reported.append(outcome)
        if outcome.name == "a":
            a_reported.set()

    result = run_group(
        broker,
        [_key("a"), _key("b"), _key("c")],
        group="gate-1",
        priority=PriorityClass.CHECK_SUBMIT,
        owner=OWNER,
        workspace=FakeWorkspace(script),
        report=report,
        fail_fast=True,
    )
    assert result.outcomes["a"].rc == 1
    assert result.outcomes["b"].rc == 0  # running: finished, its verdict recorded
    assert result.outcomes["c"].rc is None  # queued: cancelled, never started
    assert result.stopped_after == "a"
    assert broker.cancelled == {"gate-1/c": "fail-fast after a"}
    assert "gate-1/b" not in broker.cancelled  # a running key's lease is never cancelled


def test_fail_fast_ignores_an_optional_unknown() -> None:
    result, _, _ = _run(
        GatedBroker(capacity=1),
        [_key("a", required=False), _key("b")],
        FakeWorkspace(lambda k, g: 75 if k.name == "a" else 0),
        fail_fast=True,
    )
    assert {o.name: o.rc for o in result.outcomes.values()} == {"a": 75, "b": 0}
    assert result.stopped_after == ""


def test_a_key_the_engine_refuses_falls_back_and_the_rest_run() -> None:
    broker = GatedBroker(capacity=2, refuse=frozenset({"big"}))
    workspace = FakeWorkspace()
    result, _, warnings = _run(broker, [_key("big"), _key("a")], workspace)
    assert list(result.fallback) == ["big"] and "refused" in result.fallback["big"]
    assert workspace.executed == ["a"]
    assert warnings == []  # a contract answer, not an engine failure


def test_an_engine_failure_sends_every_unstarted_key_to_the_slots_path_loudly() -> None:
    a_running = threading.Event()
    b_failed = threading.Event()

    class DyingBroker(GatedBroker):
        def wait_grant(self, handle, timeout=None):
            if handle.job_id == "gate-1/b":
                assert a_running.wait(GENEROUS)  # a holds its grant and is running
                raise BrokerUnavailable("engine died")
            return super().wait_grant(handle, timeout)

    broker = DyingBroker(capacity=1)

    def script(key, grant):
        a_running.set()
        assert b_failed.wait(GENEROUS)
        return 0

    workspace = FakeWorkspace(script)

    def watch(message):
        warnings.append(message)
        b_failed.set()

    warnings: list[str] = []
    result = run_group(
        broker,
        [_key("a"), _key("b"), _key("c")],
        group="gate-1",
        priority=PriorityClass.CHECK_SUBMIT,
        owner=OWNER,
        workspace=workspace,
        report=lambda _o: None,
        warn=watch,
    )
    assert result.outcomes["a"].rc == 0  # it held its grant: it finished
    assert list(result.fallback) == ["b", "c"]
    assert len(warnings) == 1 and "local engine failed" in warnings[0]
    assert "sequential slots path" in warnings[0]
    assert workspace.executed == ["a"]


def test_a_failing_setup_job_falls_back_every_key() -> None:
    workspace = FakeWorkspace(fail_setup=True)
    result, reported, warnings = _run(GatedBroker(capacity=2), [_key("a"), _key("b")], workspace)
    assert list(result.fallback) == ["a", "b"]
    assert all("uv sync exited 1" in why for why in result.fallback.values())
    assert workspace.executed == [] and reported == []
    assert len(warnings) == 1


def test_a_grant_on_another_root_never_runs_in_the_shared_checkout() -> None:
    class SplitBroker(GatedBroker):
        def wait_grant(self, handle, timeout=None):
            grant = super().wait_grant(handle, timeout)
            root = "/roots/one" if handle.job_id.endswith("/a") else "/roots/two"
            return Grant(grant.lease_id, grant.job_id, grant.amounts, root=root)

    workspace = FakeWorkspace(root="/roots/one")
    broker = SplitBroker(capacity=1)
    result, _, _ = _run(broker, [_key("a"), _key("b")], workspace)
    assert workspace.executed == ["a"]
    assert list(result.fallback) == ["b"] and "/roots/two" in result.fallback["b"]


def test_wall_times_come_from_the_clock_port() -> None:
    clock = ManualClock()
    walls = {"a": 30.0, "b": 5.0}

    def script(key, grant):
        clock.advance(walls[key.name])
        return 0

    result, _, _ = _run(
        GatedBroker(capacity=1), [_key("a"), _key("b")], FakeWorkspace(script), clock=clock
    )
    assert {o.name: o.wall_s for o in result.outcomes.values()} == walls
    assert result.wall_s == 35.0
    lines = list(summary_lines(result))
    assert "group wall 35.000s" in lines[0] and "sum of key walls 35.000s" in lines[0]
    assert lines[1].strip() == "a: 30.000s" and lines[2].strip() == "b: 5.000s"


def test_keys_see_the_callers_context() -> None:
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
    seen: list[str] = []
    token = marker.set("scoped-cfg")
    try:
        _run(
            GatedBroker(capacity=2),
            [_key("a"), _key("b")],
            FakeWorkspace(lambda k, g: seen.append(marker.get()) or 0),
        )
    finally:
        marker.reset(token)
    assert seen == ["scoped-cfg", "scoped-cfg"]


def test_an_exception_in_a_key_is_raised_after_the_group_and_its_lease_released() -> None:
    broker = GatedBroker(capacity=2)

    class Boom(Exception):
        pass

    def script(key, grant):
        if key.name == "a":
            raise Boom
        return 0

    workspace = FakeWorkspace(script)
    with pytest.raises(Boom):
        _run(broker, [_key("a"), _key("b")], workspace)
    assert broker.reports == {"gate-1/a": 1, "gate-1/b": 0}
    assert workspace.teardowns == 1


def test_the_in_memory_fake_broker_runs_a_group_too() -> None:
    broker = InMemoryBroker(Capacity(8, 1 << 30), ManualClock())
    result, _, _ = _run(broker, [_key("a"), _key("b")], FakeWorkspace())
    assert {o.name: o.rc for o in result.outcomes.values()} == {"a": 0, "b": 0}
    assert sorted(r.job_id for r in broker.reports) == ["gate-1/a", "gate-1/b"]


@pytest.mark.parametrize(
    ("phase", "priority"),
    [
        ("check", PriorityClass.CHECK_SUBMIT),
        ("review", PriorityClass.CHECK_SUBMIT),
        ("merge", PriorityClass.MERGE_LAND),
        ("push-main", PriorityClass.MERGE_LAND),
        (None, PriorityClass.MERGE_LAND),
    ],
)
def test_group_priority(phase, priority) -> None:
    assert group_priority(phase) is priority
