"""The local engine: reservation admission, group-aware ordering, EASY, flex, nested, reclaim.

Every test owns time (``ManualClock``) and host availability (``FakeAvailability``); nothing
sleeps and nothing reads the real host. The one blocking ``wait_grant`` test is event-driven:
the waiter is woken by a ``report``, and the join bound is never a delay the test depends on.
"""

from __future__ import annotations

import threading

import pytest

from beadhive_validation_broker import (
    BrakeLevel,
    BrokerPort,
    BrokerUnavailable,
    Capacity,
    EventKind,
    FakeAvailability,
    GrantRefused,
    LeaseLost,
    LocalEngine,
    Outcome,
    Owner,
    PackerPolicy,
    PriorityClass,
    Usage,
)
from beadhive_validation_broker.contract import ManualClock, granted, job

GIB = 1024**3
OK, USED = Outcome(0), Usage(wall_s=1.0)
GENEROUS = 10.0  # an upper bound on an event wait, never a delay a test depends on
BUDGET = Capacity(16.0, 32 * GIB, {"dolt-servers": 4})
NO_FLOOR = Capacity(0.0, 0)


def _engine(
    budget: Capacity = BUDGET,
    *,
    available: Capacity | None = None,
    floor: Capacity = NO_FLOOR,
    policy: PackerPolicy | None = None,
    dead: set[Owner] | None = None,
) -> tuple[LocalEngine, ManualClock, FakeAvailability]:
    clock = ManualClock(1000.0)
    availability = FakeAvailability(available or Capacity(256.0, 256 * GIB))
    dead_owners = dead if dead is not None else set()
    engine = LocalEngine(
        budget,
        clock,
        availability=availability,
        floor=floor,
        policy=policy or PackerPolicy(),
        owner_alive=lambda owner: owner not in dead_owners,
    )
    return engine, clock, availability


def _running(engine: LocalEngine) -> set[str]:
    admitted = {e.job_id for e in engine.events() if e.kind is EventKind.ADMITTED}
    ended = {
        e.job_id
        for e in engine.events()
        if e.kind in (EventKind.FINISHED, EventKind.CANCELLED, EventKind.ORPHAN_RECLAIMED)
    }
    return admitted - ended


def test_local_engine_satisfies_the_port() -> None:
    engine, _, _ = _engine()
    assert isinstance(engine, BrokerPort)
    assert engine.name == "local"


# ---- admission against min(budget, measured available - floor) ------------------------------


def test_memory_is_admitted_against_measured_availability_less_the_floor() -> None:
    engine, _, availability = _engine(
        available=Capacity(256.0, 10 * GIB), floor=Capacity(0.0, 4 * GIB)
    )
    first = engine.submit(job("a", memory_bytes=4 * GIB))
    second = engine.submit(job("b", memory_bytes=4 * GIB))
    assert granted(engine, first) is not None
    assert granted(engine, second) is None, "6 GiB room: 4 GiB held, the second 4 GiB must wait"
    assert engine.status().in_use.memory_bytes == 4 * GIB

    availability.set(Capacity(256.0, 20 * GIB))
    engine.reap()  # any admission pass re-reads availability
    assert granted(engine, second) is not None
    assert engine.status().in_use.memory_bytes <= 20 * GIB - 4 * GIB


def test_cpu_is_admitted_against_the_smaller_of_budget_and_measured() -> None:
    engine, _, availability = _engine(available=Capacity(6.0, 256 * GIB))
    wide = engine.submit(job("wide", cpu_min=8))
    assert granted(engine, wide) is None, "the host reports 6 cpu: an 8-cpu minimum waits"
    assert engine.status().queued == 1, "waiting on measured capacity is not a refusal"
    availability.set(Capacity(64.0, 256 * GIB))
    engine.reap()
    assert granted(engine, wide) is not None


def test_the_budget_binds_when_the_host_reports_more() -> None:
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB), available=Capacity(64.0, 64 * GIB))
    first = engine.submit(job("a", cpu_min=3, memory_bytes=GIB))
    second = engine.submit(job("b", cpu_min=2, memory_bytes=GIB))
    assert granted(engine, first) is not None
    assert granted(engine, second) is None
    assert engine.status().in_use.cpu <= 4.0


