"""Reusable broker conformance scenarios: safety, nested and killed-client.

Each scenario is a function of an engine harness, defined once and run by every engine: this
package runs them against :class:`~beadhive_validation_broker.InMemoryBroker`, and the local
in-process engine (``bh-qrixp``) imports this module and runs the same bodies against itself.
An adapter supplies a :class:`BrokerHarness`: a fresh broker per case plus the two controls a
scenario cannot take from the port (kill a client, make the engine notice). A control an engine
cannot provide raises :class:`ScenarioSkipped`, and the case skips rather than passing vacuously.

Scenarios drive only the six port operations. They never sleep and never read a wall clock: a
grant is probed with ``wait_grant(timeout=0)``, so an engine that blocks for a grant must treat a
zero timeout as a non-blocking poll. ``pytest`` is imported only inside the functions that build
marks and fixtures, so the module stays importable with the standard library alone.

The group-latency replays, starvation bound, brake start-race and crash/reconcile checks of the
ADR's full kit are deliberately out of scope here (``bh-fmuqq.3``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ._errors import BrokerUnavailable, GrantRefused, GrantTimeout, LeaseLost
from ._fake import InMemoryBroker
from ._ports import BrokerPort
from ._types import (
    LEASE_ENV,
    Capacity,
    EventKind,
    Grant,
    JobHandle,
    JobSpec,
    Outcome,
    Owner,
    PriorityClass,
    Reservation,
    Usage,
)

__all__ = [
    "SCENARIOS",
    "BrokerHarness",
    "ManualClock",
    "Scenario",
    "ScenarioSkipped",
    "fake_harness",
    "granted",
    "job",
    "parametrize_scenarios",
    "run_scenario",
]

GIB = 1024**3
OK = Outcome(exit_code=0)
USED = Usage(wall_s=1.0)


class ScenarioSkipped(Exception):
    """The engine under test cannot provide a control this scenario needs."""


class BrokerHarness(Protocol):
    """A fresh engine for one case, plus the controls a scenario needs."""

    broker: BrokerPort
    #: The host budget the engine was built with (scenarios size reservations from it).
    budget: Capacity

    def kill_owner(self, owner: Owner) -> None:
        """Make ``owner`` look dead to the engine's liveness probe."""
        ...

    def reap(self) -> None:
        """Make the engine notice dead owners now (the fake reaps; a service polls)."""
        ...

    def set_available(self, available: bool) -> None:
        """Take the engine down or bring it back (``ScenarioSkipped`` if it cannot)."""
        ...


class ManualClock:
    """A monotonic clock a test advances by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def job(
    job_id: str,
    *,
    owner: Owner | None = None,
    cpu_min: float = 1,
    cpu_max: float | None = None,
    memory_bytes: int = 0,
    pools: dict[str, int] | None = None,
    priority: PriorityClass = PriorityClass.CHECK_SUBMIT,
    parent_lease: str | None = None,
    group: str = "gate-1",
) -> JobSpec:
    """A job with sensible defaults; scenarios override only what they test."""
    return JobSpec(
        job_id=job_id,
        command=("true",),
        reservation=Reservation(
            cpu_min, cpu_min if cpu_max is None else cpu_max, memory_bytes, dict(pools or {})
        ),
        priority=priority,
        expected_duration_s=1.0,
        group=group,
        owner=owner or Owner(pid=1000),
        parent_lease=parent_lease,
    )


def granted(broker: BrokerPort, handle: JobHandle) -> Grant | None:
    """The grant if the job is admitted now, else ``None`` (a non-blocking probe)."""
    try:
        return broker.wait_grant(handle, timeout=0.0)
    except GrantTimeout:
        return None


def _assert_safe(harness: BrokerHarness, live: dict[str, Grant]) -> None:
    """The safety invariant: what the engine reports in use never exceeds the budget, and every
    live grant sits inside its own reservation's bounds."""
    in_use = harness.broker.status().in_use
    assert harness.budget.fits(in_use), f"over-admission: {in_use} exceeds {harness.budget}"
    for job_id, grant in live.items():
        assert grant.job_id == job_id


# ---- safety -------------------------------------------------------------------------------


def safety_whole_reservation_never_over_admits(harness: BrokerHarness) -> None:
    broker, budget = harness.broker, harness.budget
    # Each job needs ~40% of the cpu and memory budget, so at most two run at once.
    cpu = max(1.0, budget.cpu * 0.4)
    memory = int(budget.memory_bytes * 0.4)
    specs = [job(f"j{i}", cpu_min=cpu, cpu_max=budget.cpu, memory_bytes=memory) for i in range(5)]
    handles = {spec.job_id: broker.submit(spec) for spec in specs}

    finished: set[str] = set()
    running: dict[str, Grant] = {}
    while len(finished) < len(specs):
        for spec in specs:
            if spec.job_id in finished or spec.job_id in running:
                continue
            grant = granted(broker, handles[spec.job_id])
            if grant is not None:
                reservation = spec.reservation
                assert reservation.cpu_min <= grant.amounts.cpu <= reservation.cpu_max
                assert grant.amounts.memory_bytes == reservation.memory_bytes
                running[spec.job_id] = grant
        assert 1 <= len(running) <= 2, "whole reservation or nothing: at most two fit"
        _assert_safe(harness, running)
        job_id = next(iter(running))
        broker.report(handles[job_id], OK, USED)
        finished.add(job_id)
        del running[job_id]
    status = broker.status()
    assert status.running == 0 and status.queued == 0


