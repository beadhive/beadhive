"""Execution roots in the local engine (bh-mf1o4): one bounded root per key run.

Every test owns time (``ManualClock``), host availability (``FakeAvailability``) and filesystem
space (``FakeDiskSpace``); nothing sleeps and nothing reads the real host's disks.
"""

from __future__ import annotations

import pytest

from beadhive_validation_broker import (
    LEASE_ENV,
    BrakeLevel,
    Capacity,
    EventKind,
    ExecutionRoot,
    FakeAvailability,
    FakeDiskSpace,
    GrantRefused,
    LocalEngine,
    Outcome,
    Owner,
    RootKind,
    Usage,
    validate_roots,
)
from beadhive_validation_broker.contract import (
    ManualClock,
    granted,
    job,
    local_harness,
    parametrize_scenarios,
    run_scenario,
)

GIB = 1024**3
OK, USED = Outcome(0), Usage(wall_s=1.0)
BUDGET = Capacity(16.0, 32 * GIB, {"dolt-servers": 4})
DISK = ExecutionRoot("/srv/bh-runs", RootKind.DISK, 10 * GIB)
SPARE = ExecutionRoot("/var/bh-runs", RootKind.DISK, 40 * GIB)
RAM = ExecutionRoot("/dev/shm/bh-runs", RootKind.TMPFS, 8 * GIB)


def _engine(
    roots: tuple[ExecutionRoot, ...],
    *,
    budget: Capacity = BUDGET,
    free: dict[str, int] | None = None,
    dead: set[Owner] | None = None,
) -> tuple[LocalEngine, FakeDiskSpace]:
    disk = FakeDiskSpace(free if free is not None else {r.path: 1024 * GIB for r in roots})
    dead_owners = dead if dead is not None else set()
    engine = LocalEngine(
        budget,
        ManualClock(1000.0),
        availability=FakeAvailability(Capacity(256.0, 256 * GIB)),
        owner_alive=lambda owner: owner not in dead_owners,
        roots=roots,
        disk_space=disk if roots else None,
    )
    return engine, disk


def _root(engine: LocalEngine, handle) -> str | None:
    grant = granted(engine, handle)
    assert grant is not None, f"{handle.job_id} was not admitted"
    return grant.root


# ---- unset roots: today's behaviour ---------------------------------------------------------


def test_without_roots_disk_is_ignored_and_no_root_is_granted() -> None:
    engine, _ = _engine(())
    handle = engine.submit(job("a", memory_bytes=GIB, disk_bytes=500 * GIB))
    grant = granted(engine, handle)
    assert grant is not None
    assert grant.root is None
    assert grant.amounts == Capacity(1.0, GIB, {})
    assert set(grant.env()) == {
        LEASE_ENV,
        "BH_GRANT_CPUS",
        "BH_GRANT_MEMORY_BYTES",
        "BH_GRANT_MEMORY_MB",
    }
    assert engine.status().budget == BUDGET


# ---- one root per run -----------------------------------------------------------------------


def test_each_run_is_placed_on_exactly_one_root_in_preference_order() -> None:
    engine, _ = _engine((DISK, SPARE))
    handle = engine.submit(job("a", disk_bytes=4 * GIB))
    grant = granted(engine, handle)
    assert grant is not None
    assert grant.root == DISK.path
    assert grant.amounts.pools == {DISK.pool: 4 * GIB}, "disk drawn from one root only"
    assert grant.env()["BH_GRANT_ROOT"] == DISK.path
    assert grant.env()["BH_GRANT_DISK_BYTES"] == str(4 * GIB)
    assert not any(name.startswith("BH_GRANT_ROOT:") for name in grant.env())
    admitted = [e for e in engine.events() if e.kind is EventKind.ADMITTED]
    assert admitted[-1].detail["root"] == DISK.path


def test_a_root_without_the_disk_reservation_free_under_its_bound_is_refused() -> None:
    engine, _ = _engine((DISK, SPARE))
    first = engine.submit(job("a", disk_bytes=6 * GIB, group="g1"))
    second = engine.submit(job("b", disk_bytes=6 * GIB, group="g2"))
    assert _root(engine, first) == DISK.path
    assert _root(engine, second) == SPARE.path, "DISK has only 4 GiB left of its 10 GiB bound"
    assert engine.status().in_use.pools == {DISK.pool: 6 * GIB, SPARE.pool: 6 * GIB}