def test_named_pools_are_never_over_admitted() -> None:
    engine, _, _ = _engine()
    handles = [engine.submit(job(f"p{i}", pools={"dolt-servers": 2})) for i in range(3)]
    assert [granted(engine, h) is not None for h in handles] == [True, True, False]
    assert engine.status().in_use.pools["dolt-servers"] == 4
    engine.report(handles[0], OK, USED)
    assert granted(engine, handles[2]) is not None
    assert engine.status().in_use.pools["dolt-servers"] == 4


def test_a_measured_pool_clamps_the_configured_pool() -> None:
    engine, _, _ = _engine(available=Capacity(256.0, 256 * GIB, {"dolt-servers": 1}))
    first = engine.submit(job("a", pools={"dolt-servers": 1}))
    second = engine.submit(job("b", pools={"dolt-servers": 1}))
    assert granted(engine, first) is not None
    assert granted(engine, second) is None


def test_a_reservation_beyond_the_budget_is_refused_not_queued() -> None:
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB, {"dolt-servers": 2}))
    for spec in (
        job("cpu", cpu_min=5),
        job("mem", memory_bytes=5 * GIB),
        job("pool", pools={"dolt-servers": 3}),
        job("unknown-pool", pools={"nope": 1}),
    ):
        with pytest.raises(GrantRefused):
            engine.submit(spec)
    assert engine.status().queued == 0


def test_a_failing_availability_probe_refuses_new_work_without_queueing_it() -> None:
    engine, _, availability = _engine()
    availability.fail()
    with pytest.raises(BrokerUnavailable):
        engine.submit(job("a"))
    availability.recover()
    assert engine.status().queued == 0


def test_a_probe_failure_while_queued_drops_the_job_and_reports_unavailable() -> None:
    engine, _, availability = _engine(Capacity(4.0, 4 * GIB))
    holder = engine.submit(job("holder", cpu_min=4))
    queued = engine.submit(job("queued", cpu_min=4))
    availability.fail()
    engine.report(holder, OK, USED)  # release still works; admission sees the failure
    with pytest.raises(BrokerUnavailable):
        engine.wait_grant(queued, timeout=0.0)
    availability.recover()
    with pytest.raises(LeaseLost):
        engine.wait_grant(queued, timeout=0.0)
    assert engine.status().running == 0, "a client that fell back leaves no ghost lease"


# ---- ordering -----------------------------------------------------------------------------


def test_priority_classes_are_strict() -> None:
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB))
    hog = engine.submit(job("hog", cpu_min=4))
    background = engine.submit(job("bg", cpu_min=4, priority=PriorityClass.BACKGROUND, group="bg"))
    merge = engine.submit(job("merge", cpu_min=4, priority=PriorityClass.MERGE_LAND, group="m"))
    critical = engine.submit(
        job("crit", cpu_min=4, priority=PriorityClass.CRITICAL_PATH, group="c")
    )
    order = []
    pending = [background, merge, critical]
    for _ in range(3):
        engine.report(hog, OK, USED)
        admitted = [h for h in pending if granted(engine, h) is not None]
        assert len(admitted) == 1
        hog = admitted[0]
        pending.remove(hog)
        order.append(hog.job_id)
    assert order == ["crit", "merge", "bg"]


def test_laggard_first_within_a_gate() -> None:
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB))
    hog = engine.submit(job("hog", cpu_min=4, group="other"))
    short = engine.submit(job("short", cpu_min=4, expected_duration_s=10, group="gate"))
    long = engine.submit(job("long", cpu_min=4, expected_duration_s=300, group="gate"))
    engine.report(hog, OK, USED)
    assert granted(engine, long) is not None, "the longest key of the gate goes first"
    assert granted(engine, short) is None