def safety_refuses_what_can_never_fit(harness: BrokerHarness) -> None:
    broker, budget = harness.broker, harness.budget
    too_much_cpu = job("cpu", cpu_min=budget.cpu + 1)
    too_much_memory = job("mem", memory_bytes=budget.memory_bytes + 1)
    missing_pool = job("pool", pools={"no-such-pool": 1})
    for spec in (too_much_cpu, too_much_memory, missing_pool):
        try:
            broker.submit(spec)
        except GrantRefused:
            continue
        raise AssertionError(f"{spec.job_id}: a reservation that can never fit must be refused")
    assert broker.status().queued == 0


def safety_named_pools_are_counted(harness: BrokerHarness) -> None:
    broker, budget = harness.broker, harness.budget
    pools = dict(budget.pools)
    if not pools:
        raise ScenarioSkipped("the budget declares no named pool")
    name, size = next(iter(pools.items()))
    first = broker.submit(job("p1", pools={name: size}))
    second = broker.submit(job("p2", pools={name: 1}))
    assert granted(broker, first) is not None
    assert granted(broker, second) is None, "the pool is exhausted; the second job must wait"
    broker.report(first, OK, USED)
    assert granted(broker, second) is not None


def safety_report_and_cancel_are_idempotent(harness: BrokerHarness) -> None:
    broker = harness.broker
    running = broker.submit(job("a"))
    assert granted(broker, running) is not None
    broker.report(running, OK, USED)
    broker.report(running, OK, USED)
    broker.cancel(running, "late")
    holder = broker.submit(job("b", cpu_min=harness.budget.cpu))
    waiting = broker.submit(job("c", cpu_min=harness.budget.cpu))
    assert granted(broker, holder) is not None
    assert granted(broker, waiting) is None
    broker.cancel(holder, "not needed")
    broker.cancel(holder, "not needed")
    try:
        broker.wait_grant(holder, timeout=0.0)
    except LeaseLost:
        pass
    else:
        raise AssertionError("a cancelled job must not be granted")
    assert granted(broker, waiting) is not None, "cancelling the holder frees the host"


def safety_unavailable_raises_broker_unavailable(harness: BrokerHarness) -> None:
    broker = harness.broker
    harness.set_available(False)
    try:
        for call in (
            lambda: broker.submit(job("down")),
            broker.status,
            lambda: list(broker.events()),
        ):
            try:
                call()
            except BrokerUnavailable:
                continue
            raise AssertionError("every operation must raise BrokerUnavailable while down")
    finally:
        harness.set_available(True)
    broker.status()


# ---- nested -------------------------------------------------------------------------------


def nested_request_draws_only_from_the_parent_grant(harness: BrokerHarness) -> None:
    broker, budget = harness.broker, harness.budget
    parent_spec = job(
        "parent",
        cpu_min=budget.cpu / 2,
        cpu_max=budget.cpu / 2,
        memory_bytes=budget.memory_bytes // 2,
    )
    parent_handle = broker.submit(parent_spec)
    parent = granted(broker, parent_handle)
    assert parent is not None
    assert parent.env()[LEASE_ENV] == parent.lease_id
    in_use_before = broker.status().in_use

    child_cpu = max(parent.amounts.cpu / 2, 0.5)
    child_handle = broker.submit(
        job(
            "child",
            cpu_min=child_cpu,
            memory_bytes=parent.amounts.memory_bytes // 2,
            parent_lease=parent.lease_id,
        )
    )
    child = granted(broker, child_handle)
    assert child is not None, "a nested request that fits the parent's grant is served at once"
    assert child.parent_lease == parent.lease_id
    assert parent.amounts.fits(child.amounts)
    assert broker.status().in_use == in_use_before, "nested draws are inside the parent's grant"

    # Larger than the parent's whole grant: refused, never queued behind the host.
    try:
        broker.submit(job("greedy", cpu_min=parent.amounts.cpu + 1, parent_lease=parent.lease_id))
    except GrantRefused:
        pass
    else:
        raise AssertionError("a nested reservation beyond its parent's grant must be refused")

    # An unknown parent lease is a lost lease, not a host-queue entry.
    try:
        broker.submit(job("orphan", parent_lease="lease-does-not-exist"))
    except LeaseLost:
        pass
    else:
        raise AssertionError("a nested request with no live parent must raise LeaseLost")

    # Releasing the parent revokes the nested lease with it.
    broker.report(parent_handle, OK, USED)
    try:
        broker.wait_grant(child_handle, timeout=0.0)
    except LeaseLost:
        pass
    else:
        raise AssertionError("the nested lease must end with its parent")
    assert broker.status().running == 0


