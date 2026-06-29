"""Work-graph shapes seeded into a rig's beads via real `bd create` + `bd dep add`.

Each builder returns the bead ids in a valid topological order (parents before children).
Dependencies use Beads' native `dep add <child> <parent>` (parent blocks child), so the
coordinator's `bd ready` loop naturally yields them in dependency order.
"""

from __future__ import annotations

from . import beads
from .rig import Rig


def independent(rig: Rig, n: int = 3) -> list[str]:
    return [beads.create(rig.main, f"task {i}") for i in range(n)]


def chain(rig: Rig, n: int = 3) -> list[str]:
    ids = [beads.create(rig.main, f"step {i}") for i in range(n)]
    for child, parent in zip(ids[1:], ids[:-1], strict=True):
        beads.dep_add(rig.main, child, parent)  # step i+1 depends on step i
    return ids


def fanout(rig: Rig, n: int = 3) -> list[str]:
    root = beads.create(rig.main, "root")
    leaves = [beads.create(rig.main, f"leaf {i}") for i in range(n)]
    for leaf in leaves:
        beads.dep_add(rig.main, leaf, root)
    return [root, *leaves]


def diamond(rig: Rig) -> list[str]:
    a = beads.create(rig.main, "a")
    b = beads.create(rig.main, "b")
    c = beads.create(rig.main, "c")
    d = beads.create(rig.main, "d")
    beads.dep_add(rig.main, b, a)
    beads.dep_add(rig.main, c, a)
    beads.dep_add(rig.main, d, b)
    beads.dep_add(rig.main, d, c)
    return [a, b, c, d]