def test_laggard_first_off_keeps_arrival_order() -> None:
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB), policy=PackerPolicy(laggard_first=False))
    hog = engine.submit(job("hog", cpu_min=4, group="other"))
    short = engine.submit(job("short", cpu_min=4, expected_duration_s=10, group="gate"))
    long = engine.submit(job("long", cpu_min=4, expected_duration_s=300, group="gate"))
    engine.report(hog, OK, USED)
    assert granted(engine, short) is not None
    assert granted(engine, long) is None


def test_an_admitted_gate_finishes_before_a_later_gate_starts() -> None:
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB))
    first = engine.submit(job("g1-a", cpu_min=4, expected_duration_s=50, group="g1"))
    second = engine.submit(job("g1-b", cpu_min=4, expected_duration_s=40, group="g1"))
    later = engine.submit(job("g2-a", cpu_min=4, expected_duration_s=100, group="g2"))
    assert granted(engine, first) is not None
    engine.report(first, OK, USED)
    assert granted(engine, second) is not None, "finish what you started"
    assert granted(engine, later) is None


def test_group_major_with_aging_lets_a_small_gate_bypass_a_large_one() -> None:
    policy = PackerPolicy(group_major=True, aging=0.0)
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB), policy=policy)
    hog = engine.submit(job("hog", cpu_min=4, group="hog"))
    large = engine.submit(job("large", cpu_min=4, expected_duration_s=900, group="large"))
    small = engine.submit(job("small", cpu_min=4, expected_duration_s=10, group="small"))
    engine.report(hog, OK, USED)
    assert granted(engine, small) is not None, "shortest remaining work first"
    assert granted(engine, large) is None


def test_group_major_aging_eventually_admits_the_large_gate() -> None:
    policy = PackerPolicy(group_major=True, aging=4.0)
    engine, clock, _ = _engine(Capacity(4.0, 4 * GIB), policy=policy)
    hog = engine.submit(job("hog", cpu_min=4, expected_duration_s=1000, group="hog"))
    large = engine.submit(job("large", cpu_min=4, expected_duration_s=900, group="large"))
    clock.advance(400)  # the large gate has waited: 900 - 4 * 400 < 0
    small = engine.submit(job("small", cpu_min=4, expected_duration_s=10, group="small"))
    engine.report(hog, OK, USED)
    assert granted(engine, large) is not None, "aging outranks a newer small gate"
    assert granted(engine, small) is None


# ---- EASY backfill ------------------------------------------------------------------------


def _easy_world(policy: PackerPolicy | None = None):
    """A holds 6 of 8 cpu until t=1100; B (8 cpu) is reserved for t=1100."""
    engine, clock, _ = _engine(Capacity(8.0, 32 * GIB), policy=policy)
    a = engine.submit(job("a", cpu_min=6, expected_duration_s=100, group="g0"))
    b = engine.submit(job("b", cpu_min=8, expected_duration_s=100, group="g1"))
    return engine, clock, a, b


def test_easy_refuses_a_backfill_that_would_delay_a_reserved_key() -> None:
    engine, clock, a, b = _easy_world()
    long = engine.submit(job("long", cpu_min=2, expected_duration_s=500, group="g2"))
    assert granted(engine, long) is None, "it fits now but would hold 2 cpu past t=1100"
    assert engine.status().signals["easy_reservations"] >= 1
    clock.advance(100)
    engine.report(a, OK, USED)
    assert granted(engine, b) is not None, "the reserved key starts at its reservation"


def test_easy_admits_a_backfill_that_ends_before_the_reservation() -> None:
    engine, clock, a, b = _easy_world()
    short = engine.submit(job("short", cpu_min=2, expected_duration_s=50, group="g2"))
    assert granted(engine, short) is not None
    clock.advance(50)
    engine.report(short, OK, USED)
    clock.advance(50)
    engine.report(a, OK, USED)
    assert granted(engine, b) is not None


def test_without_easy_a_long_backfill_delays_the_blocked_key() -> None:
    engine, clock, a, b = _easy_world(PackerPolicy(easy=False))
    long = engine.submit(job("long", cpu_min=2, expected_duration_s=500, group="g2"))
    assert granted(engine, long) is not None
    clock.advance(100)
    engine.report(a, OK, USED)
    assert granted(engine, b) is None, "greedy first-fit: the backfill now delays B"


