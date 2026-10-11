"""FakeClock: the shared deterministic clock (bh-j4v0l)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from beadhive.hq_sql_session import SessionRenewer
from beadhive.modules.state.contracts.ports import StateClock
from harness.clock import FakeClock


def test_satisfies_state_clock_seam():
    clock: StateClock = FakeClock()
    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)


def test_advance_moves_wall_and_monotonic_together():
    clock = FakeClock()
    wall, mono = clock.wall(), clock.monotonic()
    clock.advance(30)
    clock.advance(timedelta(seconds=0.5))
    assert clock.now() == datetime(2026, 1, 1, 0, 0, 30, 500000, tzinfo=UTC)
    assert clock.wall() == wall + 30.5
    assert clock.monotonic() == mono + 30.5


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
def test_monotonic_never_decreases(bad):
    clock = FakeClock()
    before = clock.monotonic()
    with pytest.raises(ValueError):
        clock.advance(bad)
    assert clock.monotonic() == before


def test_requires_aware_start():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))


def test_sleep_advances_without_blocking():
    clock = FakeClock()
    real = time.monotonic()
    clock.sleep(3600)
    assert time.monotonic() - real < 5
    assert clock.monotonic() == 1000.0 + 3600
    assert clock.sleeps == [3600.0]


def test_stop_callable_drives_session_renewer_run():
    clock = FakeClock()
    ticks: list[float] = []
    renewer = SessionRenewer(lambda: None)
    renewer.tick = lambda: ticks.append(clock.monotonic())  # type: ignore[method-assign]
    renewer.run(interval_s=10, stop=clock.stop_after(3))
    assert len(ticks) == 3
    assert clock.monotonic() >= 1000.0 + 10
