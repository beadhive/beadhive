from __future__ import annotations

import pytest

from stateful_fixtures import _interleave_dolt_items


class Item:
    def __init__(self, name: str, marked: bool):
        self.name = name
        self.marked = marked

    def get_closest_marker(self, name: str):
        return object() if name == "dolt_server" and self.marked else None


@pytest.mark.parametrize("slots", [1, 2, 4])
def test_ready_work_follows_each_bounded_dolt_wave(slots):
    marked = [Item(f"dolt-{index}", True) for index in range(slots * 3)]
    ready = [Item(f"ready-{index}", False) for index in range(slots * 2)]
    names = [item.name for item in _interleave_dolt_items([*marked, *ready], slots)]
    assert names[:slots] == [item.name for item in marked[:slots]]
    assert names[slots : slots * 2] == [item.name for item in ready[:slots]]
    assert names[slots * 2 : slots * 3] == [item.name for item in marked[slots : slots * 2]]


def test_explicit_unbounded_mode_preserves_collection_order():
    items = [Item("dolt", True), Item("ready", False), Item("dolt-2", True)]
    assert _interleave_dolt_items(items, 0) == items


def test_ready_cases_dispatch_while_excess_dolt_cases_remain_queued():
    """At four permits, the first dispatch has four holders plus four ordinary cases.

    ``test_the_slot_actually_excludes`` independently proves the filesystem semaphore enforces
    its holder count; this regression proves excess marked cases wait in the scheduler queue
    instead of consuming the other four worker dispatch positions.
    """
    marked = [Item(f"dolt-{index}", True) for index in range(12)]
    ready = [Item(f"ready-{index}", False) for index in range(8)]

    before_window = [*marked, *ready][:8]
    scheduled = _interleave_dolt_items([*marked, *ready], slots=4)
    dispatched, queued = scheduled[:8], scheduled[8:]

    assert sum(item.marked for item in before_window) == 8  # four holders plus four waiters
    assert [item.name for item in dispatched] == [
        "dolt-0",
        "dolt-1",
        "dolt-2",
        "dolt-3",
        "ready-0",
        "ready-1",
        "ready-2",
        "ready-3",
    ]
    assert sum(not item.marked for item in dispatched) == 4  # useful work is dispatched
    assert sum(item.marked for item in queued) == 8  # excess server cases wait in scheduler queue
