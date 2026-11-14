"""beadlog-sources: B4 for merge-heavy repositories (``attach.max_per_unit``, ``rename_guard``,
the opt-in ``own-add`` tier and ``attach.rank = proximity``).

The decision is D8 (with D9's conditional serialization) of
``docs/design/backfill-merge-heavy-repos-adr.md``. Every scenario runs under three cluster
settings: the 0.38 defaults, the 0.37 opt-outs and ``merge_types = ["*"]``.
"""

from __future__ import annotations

import json

import pytest

import beadlog
from beadlog_sources import (
    Attachment,
    AttachOptions,
    AttachOptionsError,
    ClusterOptions,
    CollationFormatError,
    collate,
    collation_schema,
    dumps_collation,
    loads_collation,
)
from beadlog_sources.attach import KNOWN_TIERS
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

CLUSTERS = [
    pytest.param({}, id="defaults"),
    pytest.param({"noise": (), "merge_titles": "subject"}, id="legacy"),
    pytest.param({"merge_types": ["*"]}, id="merge-types"),
]
OWN_ADD_TIERS = ["explicit", "same-change", "own-add", "shared-path", "lexical"]


def sha(n: int) -> str:
    return f"{n:040x}"


def key(n: int) -> str:
    return f"git:commit/{sha(n)}"


def at(n: int) -> str:
    return f"2026-01-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"


def commit(n, parents=(), *, cc_type="feat", paths=(), **kw):
    return ev.change(
        sha(n),
        occurred_at=at(n),
        parents=[sha(p) for p in parents],
        cc_type=cc_type,
        paths=list(paths),
        **kw,
    )


def doc(path: str, title: str | None = None):
    return ev.design_record(path, **({"title": title} if title else {}))


def run(*artifacts, cluster=None, **options):
    return collate(
        ev.corpus(*artifacts),
        [],
        InMemoryLedgerDirectory({"hq": ["hq-9"]}),
        local="bh",
        options=ClusterOptions(**(cluster or {})),
        attach_options=AttachOptions(**options),
    )


def att(col):
    return {a.doc: a for a in col.attachments}


def pile(n_docs: int = 10):
    """Unit 1 (a feat commit) and ``n_docs`` docs, each first added by a docs commit no unit
    holds and later touched by unit 1's commit: ten shared-path candidates for one unit."""
    names = [f"docs/d{i}.md" for i in range(n_docs)]
    arts = [commit(10 + i, cc_type="docs", paths=[n]) for i, n in enumerate(names)]
    arts.append(commit(100, [10 + n_docs - 1], paths=["src/a.py", *names]))
    arts += [doc(n) for n in names]
    return names, arts


# -- R1: max_per_unit ---------------------------------------------------------------------


@pytest.mark.parametrize("cluster", CLUSTERS)
def test_cap_binds_the_guessing_tiers_and_overflow_stands_alone(cluster) -> None:
    names, arts = pile(10)
    col = run(*arts, cluster=cluster, max_per_unit=3)
    got = att(col)
    attached = sorted(d for d, a in got.items() if a.reason == "shared-path")
    alone = sorted(d for d, a in got.items() if a.reason == "standalone")
    # equal evidence: the smaller doc keys keep the unit's places, deterministically
    assert attached == [f"doc:{n}" for n in sorted(names)[:3]]
    assert len(alone) == 7 and all(got[d].unit == d for d in alone)
    assert dumps_collation(col) == dumps_collation(
        run(*reversed(arts), cluster=cluster, max_per_unit=3)
    )


@pytest.mark.parametrize("cluster", CLUSTERS)
def test_unset_cap_is_byte_identical_and_changes_nothing(cluster) -> None:
    _, arts = pile(10)
    plain = run(*arts, cluster=cluster)
    assert {a.reason for a in plain.attachments} == {"shared-path"}
    assert plain.options["attach"] == {
        "tiers": ["explicit", "same-change", "shared-path", "lexical"],
        "lexical_percent": 60,
        "lexical_min_tokens": 2,
        "standalone": True,
    }
    assert b"runner_up" not in dumps_collation(plain)
    assert dumps_collation(plain) == dumps_collation(run(*arts, cluster=cluster, max_per_unit=None))


