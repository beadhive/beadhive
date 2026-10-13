"""The in-memory fake behaves as the port documents (validation-broker-adr)."""

from __future__ import annotations

import pytest

from beadhive_validation_broker import (
    LEASE_ENV,
    BrakeLevel,
    BrokerPort,
    BrokerUnavailable,
    Capacity,
    EventKind,
    GrantRefused,
    GrantTimeout,
    InMemoryBroker,
    LeaseLost,
    Outcome,
    PriorityClass,
    Reservation,
    Usage,
)
from beadhive_validation_broker.contract import ManualClock, granted, job

GIB = 1024**3
OK = Outcome(0)
USED = Usage(wall_s=2.0, cpu_s=3.0, peak_memory_bytes=GIB)


def _broker(**kwargs) -> tuple[InMemoryBroker, ManualClock]:
    clock = ManualClock(100.0)
    return InMemoryBroker(Capacity(8.0, 8 * GIB, {"dolt-servers": 2}), clock, **kwargs), clock


def test_fake_satisfies_the_port() -> None:
    broker, _ = _broker()
    assert isinstance(broker, BrokerPort)
    assert broker.name == "memory"


def test_grant_is_capped_at_cpu_max_and_stamped_from_the_injected_clock() -> None:
    broker, clock = _broker()
    clock.advance(5)
    handle = broker.submit(job("a", cpu_min=2, cpu_max=4, memory_bytes=GIB))
    grant = broker.wait_grant(handle)
    assert grant.amounts.cpu == 4
    assert grant.granted_at == 105.0
    assert broker.status().in_use == Capacity(4, GIB)


def test_grant_shrinks_to_what_is_free_but_never_below_cpu_min() -> None:
    broker, _ = _broker()
    first = broker.submit(job("a", cpu_min=2, cpu_max=6))
    second = broker.submit(job("b", cpu_min=4, cpu_max=8))
    assert broker.wait_grant(first).amounts.cpu == 6
    assert granted(broker, second) is None  # only 2 cpu free, needs 4
    broker.report(first, OK, USED)
    assert broker.wait_grant(second).amounts.cpu == 8


def test_grant_env_exports_the_lease_and_the_amounts() -> None:
    broker, _ = _broker()
    handle = broker.submit(
        job("a", cpu_min=2, cpu_max=3, memory_bytes=3 * GIB // 2, pools={"dolt-servers": 2})
    )
    env = broker.wait_grant(handle).env()
    assert env[LEASE_ENV].startswith("lease-")
    assert env["BH_GRANT_CPUS"] == "3"
    assert env["BH_GRANT_MEMORY_BYTES"] == str(3 * GIB // 2)
    assert env["BH_GRANT_MEMORY_MB"] == "1536"
    assert env["BH_GRANT_DOLT_SERVERS"] == "2"


def test_wait_grant_on_a_queued_job_times_out_and_the_job_stays_queued() -> None:
    broker, _ = _broker()
    broker.submit(job("hog", cpu_min=8))
    waiting = broker.submit(job("late", cpu_min=1))
    with pytest.raises(GrantTimeout):
        broker.wait_grant(waiting, timeout=1.0)
    assert broker.status().queued == 1


def test_priority_class_orders_admission() -> None:
    broker, _ = _broker()
    hog = broker.submit(job("hog", cpu_min=8))
    background = broker.submit(job("bg", cpu_min=8, priority=PriorityClass.BACKGROUND))
    critical = broker.submit(job("crit", cpu_min=8, priority=PriorityClass.CRITICAL_PATH))
    broker.report(hog, OK, USED)
    assert granted(broker, critical) is not None
    assert granted(broker, background) is None


def test_a_smaller_job_backfills_past_one_that_does_not_fit() -> None:
    broker, _ = _broker()
    broker.submit(job("hog", cpu_min=6))
    big = broker.submit(job("big", cpu_min=4))
    small = broker.submit(job("small", cpu_min=2))
    assert granted(broker, big) is None
    assert granted(broker, small) is not None


def test_submit_is_idempotent_per_job_id() -> None:
    broker, _ = _broker()
    spec = job("a")
    assert broker.submit(spec) == broker.submit(spec)
    assert broker.status().running == 1


def test_report_records_usage_and_releases_the_lease() -> None:
    broker, _ = _broker()
    handle = broker.submit(job("a", cpu_min=8))
    broker.report(handle, Outcome(3), USED)
    assert [(r.job_id, r.outcome.exit_code, r.usage) for r in broker.reports] == [("a", 3, USED)]
    assert broker.status().in_use == Capacity(0, 0)
    kinds = [e.kind for e in broker.events()]
    assert kinds == [EventKind.SUBMITTED, EventKind.ADMITTED, EventKind.FINISHED]


def test_events_resume_from_a_sequence_number() -> None:
    broker, _ = _broker()
    handle = broker.submit(job("a"))
    seen = list(broker.events())
    broker.report(handle, OK, USED)
    later = list(broker.events(since=seen[-1].seq))
    assert [e.kind for e in later] == [EventKind.FINISHED]
    assert [e.seq for e in seen + later] == list(range(1, len(seen) + len(later) + 1))


def test_reservation_validation_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        Reservation(cpu_min=4, cpu_max=2, memory_bytes=0)
    with pytest.raises(ValueError):
        Reservation(cpu_min=1, cpu_max=1, memory_bytes=-1)
    with pytest.raises(ValueError):
        Reservation(cpu_min=1, cpu_max=1, memory_bytes=0, pools={"p": -1})


def test_wait_grant_on_an_unknown_job_is_lease_lost() -> None:
    broker, _ = _broker()
    other, _ = _broker()
    handle = other.submit(job("elsewhere"))
    with pytest.raises(LeaseLost):
        broker.wait_grant(handle)


def test_red_brake_stops_admission_and_amber_admits_critical_at_cpu_min() -> None:
    broker, _ = _broker()
    broker.set_brake(BrakeLevel.RED, "memory")
    held = broker.submit(job("a"))
    assert granted(broker, held) is None
    broker.set_brake(BrakeLevel.AMBER, "memory")
    assert granted(broker, held) is None  # not critical
    critical = broker.submit(job("c", cpu_min=2, cpu_max=6, priority=PriorityClass.CRITICAL_PATH))
    assert broker.wait_grant(critical).amounts.cpu == 2
    broker.set_brake(BrakeLevel.GREEN, "clear")
    assert granted(broker, held) is not None
    assert broker.status().brake is BrakeLevel.GREEN


def test_down_fake_raises_broker_unavailable_then_recovers_with_state_intact() -> None:
    broker, _ = _broker()
    handle = broker.submit(job("a"))
    broker.set_available(False)
    with pytest.raises(BrokerUnavailable):
        broker.wait_grant(handle)
    broker.set_available(True)
    assert broker.wait_grant(handle).job_id == "a"


def test_oversized_reservation_is_refused_not_queued() -> None:
    broker, _ = _broker()
    with pytest.raises(GrantRefused):
        broker.submit(job("huge", cpu_min=9))
    assert broker.status().queued == 0
