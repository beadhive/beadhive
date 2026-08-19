"""`fleet.fanout` — shape B: concurrency + input order, capped (bh-1qxjn)."""

from __future__ import annotations

import threading

import pytest

from beadhive import fleet


def test_fanout_preserves_input_order_regardless_of_completion_order():
    """The contract that makes per-hive reports diffable: rows never reshuffle run to run."""
    import time

    def slow_first(n):
        time.sleep(0.05 if n == 0 else 0)
        return n * 10

    assert fleet.fanout(slow_first, [0, 1, 2, 3]) == [0, 10, 20, 30]


def test_fanout_actually_runs_concurrently():
    """A barrier that only clears if >1 worker is live — deadlocks (and fails) if serial."""
    barrier = threading.Barrier(4, timeout=5)

    def wait(_):
        barrier.wait()
        return True

    assert fleet.fanout(wait, list(range(4)), workers=4) == [True] * 4


def test_fanout_caps_workers_below_item_count():
    """One thread per item is the shape this replaced — the cap is the point."""
    live = 0
    peak = 0
    lock = threading.Lock()

    def track(_):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        try:
            return peak
        finally:
            with lock:
                live -= 1

    fleet.fanout(track, list(range(50)), workers=3)
    assert peak <= 3


def test_fanout_on_empty_input_returns_empty_and_builds_no_pool():
    """ThreadPoolExecutor(max_workers=0) raises — an empty fleet must not be a crash."""
    assert fleet.fanout(lambda x: x, []) == []


def test_fanout_propagates_an_exception_from_an_item():
    def boom(n):
        if n == 2:
            raise ValueError("hive 2")
        return n

    with pytest.raises(ValueError, match="hive 2"):
        fleet.fanout(boom, [0, 1, 2, 3])


def test_fanout_accepts_a_lazy_iterable():
    assert fleet.fanout(lambda n: n + 1, (n for n in range(3))) == [1, 2, 3]
