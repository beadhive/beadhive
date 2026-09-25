"""Work-graph shapes seeded into a hive's beads via real `bd create` + `bd dep add`.

Each builder returns the bead ids in a valid topological order (parents before children).
Dependencies use Beads' native `dep add <child> <parent>` (parent blocks child), so the
coordinator's `bd ready` loop naturally yields them in dependency order.
"""

from __future__ import annotations

from . import beads
from .hive import Hive


def _create(hive: Hive, titles: list[tuple[str, str]], edges: list[tuple[str, str]]) -> list[str]:
    ids = beads.create_graph(
        hive.main,
        [{"key": key, "title": title, "type": "task", "priority": 2} for key, title in titles],
        [{"from_key": child, "to_key": parent, "type": "blocks"} for child, parent in edges],
    )
    return [ids[key] for key, _title in titles]


def independent(hive: Hive, n: int = 3) -> list[str]:
    return _create(hive, [(f"task-{i}", f"task {i}") for i in range(n)], [])


def chain(hive: Hive, n: int = 3) -> list[str]:
    titles = [(f"step-{i}", f"step {i}") for i in range(n)]
    edges = [(child[0], parent[0]) for child, parent in zip(titles[1:], titles[:-1], strict=True)]
    return _create(hive, titles, edges)


def fanout(hive: Hive, n: int = 3) -> list[str]:
    root = beads.create(hive.main, "root")
    leaves = [beads.create(hive.main, f"leaf {i}") for i in range(n)]
    for leaf in leaves:
        beads.dep_add(hive.main, leaf, root)
    return [root, *leaves]


def diamond(hive: Hive) -> list[str]:
    return _create(
        hive,
        [(key, key) for key in ("a", "b", "c", "d")],
        [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")],
    )