def test_a_single_full_root_makes_the_run_wait_then_admits_on_release() -> None:
    engine, _ = _engine((DISK,))
    first = engine.submit(job("a", disk_bytes=6 * GIB, group="g1"))
    second = engine.submit(job("b", disk_bytes=6 * GIB, group="g2"))
    assert granted(engine, first) is not None
    assert granted(engine, second) is None
    assert engine.status().queued == 1, "waiting for space is not a refusal"
    engine.report(first, OK, USED)
    assert _root(engine, second) == DISK.path


def test_measured_free_space_clamps_the_bound() -> None:
    engine, disk = _engine((DISK,), free={DISK.path: 3 * GIB})
    handle = engine.submit(job("a", disk_bytes=4 * GIB))
    assert granted(engine, handle) is None, "the filesystem has 3 GiB free: 4 GiB must wait"
    assert engine.status().signals["root_free_bytes:" + DISK.path] == 3 * GIB
    disk.set(DISK.path, 50 * GIB)
    engine.reap()
    assert _root(engine, handle) == DISK.path


def test_an_unreadable_root_counts_as_full_never_as_free() -> None:
    engine, disk = _engine((DISK, SPARE))
    disk.fail(DISK.path)
    handle = engine.submit(job("a", disk_bytes=GIB))
    assert _root(engine, handle) == SPARE.path
    assert engine.status().signals["root_free_bytes:" + DISK.path] == 0


def test_a_reservation_beyond_every_roots_bound_is_refused_not_queued() -> None:
    engine, _ = _engine((DISK, RAM))
    with pytest.raises(GrantRefused):
        engine.submit(job("huge", disk_bytes=11 * GIB))
    assert engine.status().queued == 0


# ---- tmpfs roots count against memory -------------------------------------------------------


def test_a_tmpfs_roots_usage_also_counts_against_memory() -> None:
    engine, _ = _engine((RAM,), budget=Capacity(16.0, 10 * GIB, {}))
    handle = engine.submit(job("a", memory_bytes=3 * GIB, disk_bytes=4 * GIB))
    grant = granted(engine, handle)
    assert grant is not None and grant.root == RAM.path
    assert grant.amounts.memory_bytes == 7 * GIB, "4 GiB of tmpfs scratch is RAM"
    assert grant.amounts.pools == {RAM.pool: 4 * GIB}
    second = engine.submit(job("b", memory_bytes=3 * GIB, disk_bytes=GIB, group="g2"))
    assert granted(engine, second) is None, "7 + 4 GiB exceeds the 10 GiB memory budget"


def test_a_tmpfs_root_too_big_for_memory_is_refused_at_submit() -> None:
    engine, _ = _engine((RAM,), budget=Capacity(16.0, 4 * GIB, {}))
    with pytest.raises(GrantRefused):
        engine.submit(job("a", memory_bytes=2 * GIB, disk_bytes=3 * GIB))


def test_a_disk_root_is_preferred_and_saves_the_memory_a_tmpfs_root_would_cost() -> None:
    engine, _ = _engine((DISK, RAM), budget=Capacity(16.0, 10 * GIB, {}))
    handle = engine.submit(job("a", memory_bytes=3 * GIB, disk_bytes=4 * GIB))
    grant = granted(engine, handle)
    assert grant is not None and grant.root == DISK.path
    assert grant.amounts.memory_bytes == 3 * GIB


# ---- a gate shares one root ------------------------------------------------------------------


def test_a_gates_keys_are_pinned_to_one_root_never_split() -> None:
    engine, _ = _engine((DISK, SPARE))
    first = engine.submit(job("a", disk_bytes=6 * GIB, group="gate"))
    second = engine.submit(job("b", disk_bytes=6 * GIB, group="gate"))
    other = engine.submit(job("c", disk_bytes=6 * GIB, group="other"))
    assert _root(engine, first) == DISK.path
    assert granted(engine, second) is None, "the gate's root is full: wait, never split the gate"
    assert _root(engine, other) == SPARE.path
    engine.report(first, OK, USED)
    assert _root(engine, second) == DISK.path


