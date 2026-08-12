"""bh-wa3ch — the ceiling on how many tests may hold a real dolt sql-server at once.

`-n auto` is 24 workers for 54 integration tests on the box this was measured on, and before this
nothing bounded how many real servers they stood up simultaneously: no lock, no xdist group, no
constant. These tests cover the slot itself — that it actually excludes, that it releases on every
path including an exception, and that the documented `BH_DOLT_SLOTS=0` arm really is unbounded
(the A of the A/B the bead's acceptance asks to measure).
"""

from __future__ import annotations

import threading
import time

import pytest

from harness.world import MAX_CONCURRENT_DOLT_SERVER_TESTS, dolt_server_slot


def _hold(slots: int, holders: int, hold_for: float = 0.3):
    """Run *holders* threads that each take a slot and sleep. Returns the peak concurrency seen."""
    peak = 0
    live = 0
    lock = threading.Lock()

    def _worker():
        nonlocal peak, live
        with dolt_server_slot(slots):
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(hold_for)
            with lock:
                live -= 1

    threads = [threading.Thread(target=_worker) for _ in range(holders)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return peak


def test_the_slot_actually_excludes():
    """Six would-be server tests, two slots: never more than two at once. The property the
    integration suite gets for free by marking a file `dolt_server`."""
    assert _hold(slots=2, holders=6) <= 2


def test_a_released_slot_is_reusable():
    """Serial reuse, not just exclusion — a bound that leaked slots would deadlock the suite after
    the first N server tests rather than merely slowing it down."""
    for _ in range(3):
        with dolt_server_slot(1):
            pass
    assert _hold(slots=1, holders=2) == 1


def test_the_slot_is_released_when_the_test_raises():
    """The failure path is the one that matters: an integration test that dies mid-server must not
    take a permit with it."""
    with pytest.raises(RuntimeError):
        with dolt_server_slot(1):
            raise RuntimeError("the test blew up")

    assert _hold(slots=1, holders=1) == 1


def test_zero_slots_is_the_unbounded_arm():
    """`BH_DOLT_SLOTS=0` takes no lock at all — how the unbounded half of the bead's measurement
    was run (peak 9 run-owned servers unbounded vs 6 at the default of 4)."""
    assert _hold(slots=0, holders=4) > 1


def test_the_default_bound_is_a_named_constant():
    """Acceptance: 'the bound is a named constant … discoverable by grep', not a literal buried in
    a fixture."""
    assert MAX_CONCURRENT_DOLT_SERVER_TESTS >= 1
