"""Reusable broker conformance scenarios: safety, nested and killed-client.

Each scenario is a function of an engine harness, defined once and run by every engine: this
package runs them against :class:`~beadhive_validation_broker.InMemoryBroker` and
:class:`~beadhive_validation_broker.LocalEngine`, and the root package runs them against the
slots engine (``beadhive.validation_broker_slots``). An adapter supplies a
:class:`BrokerHarness`: a fresh broker per case plus the controls a scenario cannot take from the
port (kill a client, make the engine notice). A control an engine cannot provide raises
:class:`ScenarioSkipped`, and the case skips rather than passing vacuously.

Scenarios drive only the six port operations. They never sleep and never read a wall clock: a
grant is probed with ``wait_grant(timeout=0)``, so an engine that blocks for a grant must treat a
zero timeout as a non-blocking poll. Two optional harness members cover engines whose admission
is not synchronous or not sized:

* ``settle()`` returns once the engine has acted on every state change so far (an engine that
  admits on a holder thread waits for that thread's decision there, bounded by an event wait).
  Scenarios call it before every probe; a synchronous engine omits it.
* ``sized = False`` declares an engine that admits by count, not by a resource vector (the slots
  engine). Scenarios that need reservations to be sized then skip.

``pytest`` is imported only inside the functions that build marks and fixtures, so the module
stays importable with the standard library alone.

The group-latency replays, starvation bound, brake start-race and crash/reconcile checks of the
ADR's full kit are deliberately out of scope here (``bh-fmuqq.3``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ._errors import BrokerUnavailable, GrantRefused, GrantTimeout, LeaseLost
from ._fake import FakeAvailability, FakeDiskSpace, InMemoryBroker
from ._local import LocalEngine
from ._ports import BrokerPort
from ._roots import ExecutionRoot
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
    "HARNESS_BUDGET",
    "SCENARIOS",
    "BrokerHarness",
    "ManualClock",
    "Scenario",
    "ScenarioSkipped",
    "fake_harness",
    "granted",
    "job",
    "local_harness",
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
    expected_duration_s: float = 1.0,
    disk_bytes: int = 0,
) -> JobSpec:
    """A job with sensible defaults; scenarios override only what they test."""
    return JobSpec(
        job_id=job_id,
        command=("true",),
        reservation=Reservation(
            cpu_min,
            cpu_min if cpu_max is None else cpu_max,
            memory_bytes,
            dict(pools or {}),
            disk_bytes,
        ),
        priority=priority,
        expected_duration_s=expected_duration_s,
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


def _settle(harness: BrokerHarness) -> None:
    settle = getattr(harness, "settle", None)
    if settle is not None:
        settle()


def _probe(harness: BrokerHarness, handle: JobHandle) -> Grant | None:
    _settle(harness)
    return granted(harness.broker, handle)


def _must_grant(harness: BrokerHarness, handle: JobHandle, why: str) -> Grant:
    grant = _probe(harness, handle)
    assert grant is not None, f"{handle.job_id}: {why}"
    return grant


def _must_wait(harness: BrokerHarness, handle: JobHandle, why: str) -> None:
    assert _probe(harness, handle) is None, f"{handle.job_id}: {why}"


def _must_be_lost(harness: BrokerHarness, handle: JobHandle, why: str) -> None:
    _settle(harness)
    try:
        harness.broker.wait_grant(handle, timeout=0.0)
    except LeaseLost:
        return
    raise AssertionError(f"{handle.job_id}: {why}")


def _requires_sized(harness: BrokerHarness) -> None:
    if not getattr(harness, "sized", True):
        raise ScenarioSkipped("the engine admits by count; reservations are not sized")


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
        _settle(harness)
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
    _settle(harness)
    status = broker.status()
    assert status.running == 0 and status.queued == 0


def safety_refuses_what_can_never_fit(harness: BrokerHarness) -> None:
    _requires_sized(harness)
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
    _must_grant(harness, first, "the whole pool is free")
    second = broker.submit(job("p2", pools={name: 1}))
    _must_wait(harness, second, "the pool is exhausted; the second job must wait")
    broker.report(first, OK, USED)
    _must_grant(harness, second, "the pool was released")


def safety_report_and_cancel_are_idempotent(harness: BrokerHarness) -> None:
    broker = harness.broker
    running = broker.submit(job("a"))
    _must_grant(harness, running, "an idle engine admits a small job")
    broker.report(running, OK, USED)
    broker.report(running, OK, USED)
    broker.cancel(running, "late")
    holder = broker.submit(job("b", cpu_min=harness.budget.cpu))
    _must_grant(harness, holder, "the whole host is free again")
    waiting = broker.submit(job("c", cpu_min=harness.budget.cpu))
    _must_wait(harness, waiting, "the holder has the whole host")
    broker.cancel(holder, "not needed")
    broker.cancel(holder, "not needed")
    _must_be_lost(harness, holder, "a cancelled job must not be granted")
    _must_grant(harness, waiting, "cancelling the holder frees the host")


def safety_cancelled_queued_job_is_never_admitted(harness: BrokerHarness) -> None:
    broker = harness.broker
    holder = broker.submit(job("holder", cpu_min=harness.budget.cpu))
    _must_grant(harness, holder, "the host is idle")
    queued = broker.submit(job("queued", cpu_min=harness.budget.cpu))
    _must_wait(harness, queued, "the holder has the whole host")
    broker.cancel(queued, "not needed")
    broker.report(holder, OK, USED)
    _must_be_lost(harness, queued, "a job cancelled while queued must never be granted")
    _settle(harness)
    status = broker.status()
    assert (status.running, status.queued) == (0, 0), "nothing holds the host after the cancel"
    _assert_safe(harness, {})


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
    parent = _must_grant(harness, parent_handle, "the host is idle")
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
    child = _must_grant(
        harness, child_handle, "a nested request that fits the parent's grant is served at once"
    )
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
    _must_be_lost(harness, child_handle, "the nested lease must end with its parent")
    assert broker.status().running == 0


def nested_request_that_fits_later_waits_in_the_parent(harness: BrokerHarness) -> None:
    broker = harness.broker
    parent = _must_grant(harness, broker.submit(job("parent", cpu_min=2, cpu_max=2)), "idle")
    first_handle = broker.submit(job("n1", cpu_min=2, parent_lease=parent.lease_id))
    second_handle = broker.submit(job("n2", cpu_min=2, parent_lease=parent.lease_id))
    _must_grant(harness, first_handle, "the parent's grant is free")
    _must_wait(harness, second_handle, "the parent's grant is spent; wait inside it")
    broker.report(first_handle, OK, USED)
    _must_grant(harness, second_handle, "the sibling released its share of the parent")


def nested_request_never_enters_the_host_queue(harness: BrokerHarness) -> None:
    broker, budget = harness.broker, harness.budget
    parent_handle = broker.submit(job("parent", cpu_min=budget.cpu, cpu_max=budget.cpu))
    parent = _must_grant(harness, parent_handle, "the host is idle")
    blocked = broker.submit(job("blocked", cpu_min=budget.cpu))
    _must_wait(harness, blocked, "the parent holds the whole host")
    in_use = broker.status().in_use
    child_handle = broker.submit(job("child", cpu_min=1, parent_lease=parent.lease_id))
    _must_grant(harness, child_handle, "a nested request is served past a full host queue")
    _must_wait(harness, blocked, "the nested lease took nothing from the host")
    assert broker.status().in_use == in_use, "a nested lease is never counted host-wide"
    broker.report(child_handle, OK, USED)
    broker.report(parent_handle, OK, USED)
    _must_grant(harness, blocked, "the parent released the host")


def nested_leases_end_when_the_parent_is_cancelled(harness: BrokerHarness) -> None:
    broker = harness.broker
    parent_handle = broker.submit(job("parent", cpu_min=2, cpu_max=2))
    parent = _must_grant(harness, parent_handle, "the host is idle")
    running = broker.submit(job("n1", cpu_min=2, parent_lease=parent.lease_id))
    queued = broker.submit(job("n2", cpu_min=2, parent_lease=parent.lease_id))
    _must_grant(harness, running, "the parent's grant is free")
    _must_wait(harness, queued, "the parent's grant is spent")
    broker.cancel(parent_handle, "gate abandoned")
    _must_be_lost(harness, running, "a running nested lease ends with its cancelled parent")
    _must_be_lost(harness, queued, "a queued nested request ends with its cancelled parent")
    try:
        broker.submit(job("late", cpu_min=1, parent_lease=parent.lease_id))
    except LeaseLost:
        pass
    else:
        raise AssertionError("a nested request under an ended parent must raise LeaseLost")
    _settle(harness)
    assert broker.status().running == 0


# ---- killed client ------------------------------------------------------------------------


def killed_client_leases_and_nested_leases_are_reclaimed(harness: BrokerHarness) -> None:
    broker, budget = harness.broker, harness.budget
    victim, survivor = Owner(pid=4001, start_token="a"), Owner(pid=4002, start_token="b")
    parent_handle = broker.submit(
        job("parent", owner=victim, cpu_min=budget.cpu, memory_bytes=budget.memory_bytes // 2)
    )
    parent = _must_grant(harness, parent_handle, "the host is idle")
    child_handle = broker.submit(
        job("child", owner=victim, cpu_min=1, parent_lease=parent.lease_id)
    )
    _must_grant(harness, child_handle, "the child fits its parent")
    waiting_handle = broker.submit(job("waiting", owner=survivor, cpu_min=budget.cpu))
    _must_wait(harness, waiting_handle, "the dead client's lease still holds the host")

    harness.kill_owner(victim)
    harness.reap()

    _must_grant(harness, waiting_handle, "reclaim must free the capacity")
    for lost in (parent_handle, child_handle):
        _must_be_lost(harness, lost, "a dead owner's lease must be reclaimed")
    try:
        broker.report(parent_handle, OK, USED)
    except LeaseLost:
        pass
    else:
        raise AssertionError("report on a reclaimed lease must raise LeaseLost")
    reclaimed = [e for e in broker.events() if e.kind is EventKind.ORPHAN_RECLAIMED]
    assert {e.job_id for e in reclaimed} >= {"parent", "child"}
    _assert_safe(harness, {})


def killed_client_nested_lease_of_a_live_process_dies_with_its_parent(
    harness: BrokerHarness,
) -> None:
    broker = harness.broker
    victim, helper = Owner(pid=4101, start_token="a"), Owner(pid=4102, start_token="b")
    parent_handle = broker.submit(job("parent", owner=victim, cpu_min=2, cpu_max=2))
    parent = _must_grant(harness, parent_handle, "the host is idle")
    child_handle = broker.submit(
        job("child", owner=helper, cpu_min=1, parent_lease=parent.lease_id)
    )
    _must_grant(harness, child_handle, "the child fits its parent")
    harness.kill_owner(victim)
    harness.reap()
    _must_be_lost(harness, parent_handle, "the dead owner's lease is reclaimed")
    _must_be_lost(harness, child_handle, "a nested lease never outlives its parent's grant")
    _settle(harness)
    assert broker.status().running == 0


def killed_client_queued_job_is_dropped_and_live_leases_kept(harness: BrokerHarness) -> None:
    broker, budget = harness.broker, harness.budget
    victim, survivor = Owner(pid=4201, start_token="a"), Owner(pid=4202, start_token="b")
    holder = broker.submit(job("holder", owner=survivor, cpu_min=budget.cpu))
    grant = _must_grant(harness, holder, "the host is idle")
    queued = broker.submit(job("queued", owner=victim, cpu_min=budget.cpu))
    _must_wait(harness, queued, "the survivor holds the host")
    harness.kill_owner(victim)
    harness.reap()
    _must_be_lost(harness, queued, "a dead client's queued job is dropped")
    assert broker.wait_grant(holder, timeout=0.0) == grant, "a live owner's lease is untouched"
    broker.report(holder, OK, USED)
    _settle(harness)
    status = broker.status()
    assert (status.running, status.queued) == (0, 0), "the dropped job was never admitted"
    reclaimed = {e.job_id for e in broker.events() if e.kind is EventKind.ORPHAN_RECLAIMED}
    assert "queued" in reclaimed and "holder" not in reclaimed


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
        "cancelled_queued_job_is_never_admitted",
        "safety",
        safety_cancelled_queued_job_is_never_admitted,
    ),
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
    Scenario("never_enters_the_host_queue", "nested", nested_request_never_enters_the_host_queue),
    Scenario(
        "ends_when_the_parent_is_cancelled",
        "nested",
        nested_leases_end_when_the_parent_is_cancelled,
    ),
    Scenario(
        "reclaims_leases_and_nested_leases",
        "killed-client",
        killed_client_leases_and_nested_leases_are_reclaimed,
    ),
    Scenario(
        "nested_lease_of_a_live_process_dies_with_its_parent",
        "killed-client",
        killed_client_nested_lease_of_a_live_process_dies_with_its_parent,
    ),
    Scenario(
        "queued_job_is_dropped_and_live_leases_kept",
        "killed-client",
        killed_client_queued_job_is_dropped_and_live_leases_kept,
    ),
)


def run_scenario(scenario: Scenario, harness: BrokerHarness) -> None:
    scenario.run(harness)


#: The budget every in-package harness is built with: 8 cpu, 8 GiB, a 4-slot ``dolt-servers``.
HARNESS_BUDGET = Capacity(8.0, 8 * GIB, {"dolt-servers": 4})


class _FakeHarness:
    def __init__(self) -> None:
        self.clock = ManualClock()
        self.dead: set[Owner] = set()
        self.budget = HARNESS_BUDGET
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


class _LocalHarness:
    def __init__(self, roots: tuple[ExecutionRoot, ...] = ()) -> None:
        self.clock = ManualClock()
        self.dead: set[Owner] = set()
        self.budget = HARNESS_BUDGET
        # The host reports more than the budget even after the floor, so the budget binds.
        self.availability = FakeAvailability(Capacity(64.0, 64 * GIB))
        # Every root reports more free space than its bound, so the bound binds there too.
        self.disk_space = FakeDiskSpace({root.path: root.max_bytes * 2 for root in roots})
        self.broker = LocalEngine(
            self.budget,
            self.clock,
            availability=self.availability,
            floor=Capacity(0.0, 4 * GIB),
            owner_alive=lambda owner: owner not in self.dead,
            roots=roots,
            disk_space=self.disk_space if roots else None,
        )

    def kill_owner(self, owner: Owner) -> None:
        self.dead.add(owner)

    def reap(self) -> None:
        self.broker.reap()

    def set_available(self, available: bool) -> None:
        self.broker.set_available(available)


def local_harness(roots: tuple[ExecutionRoot, ...] = ()) -> BrokerHarness:
    """A fresh local engine on the same budget, with a fake clock and fake availability.

    ``roots`` places every key run on one execution root (``bh-mf1o4``), read through a fake
    disk-space probe; the safety contract must hold unchanged with roots configured.
    """
    return _LocalHarness(tuple(roots))


def parametrize_scenarios(*, groups: tuple[str, ...] | None = None) -> Any:
    """A ``pytest.mark.parametrize`` over the scenarios (``pytest`` imported lazily)."""
    import pytest

    chosen = [s for s in SCENARIOS if groups is None or s.group in groups]
    return pytest.mark.parametrize("scenario", chosen, ids=[f"{s.group}-{s.name}" for s in chosen])