def test_a_gate_is_pinned_only_to_a_root_that_fits_all_its_queued_keys() -> None:
    engine, _ = _engine((DISK, SPARE))
    engine.set_brake(BrakeLevel.RED, "hold admission while the gate is queued")
    small = engine.submit(job("small", disk_bytes=GIB, group="gate"))
    big = engine.submit(job("big", disk_bytes=20 * GIB, group="gate"))
    engine.set_brake(BrakeLevel.GREEN, "release")
    assert _root(engine, small) == SPARE.path
    assert _root(engine, big) == SPARE.path


def test_a_key_that_cannot_fit_its_gates_pinned_root_is_refused() -> None:
    engine, _ = _engine((DISK, SPARE))
    first = engine.submit(job("a", disk_bytes=GIB, group="gate"))
    assert _root(engine, first) == DISK.path
    with pytest.raises(GrantRefused):
        engine.submit(job("b", disk_bytes=20 * GIB, group="gate"))


def test_the_pin_is_released_once_the_gate_drains() -> None:
    engine, _ = _engine((DISK, SPARE))
    first = engine.submit(job("a", disk_bytes=GIB, group="gate"))
    assert _root(engine, first) == DISK.path
    engine.report(first, OK, USED)
    again = engine.submit(job("b", disk_bytes=20 * GIB, group="gate"))
    assert _root(engine, again) == SPARE.path


# ---- nested and killed clients ---------------------------------------------------------------


def test_a_nested_job_runs_in_its_parents_root_and_carves_its_disk() -> None:
    engine, _ = _engine((DISK,))
    parent = engine.submit(job("parent", cpu_min=4, disk_bytes=4 * GIB))
    lease = granted(engine, parent)
    assert lease is not None
    child = engine.submit(job("child", disk_bytes=3 * GIB, parent_lease=lease.lease_id))
    grant = granted(engine, child)
    assert grant is not None and grant.root == DISK.path
    assert engine.status().in_use.pools == {DISK.pool: 4 * GIB}, "nested never re-enters the host"
    with pytest.raises(GrantRefused):
        engine.submit(job("greedy", disk_bytes=5 * GIB, parent_lease=lease.lease_id))


def test_a_killed_clients_root_reservation_is_reclaimed() -> None:
    dead: set[Owner] = set()
    engine, _ = _engine((DISK,), dead=dead)
    victim = Owner(pid=4242)
    first = engine.submit(job("a", disk_bytes=8 * GIB, owner=victim, group="g1"))
    waiting = engine.submit(job("b", disk_bytes=8 * GIB, group="g2"))
    assert granted(engine, first) is not None
    assert granted(engine, waiting) is None
    dead.add(victim)
    engine.reap()
    assert _root(engine, waiting) == DISK.path


# ---- configuration guards --------------------------------------------------------------------


def test_roots_must_be_absolute_bounded_distinct_and_not_nested() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ExecutionRoot("relative/runs", RootKind.DISK, GIB)
    with pytest.raises(ValueError, match="max_bytes"):
        ExecutionRoot("/srv/runs", RootKind.DISK, 0)
    with pytest.raises(ValueError, match="nested"):
        validate_roots([DISK, ExecutionRoot(DISK.path + "/inner", RootKind.DISK, GIB)])
    with pytest.raises(ValueError, match="nested"):
        validate_roots([DISK, ExecutionRoot(DISK.path + "/", RootKind.TMPFS, GIB)])
    assert validate_roots([DISK, ExecutionRoot("/srv/bh-runs-2", RootKind.DISK, GIB)])


def test_the_root_pool_prefix_is_reserved_and_roots_need_a_space_probe() -> None:
    with pytest.raises(ValueError, match="reserved"):
        LocalEngine(Capacity(1.0, GIB, {"root:/x": 1}), ManualClock())
    with pytest.raises(ValueError, match="disk_space"):
        LocalEngine(BUDGET, ManualClock(), roots=(DISK,))


# ---- the safety contract still holds with roots ----------------------------------------------


@parametrize_scenarios()
def test_local_engine_with_roots_passes_the_scenario(scenario) -> None:
    run_scenario(scenario, local_harness(roots=(DISK, RAM)))
