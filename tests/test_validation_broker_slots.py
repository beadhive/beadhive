"""SlotsEngine reproduces today's host admission behind the validation broker port (bh-uljpc).

The engine wraps :func:`beadhive.validation_admission.host_slot`, so these tests drive the real
file-lock semaphore in a temp root. Nothing sleeps: every wait is an event the engine sets, and
"a third request waits" is proved by the semaphore itself having no free permit.
"""

from __future__ import annotations

import pytest

from beadhive import validation_admission
from beadhive.validation_broker_slots import SlotsEngine
from beadhive_validation_broker import (
    LEASE_ENV,
    BrokerPort,
    EventKind,
    GrantRefused,
    GrantTimeout,
    LeaseLost,
    Outcome,
    Usage,
)
from beadhive_validation_broker.contract import job

OK, USED = Outcome(0), Usage(wall_s=1.0)
GENEROUS = 10.0  # an upper bound on an event wait, never a delay the test depends on


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    monkeypatch.setenv("BH_VALIDATION_MEMORY_FLOOR", "0")
    return SlotsEngine({"work": {"validation_slots": 2}}, root=tmp_path)


def _permit_is_free(tmp_path) -> bool:
    """True when a slot can be taken right now, using the same non-blocking flock probe."""
    import fcntl

    for index in range(2):
        with (tmp_path / f"slot-{index}").open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return True
    return False


def test_engine_satisfies_the_port(engine) -> None:
    assert isinstance(engine, BrokerPort)
    assert engine.name == "slots"


def test_two_concurrent_requests_are_admitted_and_a_third_waits(engine, tmp_path) -> None:
    first = engine.submit(job("a"))
    second = engine.submit(job("b"))
    assert engine.wait_grant(first, timeout=GENEROUS).job_id == "a"
    assert engine.wait_grant(second, timeout=GENEROUS).job_id == "b"
    assert not _permit_is_free(tmp_path), "validation_slots=2: both permits are held"

    third = engine.submit(job("c"))
    with pytest.raises(GrantTimeout):
        engine.wait_grant(third, timeout=0.0)
    status = engine.status()
    assert (status.running, status.queued) == (2, 1)
    assert status.budget.cpu == 2

    engine.report(first, OK, USED)
    assert engine.wait_grant(third, timeout=GENEROUS).job_id == "c"
    engine.report(second, OK, USED)
    engine.report(third, OK, USED)
    assert engine.status().running == 0


def test_report_releases_the_kernel_permit(engine, tmp_path) -> None:
    handle = engine.submit(job("a"))
    engine.wait_grant(handle, timeout=GENEROUS)
    engine.report(handle, OK, USED)
    engine.report(handle, OK, USED)  # idempotent
    # The holder thread leaves host_slot once released; a fresh engine can then take both permits.
    held = engine._jobs["a"]
    assert held.ended.wait(GENEROUS)
    assert _permit_is_free(tmp_path)


def test_a_cancelled_queued_job_never_holds_a_permit(engine, tmp_path) -> None:
    a, b = engine.submit(job("a")), engine.submit(job("b"))
    engine.wait_grant(a, timeout=GENEROUS)
    engine.wait_grant(b, timeout=GENEROUS)
    queued = engine.submit(job("c"))
    engine.cancel(queued, "not needed")
    engine.cancel(queued, "not needed")
    with pytest.raises(LeaseLost):
        engine.wait_grant(queued, timeout=GENEROUS)
    engine.report(a, OK, USED)
    assert engine._jobs["c"].ended.wait(GENEROUS), "the cancelled job released its permit at once"
    engine.report(b, OK, USED)
    assert [e.kind for e in engine.events() if e.job_id == "c"] == [
        EventKind.SUBMITTED,
        EventKind.CANCELLED,
    ]


def test_zero_slots_disables_admission_exactly_as_today(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    monkeypatch.setenv("BH_VALIDATION_MEMORY_FLOOR", "0")
    engine = SlotsEngine({"work": {"validation_slots": 0}}, root=tmp_path)
    handles = [engine.submit(job(f"j{i}")) for i in range(5)]
    for handle in handles:
        assert engine.wait_grant(handle, timeout=GENEROUS).job_id == handle.job_id
    assert engine.status().budget.cpu == 0
    for handle in handles:
        engine.report(handle, OK, USED)


def test_environment_override_wins_like_host_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BH_VALIDATION_SLOTS", "1")
    monkeypatch.setenv("BH_VALIDATION_MEMORY_FLOOR", "0")
    engine = SlotsEngine({"work": {"validation_slots": 5}}, root=tmp_path)
    assert engine.status().budget.cpu == validation_admission.configured_slots({}) == 1
    first, second = engine.submit(job("a")), engine.submit(job("b"))
    engine.wait_grant(first, timeout=GENEROUS)
    with pytest.raises(GrantTimeout):
        engine.wait_grant(second, timeout=0.0)
    engine.report(first, OK, USED)
    engine.wait_grant(second, timeout=GENEROUS)
    engine.report(second, OK, USED)


def test_nested_request_is_served_from_the_parent_without_a_second_permit(engine, tmp_path) -> None:
    parent_handle = engine.submit(job("parent", cpu_min=2, cpu_max=2))
    parent = engine.wait_grant(parent_handle, timeout=GENEROUS)
    other = engine.submit(job("other"))
    engine.wait_grant(other, timeout=GENEROUS)  # the second permit
    assert not _permit_is_free(tmp_path)

    child_handle = engine.submit(job("child", cpu_min=1, parent_lease=parent.lease_id))
    child = engine.wait_grant(child_handle, timeout=0.0)
    assert child.parent_lease == parent.lease_id
    assert child.env()[LEASE_ENV] == child.lease_id
    assert engine.status().running == 3 and engine.status().in_use.cpu == 2

    with pytest.raises(GrantRefused):
        engine.submit(job("greedy", cpu_min=3, parent_lease=parent.lease_id))
    with pytest.raises(LeaseLost):
        engine.submit(job("orphan", parent_lease="slots-lease-missing"))

    engine.report(parent_handle, OK, USED)
    with pytest.raises(LeaseLost):
        engine.wait_grant(child_handle, timeout=0.0)
    engine.report(other, OK, USED)


def test_a_nested_request_waits_inside_the_parents_grant_for_a_sibling(engine) -> None:
    parent_handle = engine.submit(job("parent", cpu_min=2, cpu_max=2))
    parent = engine.wait_grant(parent_handle, timeout=GENEROUS)
    first = engine.submit(job("n1", cpu_min=2, parent_lease=parent.lease_id))
    second = engine.submit(job("n2", cpu_min=2, parent_lease=parent.lease_id))
    engine.wait_grant(first, timeout=0.0)
    with pytest.raises(GrantTimeout):
        engine.wait_grant(second, timeout=0.0)
    engine.report(first, OK, USED)
    engine.wait_grant(second, timeout=0.0)
    engine.report(parent_handle, OK, USED)


def test_a_memory_floor_refusal_propagates_unchanged(tmp_path, monkeypatch) -> None:
    import typer

    def refuse(_cfg):
        raise typer.Exit(75)

    monkeypatch.setattr(validation_admission, "_admit_memory", refuse)
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    engine = SlotsEngine({"work": {"validation_slots": 1}}, root=tmp_path)
    handle = engine.submit(job("a"))
    with pytest.raises(typer.Exit) as excinfo:
        engine.wait_grant(handle, timeout=GENEROUS)
    assert excinfo.value.exit_code == 75