def test_flex_never_delays_a_reserved_key() -> None:
    engine, clock, _ = _engine(Capacity(8.0, 32 * GIB), policy=PackerPolicy(headroom_cpu=0))
    a = engine.submit(job("a", cpu_min=4, expected_duration_s=100, group="g0"))
    b = engine.submit(job("b", cpu_min=6, expected_duration_s=100, group="g1"))
    # B is reserved for t=1100 and leaves 2 cpu spare then. A 200 s job may start now at 1 cpu,
    # and flex may lift it only to 2: 4 free now would hold B back at its reservation.
    short = engine.submit(job("short", cpu_min=1, cpu_max=4, expected_duration_s=200, group="g2"))
    assert engine.wait_grant(short, timeout=0).amounts.cpu == 2
    clock.advance(100)
    engine.report(a, OK, USED)
    assert granted(engine, b) is not None, "the flexed backfill left room for the reservation"


def test_an_overrunning_lease_is_treated_as_ending_now() -> None:
    engine, clock, a, b = _easy_world()
    clock.advance(10_000)  # A is far past its expected end
    long = engine.submit(job("long", cpu_min=2, expected_duration_s=50, group="g2"))
    assert granted(engine, long) is None, "B's reservation is now: nothing may take its cpu"
    engine.report(a, OK, USED)
    assert granted(engine, b) is not None


# ---- flexible grants ----------------------------------------------------------------------


def test_the_laggard_gets_flex_up_to_cpu_max_without_headroom() -> None:
    engine, _, _ = _engine(Capacity(16.0, 32 * GIB))
    lag = engine.submit(job("lag", cpu_min=4, cpu_max=16, expected_duration_s=300))
    assert engine.wait_grant(lag, timeout=0).amounts.cpu == 16


def test_a_non_laggard_keeps_headroom_cpu_back() -> None:
    engine, _, _ = _engine(Capacity(16.0, 32 * GIB))
    lag = engine.submit(job("lag", cpu_min=4, cpu_max=4, expected_duration_s=300))
    other = engine.submit(job("other", cpu_min=2, cpu_max=12, expected_duration_s=30))
    assert engine.wait_grant(lag, timeout=0).amounts.cpu == 4
    # 12 cpu free after lag; other takes 2 + flex, keeping the default 8 back: 2 + (10 - 8) = 4.
    assert engine.wait_grant(other, timeout=0).amounts.cpu == 4


def test_laggard_flex_off_applies_headroom_to_the_laggard_too() -> None:
    engine, _, _ = _engine(Capacity(16.0, 32 * GIB), policy=PackerPolicy(laggard_flex=False))
    lag = engine.submit(job("lag", cpu_min=4, cpu_max=16, expected_duration_s=300))
    assert engine.wait_grant(lag, timeout=0).amounts.cpu == 8  # 4 + (12 - 8)


def test_headroom_is_configurable() -> None:
    engine, _, _ = _engine(Capacity(16.0, 32 * GIB), policy=PackerPolicy(headroom_cpu=2))
    lag = engine.submit(job("lag", cpu_min=4, cpu_max=4, expected_duration_s=300))
    other = engine.submit(job("other", cpu_min=2, cpu_max=12, expected_duration_s=30))
    engine.wait_grant(lag, timeout=0)
    assert engine.wait_grant(other, timeout=0).amounts.cpu == 10  # 2 + (10 - 2)


def test_amber_admits_critical_only_at_cpu_min_and_red_admits_nothing() -> None:
    engine, _, _ = _engine()
    engine.set_brake(BrakeLevel.RED, "memory")
    plain = engine.submit(job("plain"))
    assert granted(engine, plain) is None
    engine.set_brake(BrakeLevel.AMBER, "memory")
    assert granted(engine, plain) is None
    critical = engine.submit(
        job("crit", cpu_min=2, cpu_max=8, priority=PriorityClass.CRITICAL_PATH, group="c")
    )
    assert engine.wait_grant(critical, timeout=0).amounts.cpu == 2
    engine.set_brake(BrakeLevel.GREEN, "clear")
    assert granted(engine, plain) is not None


