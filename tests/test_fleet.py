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


# ---- shape A: the bulk cross-hive transport (bh-0gvs3) ----------------------


class _Res:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_sql_rows_parses_a_result_body():
    assert fleet.sql_rows(_Res(0, '[{"db": "bh", "max_version": 62}]')) == [
        {"db": "bh", "max_version": 62}
    ]


def test_sql_rows_is_none_on_a_failed_call_not_empty():
    """None means FALL BACK to shape B. Returning [] here would read as 'the fleet is empty'."""
    assert fleet.sql_rows(_Res(1, "")) is None


def test_sql_rows_is_none_on_unparseable_output():
    assert fleet.sql_rows(_Res(0, "Warning: something\nnot json")) is None


# ---- shape H: fleet.once, pool-safe (bh-gy7bc / bh-w49zv) -------------------


def test_once_collapses_concurrent_callers_to_a_single_execution():
    """THE WHOLE POINT. functools.cache does NOT do this — under a pool all N callers miss and
    all N execute, which is how bh-i6e5g's `bd --version` memo was silently defeated when
    bh-ti7ws made its caller concurrent. Written as a pool because a sequential test passes
    against the broken implementation too."""
    import time

    runs = []

    def slow():
        runs.append(1)
        time.sleep(0.05)  # wide enough that every worker is inside the miss window
        return "v1"

    memo = fleet.once(slow)
    assert fleet.fanout(lambda _: memo(), list(range(15)), workers=15) == ["v1"] * 15
    assert len(runs) == 1, f"expected ONE execution under a 15-way pool, got {len(runs)}"


def test_once_the_same_test_fails_against_functools_cache():
    """Pins WHY fleet.once exists rather than @cache — if this ever stops holding, the stdlib
    grew stampede protection and `once` can go."""
    import time
    from functools import cache

    runs = []

    @cache
    def slow():
        runs.append(1)
        time.sleep(0.05)
        return "v1"

    fleet.fanout(lambda _: slow(), list(range(15)), workers=15)
    assert len(runs) > 1, "functools.cache appears to have gained stampede protection"


def test_once_does_not_cache_a_raised_exception():
    """A cached failure would turn one transient probe failure into a permanently wrong answer
    for the life of the process."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return "ok"

    memo = fleet.once(flaky)
    with pytest.raises(RuntimeError):
        memo()
    assert memo() == "ok"


def test_once_caches_a_falsy_result():
    """`bd --version` returns None when bd is absent — a legitimate answer, not a miss."""
    calls = []

    def none_returner():
        calls.append(1)
        return None

    memo = fleet.once(none_returner)
    assert memo() is None
    assert memo() is None
    assert len(calls) == 1
