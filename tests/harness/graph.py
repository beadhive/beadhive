"""Work-graph shapes seeded into a hive's beads via real `bd create` + `bd dep add`.

Each builder returns the bead ids in a valid topological order (parents before children).
Dependencies use Beads' native `dep add <child> <parent>` (parent blocks child), so the
coordinator's `bd ready` loop naturally yields them in dependency order.
"""

from __future__ import annotations

from . import beads
from .hive import Hive


def independent(hive: Hive, n: int = 3) -> list[str]:
    return [beads.create(hive.main, f"task {i}") for i in range(n)]


def chain(hive: Hive, n: int = 3) -> list[str]:
    ids = [beads.create(hive.main, f"step {i}") for i in range(n)]
    for child, parent in zip(ids[1:], ids[:-1], strict=True):
        beads.dep_add(hive.main, child, parent)  # step i+1 depends on step i
    return ids


def fanout(hive: Hive, n: int = 3) -> list[str]:
    root = beads.create(hive.main, "root")
    leaves = [beads.create(hive.main, f"leaf {i}") for i in range(n)]
    for leaf in leaves:
        beads.dep_add(hive.main, leaf, root)
    return [root, *leaves]


def diamond(hive: Hive) -> list[str]:
    a = beads.create(hive.main, "a")
    b = beads.create(hive.main, "b")
    c = beads.create(hive.main, "c")
    d = beads.create(hive.main, "d")
    beads.dep_add(hive.main, b, a)
    beads.dep_add(hive.main, c, a)
    beads.dep_add(hive.main, d, b)
    beads.dep_add(hive.main, d, c)
    return [a, b, c, d]
