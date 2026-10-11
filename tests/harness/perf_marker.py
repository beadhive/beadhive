"""Keep `@pytest.mark.perf` tests out of every gate lane (bh-zbn8a).

A perf test asserts a measured wall-clock latency or overlap, which flakes under host contention
(`-n auto`, concurrent gates). It is deselected unless explicitly requested with `-m perf` (any
marker expression naming `perf`) or `BH_PERF_TESTS=1`, so it runs only on a quiet host.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("BH_PERF_TESTS") == "1" or "perf" in (config.getoption("markexpr") or ""):
        return
    kept = [item for item in items if item.get_closest_marker("perf") is None]
    dropped = [item for item in items if item.get_closest_marker("perf") is not None]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