def test_overflow_falls_to_the_next_tier() -> None:
    # unit 1 is full after one shared-path doc; the second doc is lexically close to unit 2
    arts = [
        commit(10, cc_type="docs", paths=["docs/a.md"]),
        commit(11, cc_type="docs", paths=["docs/quantum-scheduler.md"]),
        commit(100, [11], paths=["src/a.py", "docs/a.md", "docs/quantum-scheduler.md"]),
        commit(200, [100], paths=["src/b.py"], title="feat: quantum scheduler gizmo"),
        doc("docs/a.md"),
        doc("docs/quantum-scheduler.md", "Quantum scheduler gizmo handler"),
    ]
    uncapped = att(run(*arts))
    assert uncapped["doc:docs/quantum-scheduler.md"].unit == key(100)
    capped = att(run(*arts, max_per_unit=1))
    assert capped["doc:docs/a.md"].unit == key(100)
    second = capped["doc:docs/quantum-scheduler.md"]
    assert (second.unit, second.reason, second.evidence) == (key(200), "lexical", (key(200),))


def test_explicit_and_same_change_are_exempt_from_the_cap() -> None:
    names = ["docs/a.md", "docs/b.md", "docs/c.md"]
    arts = [commit(1, paths=["src/a.py", *names])] + [doc(n) for n in names]
    got = att(run(*arts, max_per_unit=1))
    assert {a.reason for a in got.values()} == {"same-change"}
    assert {a.unit for a in got.values()} == {key(1)}
    explicit = [
        commit(1, paths=["src/a.py"]),
        *[ev.design_record(n, refs=[ev.ref(sha(1), "sha", "body")]) for n in names],
    ]
    assert {a.reason for a in att(run(*explicit, max_per_unit=1)).values()} == {"explicit"}


def test_cap_prefers_stronger_evidence() -> None:
    weak, strong = "docs/a.md", "docs/z.md"
    arts = [
        commit(10, cc_type="docs", paths=[weak]),
        commit(11, [10], cc_type="docs", paths=[strong]),
        commit(100, [11], paths=["src/a.py", weak, strong]),
        commit(101, [100], paths=[strong]),
        ev.review(9, commits=[sha(100), sha(101)], title="PR nine"),
        doc(weak),
        doc(strong),
    ]
    got = att(run(*arts, max_per_unit=1))
    assert got[f"doc:{strong}"].reason == "shared-path"  # two changes beat one, despite the key
    assert got[f"doc:{weak}"].reason == "standalone"


# -- R2: rename_guard ----------------------------------------------------------------------


def reorganise(n_docs: int = 4):
    """Docs authored long ago, then moved into docs/study/ by one commit that sits in a feat
    unit (the moving commit counts as the add, so the unit would collect them)."""
    new = [f"docs/study/s{i}.md" for i in range(n_docs)]
    arts = [commit(1, paths=["src/a.py"])]
    arts.append(commit(50, [1], paths=[*new, "docs/old.md"], renamed=list(new)))
    arts += [doc(n) for n in new]
    return new, arts


@pytest.mark.parametrize("cluster", CLUSTERS)
def test_rename_guard_keeps_moved_docs_out_of_the_moving_unit(cluster) -> None:
    new, arts = reorganise()
    off = att(run(*arts, cluster=cluster))
    assert {off[f"doc:{n}"].unit for n in new} == {key(50)}
    assert {a.reason for a in off.values()} == {"same-change"}
    on = att(run(*arts, cluster=cluster, rename_guard=True))
    assert {a.reason for a in on.values()} == {"standalone"}
    # a doc the same commit really authored (not in renamed) still attaches
    fresh = [*arts, doc("docs/fresh.md")]
    fresh[1] = commit(50, [1], paths=[*new, "docs/fresh.md", "src/b.py"], renamed=list(new))
    got = att(run(*fresh, cluster=cluster, rename_guard=True))
    assert got["doc:docs/fresh.md"].reason == "same-change"
    assert all(got[f"doc:{n}"].reason == "standalone" for n in new)


def test_rename_guard_does_not_use_a_moving_commit_as_shared_path_evidence() -> None:
    arts = [
        commit(1, paths=["src/a.py"]),
        commit(50, [1], cc_type="docs", paths=["docs/s.md", "docs/o.md"]),
        commit(60, [50], paths=["docs/s.md", "docs/o.md"], renamed=["docs/s.md"]),
        doc("docs/s.md"),
    ]
    assert att(run(*arts))["doc:docs/s.md"].unit == key(60)
    guarded = att(run(*arts, rename_guard=True))["doc:docs/s.md"]
    assert guarded.reason == "standalone"


