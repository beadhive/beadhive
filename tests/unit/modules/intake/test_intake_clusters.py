"""Duplicate clusters and cluster-aware batching (bh-hrx2r), pure: ``domain/clusters.py``.

Three signals join reports (the store's duplicate pairs, stdlib title/body similarity, and code
paths both proposals' evidence cites), union-find makes the clusters, and batching packs WHOLE
clusters, in item order, into batches of at most ``batch_size``. No I/O, no clock.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake.domain import (
    Cluster,
    ClusterItem,
    ClusterPolicy,
    ClusterSignal,
    DuplicatePair,
    batches,
    cluster,
)
from beadhive.modules.intake.domain.clusters import jaccard, title_similarity, tokens

#: Thirty unrelated titles: no two share a word, so no text or store signal joins them.
WORDS = (
    "apple bridge candle dragon engine falcon garden harbor island jungle kettle lantern "
    "meadow needle orchid pepper quartz river saddle tunnel umbrella violin walnut xylophone "
    "yacht zipper anchor blanket canyon dolphin ember fossil glacier helmet igloo jasmine "
    "kayak lemon marble nectar oyster pillow quiver rocket summit thistle unicorn velvet "
    "willow yarrow zenith acorn beacon cactus desert eagle fabric goblet hammock iris jigsaw "
    "koala ladder magnet nutmeg"
).split()


def filler(n: int) -> ClusterItem:
    return ClusterItem(f"r{n:02d}", " ".join(WORDS[2 * n : 2 * n + 2]))


def items(count: int) -> list[ClusterItem]:
    return [filler(n) for n in range(count)]


def test_unrelated_reports_are_clusters_of_one_in_item_order() -> None:
    found = cluster(items(6))
    assert [c.members for c in found] == [(f"r{n:02d}",) for n in range(6)]
    assert all(c.key == c.members[0] and not c.links for c in found)


def test_store_pairs_join_transitively_and_ignore_reports_outside_the_run() -> None:
    pairs = (
        DuplicatePair("r05", "r01", 0.9),
        DuplicatePair("r01", "r03", 0.7),
        DuplicatePair("r03", "elsewhere", 1.0),  # not in this run: ignored
    )
    found = cluster(items(6), pairs)
    joined = [c for c in found if c.size > 1]
    assert len(joined) == 1
    assert joined[0].key == "r01" and joined[0].members == ("r01", "r03", "r05")
    assert {(k.first, k.second, k.kind) for k in joined[0].links} == {
        ("r01", "r05", ClusterSignal.STORE),
        ("r01", "r03", ClusterSignal.STORE),
    }
    assert len(found) == 4


def test_cluster_is_independent_of_pair_order() -> None:
    pairs = [DuplicatePair("r04", "r00", 0.8), DuplicatePair("r02", "r04", 0.8)]
    assert cluster(items(5), pairs) == cluster(items(5), list(reversed(pairs)))


def test_similar_titles_join_by_text() -> None:
    run = [
        *items(3),
        ClusterItem("a", "Crash when opening the settings panel"),
        ClusterItem("b", "crash when opening settings panel"),
    ]
    found = cluster(run)
    assert found[-1].members == ("a", "b")
    (link,) = found[-1].links
    assert (link.kind, link.detail) == (ClusterSignal.TEXT, "title")
    assert link.score >= ClusterPolicy().title_threshold


def test_similar_bodies_join_only_when_long_enough() -> None:
    body = "the export command writes an empty archive whenever the target directory has spaces"
    long_run = [ClusterItem("a", "export broken", body), ClusterItem("b", "archive empty", body)]
    assert [c.members for c in cluster(long_run)] == [("a", "b")]
    short = [ClusterItem("a", "export broken", "it fails"), ClusterItem("b", "zip", "it fails")]
    assert [c.members for c in cluster(short)] == [("a",), ("b",)]


def test_a_shared_cited_path_joins_two_proposals() -> None:
    run = [
        ClusterItem("a", "export broken", evidence=("src/pkg/export.py:12 is the culprit",)),
        ClusterItem("b", "archive empty", evidence=("see src/pkg/export.py#L40-L44",)),
        ClusterItem("c", "colour wrong", evidence=("src/pkg/theme.py:3",)),
    ]
    found = cluster(run)
    assert [c.members for c in found] == [("a", "b"), ("c",)]
    (link,) = found[0].links
    assert (link.kind, link.detail, link.score) == (ClusterSignal.PATH, "src/pkg/export.py", 1.0)
    off = cluster(run, policy=ClusterPolicy(min_shared_paths=0))
    assert [c.members for c in off] == [("a",), ("b",), ("c",)]


def test_three_duplicates_spread_across_the_range_land_in_one_batch() -> None:
    run = items(30)
    pairs = (DuplicatePair("r02", "r15", 0.9), DuplicatePair("r15", "r29", 0.9))
    packed = batches(cluster(run, pairs), batch_size=10)
    holding = [b.number for b in packed if {"r02", "r15", "r29"} & set(b.members)]
    assert holding == [1]
    assert all(len(b.members) <= 10 for b in packed)
    assert sorted(m for b in packed for m in b.members) == sorted(i.id for i in run)


def test_a_cluster_never_splits_and_an_oversized_one_is_its_own_batch() -> None:
    clusters = (
        Cluster("a", ("a", "b")),
        Cluster("c", ("c", "d", "e")),  # does not fit beside a, b in a batch of 4
        Cluster("f", ("f", "g", "h", "i", "j")),  # larger than the batch: alone
        Cluster("k", ("k",)),
    )
    packed = batches(clusters, batch_size=4)
    assert [b.members for b in packed] == [
        ("a", "b"),
        ("c", "d", "e"),
        ("f", "g", "h", "i", "j"),
        ("k",),
    ]
    assert [b.number for b in packed] == [1, 2, 3, 4]


def test_batching_is_deterministic_and_validates_its_size() -> None:
    run = items(25)
    pairs = (DuplicatePair("r01", "r24", 0.6),)
    assert batches(cluster(run, pairs), 7) == batches(cluster(run, pairs), 7)
    with pytest.raises(ValueError):
        batches((), 0)
    with pytest.raises(ValueError):
        cluster([ClusterItem("a", "x"), ClusterItem("a", "y")])


def test_similarity_helpers() -> None:
    assert tokens("The Export, the ARCHIVE!") == frozenset({"export", "archive"})
    assert jaccard(frozenset(), frozenset()) == 0.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == pytest.approx(1 / 3)
    assert title_similarity("", "anything") == 0.0
    assert title_similarity("Same  Title", "same title") == 1.0
    with pytest.raises(ValueError):
        ClusterPolicy(title_threshold=0.0)