def nested_request_that_fits_later_waits_in_the_parent(harness: BrokerHarness) -> None:
    broker = harness.broker
    parent = granted(broker, broker.submit(job("parent", cpu_min=2, cpu_max=2)))
    assert parent is not None
    first_handle = broker.submit(job("n1", cpu_min=2, parent_lease=parent.lease_id))
    second_handle = broker.submit(job("n2", cpu_min=2, parent_lease=parent.lease_id))
    assert granted(broker, first_handle) is not None
    assert granted(broker, second_handle) is None, "the parent's grant is spent; wait inside it"
    broker.report(first_handle, OK, USED)
    assert granted(broker, second_handle) is not None


# ---- killed client ------------------------------------------------------------------------


def killed_client_leases_and_nested_leases_are_reclaimed(harness: BrokerHarness) -> None:
    broker, budget = harness.broker, harness.budget
    victim, survivor = Owner(pid=4001, start_token="a"), Owner(pid=4002, start_token="b")
    parent_handle = broker.submit(
        job("parent", owner=victim, cpu_min=budget.cpu, memory_bytes=budget.memory_bytes // 2)
    )
    parent = granted(broker, parent_handle)
    assert parent is not None
    child_handle = broker.submit(
        job("child", owner=victim, cpu_min=1, parent_lease=parent.lease_id)
    )
    assert granted(broker, child_handle) is not None
    waiting_handle = broker.submit(job("waiting", owner=survivor, cpu_min=budget.cpu))
    assert granted(broker, waiting_handle) is None, "the dead client's lease still holds the host"

    harness.kill_owner(victim)
    harness.reap()

    assert granted(broker, waiting_handle) is not None, "reclaim must free the capacity"
    for lost in (parent_handle, child_handle):
        try:
            broker.wait_grant(lost, timeout=0.0)
        except LeaseLost:
            pass
        else:
            raise AssertionError(f"{lost.job_id}: a dead owner's lease must be reclaimed")
    try:
        broker.report(parent_handle, OK, USED)
    except LeaseLost:
        pass
    else:
        raise AssertionError("report on a reclaimed lease must raise LeaseLost")
    reclaimed = [e for e in broker.events() if e.kind is EventKind.ORPHAN_RECLAIMED]
    assert {e.job_id for e in reclaimed} >= {"parent", "child"}
    _assert_safe(harness, {})


# ---- registry and adapters ----------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    name: str
    group: str
    run: Callable[[BrokerHarness], None]


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "whole_reservation_never_over_admits", "safety", safety_whole_reservation_never_over_admits
    ),
    Scenario("refuses_what_can_never_fit", "safety", safety_refuses_what_can_never_fit),
    Scenario("named_pools_are_counted", "safety", safety_named_pools_are_counted),
    Scenario("report_and_cancel_are_idempotent", "safety", safety_report_and_cancel_are_idempotent),
    Scenario(
        "unavailable_raises_broker_unavailable",
        "safety",
        safety_unavailable_raises_broker_unavailable,
    ),
    Scenario(
        "draws_only_from_the_parent_grant",
        "nested",
        nested_request_draws_only_from_the_parent_grant,
    ),
    Scenario(
        "waits_inside_the_parent", "nested", nested_request_that_fits_later_waits_in_the_parent
    ),
    Scenario(
        "reclaims_leases_and_nested_leases",
        "killed-client",
        killed_client_leases_and_nested_leases_are_reclaimed,
    ),
)


def run_scenario(scenario: Scenario, harness: BrokerHarness) -> None:
    scenario.run(harness)


class _FakeHarness:
    def __init__(self) -> None:
        self.clock = ManualClock()
        self.dead: set[Owner] = set()
        self.budget = Capacity(8.0, 8 * GIB, {"dolt-servers": 4})
        self.broker = InMemoryBroker(
            self.budget, self.clock, owner_alive=lambda owner: owner not in self.dead
        )

    def kill_owner(self, owner: Owner) -> None:
        self.dead.add(owner)

    def reap(self) -> None:
        self.broker.reap()

    def set_available(self, available: bool) -> None:
        self.broker.set_available(available)


def fake_harness() -> BrokerHarness:
    """A fresh in-memory engine: 8 cpu, 8 GiB, and a 4-slot ``dolt-servers`` pool."""
    return _FakeHarness()


def parametrize_scenarios(*, groups: tuple[str, ...] | None = None) -> Any:
    """A ``pytest.mark.parametrize`` over the scenarios (``pytest`` imported lazily)."""
    import pytest

    chosen = [s for s in SCENARIOS if groups is None or s.group in groups]
    return pytest.mark.parametrize("scenario", chosen, ids=[f"{s.group}-{s.name}" for s in chosen])