# -- R3: own-add ---------------------------------------------------------------------------


@pytest.mark.parametrize("cluster", CLUSTERS)
def test_own_add_makes_a_unit_of_a_doc_whose_add_commit_no_unit_claimed(cluster) -> None:
    cluster = {**cluster, "include_types": ["feat"]}
    arts = [
        commit(10, cc_type="docs", paths=["docs/design/adr.md"]),
        commit(20, [10], paths=["src/a.py", "docs/design/adr.md"]),
        doc("docs/design/adr.md", "ADR for the widget"),
    ]
    off = att(run(*arts, cluster=cluster))["doc:docs/design/adr.md"]
    assert (off.reason, off.unit) == ("shared-path", key(20))
    col = run(*arts, cluster=cluster, tiers=OWN_ADD_TIERS)
    got = att(col)["doc:docs/design/adr.md"]
    assert (got.unit, got.reason) == ("doc:docs/design/adr.md", "own-add")
    assert got.evidence == (key(10), key(20))  # the add commit, then the implementing anchor
    unit = col.unit("doc:docs/design/adr.md")
    assert unit is not None and (unit.kind, unit.rule, unit.artifacts) == (
        "issue",
        "doc",
        ("doc:docs/design/adr.md",),
    )
    assert col.options["attach"]["tiers"] == OWN_ADD_TIERS
    assert loads_collation(dumps_collation(col)) == col


def test_own_add_needs_an_unclaimed_add_commit_and_respects_the_tier_order() -> None:
    # the add commit is claimed (feat unit holding code): same-change wins before own-add
    arts = [commit(1, paths=["src/a.py", "docs/d.md"]), doc("docs/d.md")]
    assert att(run(*arts, tiers=OWN_ADD_TIERS))["doc:docs/d.md"].reason == "same-change"
    # placed after shared-path, own-add never fires for a doc shared-path took
    late = ["explicit", "same-change", "shared-path", "own-add", "lexical"]
    arts = [
        commit(10, cc_type="docs", paths=["docs/d.md"]),
        commit(20, [10], paths=["src/a.py", "docs/d.md"]),
        doc("docs/d.md"),
    ]
    got = att(run(*arts, cluster={"include_types": ["feat"]}, tiers=late))["doc:docs/d.md"]
    assert got.reason == "shared-path"


def test_own_add_obeys_the_rename_guard() -> None:
    arts = [
        commit(10, cc_type="docs", paths=["docs/d.md"], renamed=["docs/d.md"]),
        doc("docs/d.md"),
    ]
    cluster = {"include_types": ["feat"]}
    assert att(run(*arts, cluster=cluster, tiers=OWN_ADD_TIERS))["doc:docs/d.md"].reason == (
        "own-add"
    )
    guarded = run(*arts, cluster=cluster, tiers=OWN_ADD_TIERS, rename_guard=True)
    assert att(guarded)["doc:docs/d.md"].reason == "standalone"


# -- R4: ranking ---------------------------------------------------------------------------


def reporter_world():
    """A design doc added by a docs commit no unit holds, then touched by a feat commit five
    minutes later whose title matches it and by an unrelated PR (two commits) ten hours later."""
    path = "docs/design/quantum.md"
    return path, [
        commit(10, cc_type="docs", paths=[path]),
        commit(300, [10], paths=["src/q.py", path], title="feat: quantum scheduler gizmo"),
        commit(36000, [300], paths=["src/z.py", path]),
        commit(36001, [36000], paths=[path]),
        ev.review(9, commits=[sha(36000), sha(36001)], title="PR nine", occurred_at=at(36001)),
        doc(path, "Quantum scheduler gizmo design"),
    ]