# ---- nested -------------------------------------------------------------------------------


def test_nested_requests_are_never_braked() -> None:
    engine, _, _ = _engine()
    parent = engine.wait_grant(engine.submit(job("parent", cpu_min=4, cpu_max=4)), timeout=0)
    engine.set_brake(BrakeLevel.RED, "memory")
    child = engine.submit(job("child", cpu_min=2, parent_lease=parent.lease_id))
    assert granted(engine, child) is not None


def test_a_nested_request_beyond_the_parent_grant_is_refused() -> None:
    engine, _, _ = _engine()
    parent = engine.wait_grant(
        engine.submit(job("parent", cpu_min=2, cpu_max=2, pools={"dolt-servers": 1})), timeout=0
    )
    for spec in (
        job("cpu", cpu_min=3, parent_lease=parent.lease_id),
        job("pool", pools={"dolt-servers": 2}, parent_lease=parent.lease_id),
        job("mem", memory_bytes=1, parent_lease=parent.lease_id),
    ):
        with pytest.raises(GrantRefused):
            engine.submit(spec)


def test_a_nested_grant_is_flexed_from_the_parent_only() -> None:
    engine, _, _ = _engine()
    parent = engine.wait_grant(engine.submit(job("parent", cpu_min=6, cpu_max=6)), timeout=0)
    child = engine.submit(job("child", cpu_min=1, cpu_max=16, parent_lease=parent.lease_id))
    assert engine.wait_grant(child, timeout=0).amounts.cpu == 6


# ---- killed client ------------------------------------------------------------------------


def test_a_dead_owner_is_reclaimed_on_the_next_admission_pass() -> None:
    dead: set[Owner] = set()
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB), dead=dead)
    victim = Owner(pid=7001, start_token="x")
    held = engine.submit(job("held", owner=victim, cpu_min=4))
    waiting = engine.submit(job("waiting", cpu_min=4))
    assert granted(engine, waiting) is None
    dead.add(victim)
    assert granted(engine, waiting) is not None, "wait_grant itself notices the dead owner"
    with pytest.raises(LeaseLost):
        engine.wait_grant(held, timeout=0)


# ---- blocking wait ------------------------------------------------------------------------


def test_a_blocking_wait_is_woken_by_a_release() -> None:
    engine, _, _ = _engine(Capacity(4.0, 4 * GIB))
    holder = engine.submit(job("holder", cpu_min=4))
    waiting = engine.submit(job("waiting", cpu_min=4))
    result: list[object] = []
    started = threading.Event()

    def wait() -> None:
        started.set()
        result.append(engine.wait_grant(waiting, timeout=None))

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    assert started.wait(GENEROUS)
    engine.report(holder, OK, USED)
    thread.join(GENEROUS)
    assert not thread.is_alive()
    assert getattr(result[0], "job_id", None) == "waiting"


# ---- knobs --------------------------------------------------------------------------------


def test_policy_and_engine_knobs_are_validated() -> None:
    with pytest.raises(ValueError):
        PackerPolicy(headroom_cpu=-1)
    with pytest.raises(ValueError):
        PackerPolicy(aging=-1)
    with pytest.raises(ValueError):
        LocalEngine(Capacity(1, 1), ManualClock(), repoll_seconds=0)
    assert PackerPolicy() == PackerPolicy(
        laggard_first=True,
        laggard_flex=True,
        easy=True,
        headroom_cpu=8.0,
        group_major=False,
        aging=4.0,
    ), "the ADR's packer=group defaults"


def test_no_availability_port_admits_against_the_budget_alone() -> None:
    engine = LocalEngine(Capacity(2.0, GIB), ManualClock())
    first = engine.submit(job("a", cpu_min=2))
    second = engine.submit(job("b", cpu_min=1))
    assert granted(engine, first) is not None
    assert granted(engine, second) is None
    assert _running(engine) == {"a"}
