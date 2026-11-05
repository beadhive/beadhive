"""Duplicate clusters and cluster-aware review batches (bh-hrx2r).

The 2026-11-04 single-pass triage batched reports by number range, so families such as
#44/#87/#118/#132 were split across batches and no analyst saw the whole family. Here every
pending report is joined to the reports it probably duplicates, by three independent signals:

* ``store``: a pair the bead store flags (``bd find-duplicates``, read through
  ``ReportStore.duplicate_pairs``; that stays on the CLI and arrives here as plain pairs);
* ``text``: stdlib title similarity (the larger of the token Jaccard index and
  :class:`difflib.SequenceMatcher`'s ratio) or body similarity (token Jaccard over bodies long
  enough to mean something);
* ``path``: a code path both reports' proposal evidence cites
  (:func:`.proposal_checks.cited_paths`).

Union-find over those links gives the clusters, and batching packs whole clusters into batches of
at most ``batch_size`` reports: a cluster NEVER splits, so a cluster larger than the batch size is
a batch of its own. Everything is ordered by the caller's item order (the review service passes
reports in issue-number order), so the same input always gives the same clusters and batches.

Pure: standard library only, no I/O, no clock.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from .models import DuplicatePair
from .proposal_checks import cited_paths

#: The default number of reports in one review batch.
DEFAULT_BATCH_SIZE = 20

#: Words too common to say two texts are about the same thing.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "this",
        "to",
        "was",
        "when",
        "with",
    }
)
_TOKEN = re.compile(r"[a-z0-9]+")
_SPACE = re.compile(r"\s+")


class ClusterSignal(StrEnum):
    """Why two reports are in the same cluster."""

    STORE = "store"  # the bead store flags them (bd find-duplicates)
    TEXT = "text"  # their titles or bodies are similar
    PATH = "path"  # their proposals' evidence cites a shared code path


@dataclass(frozen=True)
class ClusterItem:
    """One report as clustering reads it: its title, body and the evidence of its proposal."""

    id: str
    title: str
    body: str = ""
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClusterPolicy:
    """The similarity thresholds. ``title_threshold`` applies to the larger of the title token
    Jaccard index and the title character ratio; ``body_threshold`` to the body token Jaccard
    index, and only when both bodies have at least ``min_body_tokens`` distinct tokens;
    ``min_shared_paths`` cited paths in common link two proposals (0 turns the path signal off)."""

    title_threshold: float = 0.8
    body_threshold: float = 0.6
    min_body_tokens: int = 8
    min_shared_paths: int = 1

    def __post_init__(self) -> None:
        for name in ("title_threshold", "body_threshold"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1], got {value}")
        if self.min_body_tokens < 1:
            raise ValueError("min_body_tokens must be at least 1")
        if self.min_shared_paths < 0:
            raise ValueError("min_shared_paths must not be negative")


@dataclass(frozen=True)
class ClusterLink:
    """One reason ``first`` and ``second`` (in item order) belong together. ``score`` is the
    similarity (the store's own for ``store``; the shared path count for ``path``) and
    ``detail`` names what matched (``title`` / ``body``, or the shared paths)."""

    first: str
    second: str
    kind: ClusterSignal
    score: float
    detail: str = ""


@dataclass(frozen=True)
class Cluster:
    """Reports that probably report the same thing. ``key`` is its first member in item order,
    ``members`` are in item order and ``links`` are every link inside the cluster. A report
    with no link is a cluster of one."""

    key: str
    members: tuple[str, ...]
    links: tuple[ClusterLink, ...] = ()

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class ClusterBatch:
    """One review batch: whole clusters, numbered from 1."""

    number: int
    clusters: tuple[Cluster, ...]

    @property
    def members(self) -> tuple[str, ...]:
        return tuple(m for c in self.clusters for m in c.members)


# -- similarity --------------------------------------------------------------------------------


def tokens(text: str) -> frozenset[str]:
    """The distinct lower-case word tokens of ``text``, without stopwords."""
    return frozenset(t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS)


def jaccard(first: frozenset[str], second: frozenset[str]) -> float:
    """``|a & b| / |a | b|``; 0.0 for two empty sets."""
    union = first | second
    return len(first & second) / len(union) if union else 0.0


def _normal(text: str) -> str:
    return _SPACE.sub(" ", text.lower()).strip()


def title_similarity(first: str, second: str) -> float:
    """The larger of the token Jaccard index and the character ratio of two titles."""
    a, b = _normal(first), _normal(second)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return max(jaccard(tokens(a), tokens(b)), ratio)


# -- union-find --------------------------------------------------------------------------------


class _UnionFind:
    """Union-find over item positions; the root of a set is always its smallest position, so the
    result does not depend on the order links are applied in."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        root = index
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[index] != root:  # path compression
            self._parent[index], index = root, self._parent[index]
        return root

    def union(self, first: int, second: int) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self._parent[max(a, b)] = min(a, b)


def _links(
    items: Sequence[ClusterItem], pairs: Iterable[DuplicatePair], policy: ClusterPolicy
) -> list[tuple[int, int, ClusterLink]]:
    position = {item.id: index for index, item in enumerate(items)}
    found: list[tuple[int, int, ClusterLink]] = []

    def add(i: int, j: int, kind: ClusterSignal, score: float, detail: str = "") -> None:
        i, j = min(i, j), max(i, j)
        found.append((i, j, ClusterLink(items[i].id, items[j].id, kind, round(score, 4), detail)))

    store: dict[tuple[int, int], float] = {}
    for pair in pairs:  # pairs naming a report outside this run are not ours to cluster
        i, j = position.get(pair.first), position.get(pair.second)
        if i is None or j is None or i == j:
            continue
        key = (min(i, j), max(i, j))
        store[key] = max(store.get(key, 0.0), pair.similarity)
    for (i, j), score in sorted(store.items()):
        add(i, j, ClusterSignal.STORE, score)

    bodies = [tokens(item.body) for item in items]
    paths = [frozenset(cited_paths(item.evidence)) for item in items]
    for i, first in enumerate(items):
        for j in range(i + 1, len(items)):
            second = items[j]
            title = title_similarity(first.title, second.title)
            if title >= policy.title_threshold:
                add(i, j, ClusterSignal.TEXT, title, "title")
            elif min(len(bodies[i]), len(bodies[j])) >= policy.min_body_tokens:
                body = jaccard(bodies[i], bodies[j])
                if body >= policy.body_threshold:
                    add(i, j, ClusterSignal.TEXT, body, "body")
            if policy.min_shared_paths:
                shared = paths[i] & paths[j]
                if len(shared) >= policy.min_shared_paths:
                    add(i, j, ClusterSignal.PATH, float(len(shared)), " ".join(sorted(shared)))
    return found


def cluster(
    items: Sequence[ClusterItem],
    pairs: Iterable[DuplicatePair] = (),
    policy: ClusterPolicy | None = None,
) -> tuple[Cluster, ...]:
    """Every item in exactly one cluster, clusters ordered by their first member's position.

    ``items`` must have unique ids; ``pairs`` are the store's duplicate pairs (any pair naming an
    id outside ``items`` is ignored)."""
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("cluster items must have unique ids")
    links = _links(items, pairs, policy or ClusterPolicy())
    sets = _UnionFind(len(items))
    for i, j, _ in links:
        sets.union(i, j)
    members: dict[int, list[str]] = {}
    for index, item in enumerate(items):
        members.setdefault(sets.find(index), []).append(item.id)
    inside: dict[int, list[ClusterLink]] = {}
    for i, _, link in sorted(links, key=lambda found: (found[0], found[1], found[2].kind.value)):
        inside.setdefault(sets.find(i), []).append(link)
    return tuple(
        Cluster(group[0], tuple(group), tuple(inside.get(root, ())))
        for root, group in sorted(members.items())
    )


def batches(
    clusters: Sequence[Cluster], batch_size: int = DEFAULT_BATCH_SIZE
) -> tuple[ClusterBatch, ...]:
    """Pack whole clusters, in order, into batches of at most ``batch_size`` reports.

    A cluster never splits: one that does not fit in the current batch starts the next one, and a
    cluster larger than ``batch_size`` is a batch on its own. Deterministic: the same clusters in
    the same order always give the same batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    packed: list[list[Cluster]] = []
    count = 0
    for group in clusters:
        if not packed or count + group.size > batch_size:
            packed.append([])
            count = 0
        packed[-1].append(group)
        count += group.size
    return tuple(ClusterBatch(number, tuple(group)) for number, group in enumerate(packed, start=1))


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "Cluster",
    "ClusterBatch",
    "ClusterItem",
    "ClusterLink",
    "ClusterPolicy",
    "ClusterSignal",
    "batches",
    "cluster",
    "jaccard",
    "title_similarity",
    "tokens",
]