@pytest.mark.parametrize("cluster", CLUSTERS)
def test_proximity_picks_the_nearby_matching_unit_and_records_the_runner_up(cluster) -> None:
    path, arts = reporter_world()
    default = att(run(*arts, cluster={**cluster, "include_types": ["feat"]}))[f"doc:{path}"]
    assert default.unit == "gh:pr/9" and default.runner_up is None  # more evidence, far away
    col = run(*arts, cluster={**cluster, "include_types": ["feat"]}, rank="proximity")
    got = att(col)[f"doc:{path}"]
    assert (got.unit, got.reason) == (key(300), "shared-path")
    assert got.runner_up == {"unit": "gh:pr/9", "distance": 36001 - 10, "overlap": 0, "paths": 3}
    assert col.options["attach"]["rank"] == "proximity"
    assert loads_collation(dumps_collation(col)).attachments == col.attachments
    assert dumps_collation(col) == dumps_collation(
        run(*reversed(arts), cluster={**cluster, "include_types": ["feat"]}, rank="proximity")
    )


def test_proximity_breaks_ties_by_overlap_then_edit_size_then_key() -> None:
    path = "docs/d.md"
    arts = [
        commit(10, cc_type="docs", paths=[path]),
        commit(20, [10], paths=["src/a.py", path], title="feat: alpha"),
        commit(30, [10], paths=["a", "b", "c", path], title="feat: beta"),
        doc(path, "Alpha notes"),
    ]
    got = att(run(*arts, rank="proximity"))[f"doc:{path}"]
    # 20 and 30 are 10 and 20 seconds from the add: the nearer one wins on distance alone
    assert got.unit == key(20) and got.runner_up["unit"] == key(30)


def test_a_lone_candidate_has_no_runner_up() -> None:
    arts = [
        commit(10, cc_type="docs", paths=["docs/d.md"]),
        commit(20, [10], paths=["src/a.py", "docs/d.md"]),
        doc("docs/d.md"),
    ]
    got = att(run(*arts, cluster={"include_types": ["feat"]}, rank="proximity"))["doc:docs/d.md"]
    assert got.unit == key(20) and got.runner_up is None


# -- options, serialization, schema ----------------------------------------------------------


def test_option_validation_and_conditional_serialization() -> None:
    assert KNOWN_TIERS[2] == "own-add"
    assert AttachOptions().to_json().keys() == {
        "tiers",
        "lexical_percent",
        "lexical_min_tokens",
        "standalone",
    }
    full = AttachOptions(max_per_unit=2, rename_guard=True, rank="proximity")
    assert {k: v for k, v in full.to_json().items() if k not in AttachOptions().to_json()} == {
        "max_per_unit": 2,
        "rename_guard": True,
        "rank": "proximity",
    }
    assert AttachOptions.from_json(full.to_json()) == full
    base = AttachOptions().to_json()
    assert AttachOptions.from_json({**base, "rank": "proximity"}).rank == "proximity"
    assert AttachOptions.from_mapping({"max_per_unit": None, "rank": "evidence"}) == AttachOptions()
    for bad in (
        {"max_per_unit": 0},
        {"max_per_unit": True},
        {"max_per_unit": 1.5},
        {"rename_guard": 1},
        {"rank": "nearest"},
    ):
        with pytest.raises(AttachOptionsError):
            AttachOptions(**bad)
    with pytest.raises(AttachOptionsError):
        AttachOptions.from_json({**base, "extra": 1})


def test_runner_up_serialization_and_strict_loading() -> None:
    path, arts = reporter_world()
    col = run(
        *arts,
        cluster={"include_types": ["feat"]},
        rank="proximity",
        max_per_unit=2,
        rename_guard=True,
    )
    obj = json.loads(dumps_collation(col))
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(collation_schema()).validate(obj)
    first = obj["attachments"][0]
    assert "runner_up" in first
    for bad in (
        {**first, "runner_up": {**first["runner_up"], "extra": 1}},
        {**first, "runner_up": {"unit": "x"}},
        {**first, "runner_up": {**first["runner_up"], "overlap": -1}},
        {**first, "extra": 1},
    ):
        with pytest.raises(CollationFormatError):
            loads_collation(beadlog.canonical_json({**obj, "attachments": [bad]}).encode() + b"\n")
    with pytest.raises(CollationFormatError):
        Attachment("doc:a", "doc:a", "standalone", (), {"unit": "u"})
    # a 0.37 collation (no new members anywhere) still reads and writes the same bytes
    _, plain = pile(3)
    old = run(*plain)
    assert loads_collation(dumps_collation(old)) == old
