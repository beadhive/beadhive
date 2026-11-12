"""beadlog-sources: collate B3, clustering artifacts into work units and area epics."""

from __future__ import annotations

import json
from collections import Counter

import pytest

import beadlog
from beadlog_sources import (
    CLAIM_RULES,
    ClusterOptions,
    ClusterOptionsError,
    CollationFormatError,
    claimed_artifacts,
    cluster,
    collate,
    collation_schema,
    dumps_collation,
    loads_collation,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000aa", "bh")


def sha(n: int) -> str:
    return f"{n:040x}"


def key(n: int) -> str:
    return f"git:commit/{sha(n)}"


def at(n: int) -> str:
    return f"2026-01-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"


def commit(n: int, parents=(), *, cc_type="feat", scope=None, paths=(), refs=(), **kw):
    return ev.change(
        sha(n),
        occurred_at=at(n),
        parents=[sha(p) for p in parents],
        cc_type=cc_type,
        cc_scope=scope,
        paths=list(paths),
        refs=list(refs),
        kind="merge" if len(parents) > 1 else "commit",
        **kw,
    )


def _baseline(*ids: str) -> list[beadlog.BeadEvent]:
    return [
        beadlog.mint_event(
            "create",
            beadlog.BeadRef(GROUP.id, bead),
            {"type": "task", "fields": {"title": f"bead {bead}"}},
            occurred_at="2026-07-01T00:00:00Z",
            recorded_at="2026-07-01T00:00:00Z",
            extracted_at="2026-10-31T00:00:00Z",
            source=("bd", f"bd:create/{bead}"),
            provenance="observed",
        )
        for bead in ids
    ]


def run(*artifacts, beads=(), **options):
    return collate(
        ev.corpus(*artifacts),
        _baseline(*beads),
        InMemoryLedgerDirectory({"hq": ["hq-9"]}),
        local="bh",
        options=ClusterOptions(**options),
    )


def units(collation):
    return {u.key: u for u in collation.units}


def assert_claimed_once(collation) -> None:
    claims = [a for u in collation.units for a in u.artifacts]
    assert len(claims) == len(set(claims)) == len(claimed_artifacts(collation.units))


# -- the history used by most rule tests --------------------------------------------------
#
# mainline: 1 <- 2 <- M10(merge 2, 4: PR #7 = 3, 4) <- 11 <- M20(merge 11, 13: branch 12, 13)
#           <- 21(fix) <- 22(docs)


def _history(*, merge_ref=False):
    pr_merge_refs = [ev.ref("#7", "pr")] if merge_ref else []
    return [
        commit(1, scope="cli", paths=["src/cli/a.py"]),
        commit(2, [1], scope="cli", paths=["src/cli/b.py"]),
        commit(3, [2], scope="hq", paths=["src/hq/a.py"]),
        commit(4, [3], scope="hq", paths=["src/hq/b.py"]),
        commit(10, [2, 4], cc_type="chore", scope="merge", refs=pr_merge_refs),
        commit(11, [10], scope="hq", paths=["src/hq/c.py"]),
        commit(12, [11], cc_type="fix", scope="work", paths=["src/work/a.py"]),
        commit(13, [12], scope="work", paths=["src/work/b.py"]),
        commit(20, [11, 13], cc_type=None),
        commit(21, [20], cc_type="fix", paths=["src/x.py"]),
        commit(22, [21], cc_type="docs", paths=["docs/x.md"]),
        ev.review(7, commits=[sha(3), sha(4)], title="PR seven"),
    ]


def test_default_rules_on_a_small_history() -> None:
    col = run(*_history())
    got = units(col)
    pr = got["gh:pr/7"]
    assert (pr.kind, pr.rule, pr.title) == ("issue", "review", "PR seven")
    # the PR claims its commits and the merge whose second-parent side it holds
    assert pr.artifacts == ("gh:pr/7", key(3), key(4), key(10))
    merge = got[key(20)]
    assert merge.rule == "merge" and merge.artifacts == (key(20), key(12), key(13))
    # leftover feat commits are units; fix/docs are skipped
    assert {k for k, u in got.items() if u.rule == "commit"} == {key(1), key(2), key(11)}
    assert key(21) not in claimed_artifacts(col.units)
    assert key(22) not in claimed_artifacts(col.units)
    assert_claimed_once(col)


def test_review_claims_a_merge_that_refs_it() -> None:
    arts = _history(merge_ref=True)
    arts[-1] = ev.review(7, commits=[sha(3)])  # the merge's side is no longer all PR #7
    arts[5] = commit(11, [10], scope="hq", refs=[ev.ref("#7", "pr")])  # not a merge: no claim
    got = units(run(*arts))
    assert got[key(11)].rule == "commit"
    assert got["gh:pr/7"].artifacts == ("gh:pr/7", key(3), key(10))
    assert key(4) in got and got[key(4)].rule == "commit"


def test_merge_joins_the_one_unit_holding_its_side() -> None:
    # without the review rule PR #7's merge forms a merge unit; with it the merge joins it
    no_review = units(run(*_history(), rules=["merge", "commit"]))
    assert no_review[key(10)].artifacts == (key(10), key(3), key(4))
    # side already attached to one bead: the merge joins that bead
    arts = [
        commit(1),
        commit(2, [1], refs=[ev.ref("bh-1")]),
        commit(3, [2], refs=[ev.ref("bh-1")]),
        commit(4, [1, 3], cc_type="chore"),
    ]
    got = units(run(*arts, beads=["bh-1"]))
    assert got["bead:bh-1"].artifacts == (key(2), key(3), key(4))
    # side split across units: the merge stays free
    arts[2] = commit(3, [2], refs=[ev.ref("bh-2")])
    got = units(run(*arts, beads=["bh-1", "bh-2"]))
    assert key(4) not in claimed_artifacts(got.values())


def test_local_bead_attaches_and_own_ref_beats_inherited() -> None:
    arts = [
        commit(1),
        commit(2, [1], refs=[ev.ref("bh-2")]),
        commit(3, [2]),
        commit(4, [1, 3], cc_type="chore", refs=[ev.ref("bh-1"), ev.ref("bh-2", where="body")]),
        commit(5, [4], refs=[ev.ref("hq-9")]),  # foreign: not attached
        commit(6, [5], refs=[ev.ref("bh-404")]),  # unresolved: not attached
        ev.review(3, commits=[sha(6)], refs=[ev.ref("bh-3")]),
    ]
    col = run(*arts, beads=["bh-1", "bh-2", "bh-3"])
    got = units(col)
    one, two, three = got["bead:bh-1"], got["bead:bh-2"], got["bead:bh-3"]
    assert (one.kind, one.rule, one.bead, one.title) == ("attach", "local-bead", "bh-1", "bh-1")
    assert one.artifacts == (key(4), key(3))  # the first local ref wins; 2 keeps its own bead
    assert two.artifacts == (key(2),)
    assert three.artifacts == ("gh:pr/3", key(6))  # the review sweeps its commits
    assert got[key(5)].rule == "commit"
    assert all(u.parent is None and u.area is None for u in (one, two, three))
    assert_claimed_once(col)


def test_plan_items_are_units_with_structure() -> None:
    arts = [
        ev.plan_item("phase-1", order=1),
        ev.plan_item("phase-1/plan-01", parent="phase-1", order=2),
        ev.plan_item("phase-1/plan-02", parent="plan:phase-1", depends_on=["phase-1/plan-01"]),
        ev.plan_item("lonely", parent="nowhere", depends_on=["nothing", "lonely"]),
    ]
    got = units(run(*arts))
    assert got["plan:phase-1"].kind == "epic"
    assert got["plan:phase-1"].children == ("plan:phase-1/plan-01", "plan:phase-1/plan-02")
    assert got["plan:phase-1/plan-02"].parent == "plan:phase-1"
    assert got["plan:phase-1/plan-02"].depends_on == ("plan:phase-1/plan-01",)
    lonely = got["plan:lonely"]
    assert (lonely.kind, lonely.parent, lonely.depends_on) == ("issue", None, ())
    assert all(u.rule == "plan-item" for u in got.values())


def test_release_and_tracker_never_make_units_and_a_lone_doc_is_a_doc_unit() -> None:
    col = run(
        ev.release_marker("v1"),
        ev.tracker_item("12"),
        ev.design_record("docs/a.md"),
        commit(1, cc_type="docs"),
    )
    # B3 leaves docs free; B4 makes the one no unit took a doc-unit
    assert [(u.key, u.rule) for u in col.units] == [("doc:docs/a.md", "doc")]


def test_include_types() -> None:
    assert {u.key for u in run(*_history(), include_types=["fix"]).units} >= {key(21)}
    keys = {u.key for u in run(*_history(), include_types=["fix"], epics=False).units}
    assert keys == {"gh:pr/7", key(20), key(21)}
    keys = {u.key for u in run(*_history(), include_types=["*"], epics=False).units}
    assert keys == {"gh:pr/7", key(20), key(1), key(2), key(11), key(21), key(22)}
    keys = {u.key for u in run(*_history(), include_types=[], epics=False).units}
    assert keys == {"gh:pr/7", key(20)}


@pytest.mark.parametrize("dropped", CLAIM_RULES)
def test_every_rule_toggles(dropped: str) -> None:
    arts = [*_history(), ev.plan_item("p"), commit(30, [22], refs=[ev.ref("bh-1")])]
    rules = [r for r in CLAIM_RULES if r != dropped]
    col = run(*arts, beads=["bh-1"], rules=rules)
    assert dropped not in {u.rule for u in col.units}
    assert {u.rule for u in col.units} >= set(rules) - {dropped}
    assert_claimed_once(col)


def test_rule_order_is_precedence() -> None:
    # merge before review: the merge takes PR #7's commits; the PR keeps only itself
    got = units(run(*_history(), rules=["merge", "review", "commit"]))
    assert got[key(10)].artifacts == (key(10), key(3), key(4))
    assert got["gh:pr/7"].artifacts == ("gh:pr/7",)
    # commit before merge: feat commits on a branch become their own units first
    got = units(run(*_history(), rules=["commit", "merge"]))
    assert got[key(20)].artifacts == (key(20), key(12))


def test_area_epics_by_scope_path_and_release() -> None:
    arts = [
        commit(1, scope="cli", paths=["src/cli/a.py"]),
        commit(2, [1], scope="cli"),
        commit(3, [2], paths=["pkg/x/a.py", "pkg/y/b.py"]),
        commit(4, [3], paths=["pkg/z.py", "README.md"]),
        commit(5, [4]),
        commit(6, [5]),
        commit(7, [6]),
        ev.release_marker("v1", commit_range=sha(5), occurred_at=at(5)),
        ev.release_marker("v2", commit_range=f"{sha(5)}..{sha(6)}", occurred_at=at(6)),
    ]
    got = units(run(*arts, min_epic_units=1))
    assert got["area:cli"].children == (key(1), key(2))
    assert got["area:pkg"].children == (key(3), key(4))  # root files carry no area
    assert got[key(5)].area == "release/v1" and got["area:release/v1"].artifacts == ("git:tag/v1",)
    assert got[key(6)].parent == "area:release/v2"
    assert got[key(7)].area == "release/unreleased"
    assert got["area:cli"].kind == "epic" and got["area:cli"].rule == "area"
    assert got["area:cli"].artifacts == ()
    # a lone unit gets an area but no epic by default
    default = units(run(*arts))
    assert "area:pkg" in default and "area:release/v2" not in default
    assert default[key(6)].area == "release/v2" and default[key(6)].parent is None
    assert not any(u.rule == "area" for u in run(*arts, epics=False).units)


def test_release_window_by_time_when_no_range() -> None:
    arts = [
        commit(1),
        commit(2, [1]),
        ev.release_marker("v1", occurred_at=at(1)),
        ev.release_marker("v0", occurred_at=at(0)),
    ]
    got = units(run(*arts, min_epic_units=1))
    assert got[key(1)].area == "release/v1" and got[key(2)].area == "release/unreleased"


def test_area_override_map() -> None:
    arts = [
        commit(1, scope="cli", paths=["src/cli/a.py"]),
        commit(2, [1], scope="commands", paths=["src/cli/b.py"]),
        commit(3, [2], scope="hq", paths=["src/hq/store/a.py", "src/hq/store/b.py", "x/c.py"]),
        commit(4, [3], scope="hq", paths=["src/hq/a.py"]),
    ]
    areas = {"cli": "cli", "commands": "cli", "src/hq/store/": "storage", "src/hq": "hq-core"}
    got = units(run(*arts, areas=areas, min_epic_units=1))
    assert got["area:cli"].children == (key(1), key(2))  # renamed scopes share one epic
    assert got[key(3)].area == "storage"  # longest prefix, majority over paths
    assert got[key(4)].area == "hq-core"  # a path override beats the scope


def test_determinism_and_round_trip() -> None:
    arts = [*_history(), ev.plan_item("p"), ev.release_marker("v1", commit_range=sha(11))]
    first, second = run(*arts), run(*reversed(arts))
    data = dumps_collation(first)
    assert data == dumps_collation(second)
    loaded = loads_collation(data)
    assert loaded.units == first.units
    assert [u.key for u in first.units] == sorted(u.key for u in first.units)
    with pytest.raises(CollationFormatError):
        cluster(loaded)


def test_schema_validates_units() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    arts = [*_history(), ev.plan_item("p"), commit(30, [22], refs=[ev.ref("bh-1")])]
    obj = json.loads(dumps_collation(run(*arts, beads=["bh-1"])))
    assert {u["kind"] for u in obj["units"]} == {"epic", "issue", "attach"}
    jsonschema.Draft202012Validator(collation_schema()).validate(obj)


def test_loader_refuses_bad_units() -> None:
    obj = json.loads(dumps_collation(run(*_history())))
    unit = obj["units"][0]
    for bad in ({**unit, "kind": "story"}, {**unit, "rule": "guess"}, {**unit, "x": 1}):
        with pytest.raises(CollationFormatError):
            loads_collation(beadlog.canonical_json({**obj, "units": [bad]}).encode() + b"\n")


def test_options_validation_and_mapping() -> None:
    for bad in (
        {"rules": ["review", "review"]},
        {"rules": ["area"]},
        {"rules": "review"},
        {"include_types": [""]},
        {"areas": {"x": ""}},
        {"path_depth": 0},
        {"min_epic_units": True},
        {"epics": "yes"},
    ):
        with pytest.raises(ClusterOptionsError):
            ClusterOptions(**bad)
    opts = ClusterOptions.from_mapping(
        {"create": "off", "include_types": ["feat", "perf"], "areas": {"b": "x", "a": "y"}}
    )
    assert opts.include_types == ("feat", "perf") and list(opts.areas) == ["a", "b"]
    assert opts.rules == CLAIM_RULES and opts == ClusterOptions(
        include_types=("feat", "perf"), areas={"a": "y", "b": "x"}
    )


# -- a mid-size synthetic history -----------------------------------------------------------

SCOPES = ("cli", "hq", "work", "docs", None)
TYPES = ("feat", "fix", "feat", "docs", "chore", "feat", "test", "refactor")


def _synthetic(blocks: int = 175):
    """~250 commits: a mainline with PR branches, plain branch merges, bead refs and tags."""
    arts, n, head = [], 0, None
    expect = Counter()
    mainline = 0

    def add(parents, **kw):
        nonlocal n
        n += 1
        arts.append(commit(n, parents, **kw))
        return n

    for i in range(blocks):
        scope = SCOPES[i % len(SCOPES)]
        paths = [f"src/{scope or 'misc'}/f{i}.py"]
        parents = [head] if head else []
        if i % 10 == 9:  # a PR: three branch commits and a merge that refs it
            side = head
            pr_shas = []
            for j in range(3):
                side = add([side], scope=scope, paths=paths, cc_type=TYPES[(i + j) % 8])
                pr_shas.append(sha(side))
            head = add([head, side], cc_type=None, refs=[ev.ref(f"#{i}", "pr")])
            arts.append(ev.review(i, commits=pr_shas, occurred_at=at(head)))
            expect["review"] += 1
        elif i % 15 == 7:  # a plain branch merge of two commits
            side = add([head], scope=scope, paths=paths, cc_type="fix")
            side = add([side], scope=scope, paths=paths)
            head = add([head, side], cc_type="chore", scope="merge")
            expect["merge"] += 1
        elif i % 23 == 11:  # a commit naming a local bead
            head = add(parents, scope=scope, paths=paths, refs=[ev.ref(f"bh-{i}")])
            expect["local-bead"] += 1
        else:
            cc_type = TYPES[i % 8]
            head = add(parents, scope=scope, paths=paths, cc_type=cc_type)
            expect["commit"] += cc_type == "feat"
        mainline += 1
        if mainline % 50 == 0:
            arts.append(ev.release_marker(f"v{mainline // 50}", commit_range=sha(head)))
    beads = [f"bh-{i}" for i in range(blocks) if i % 23 == 11 and i % 10 != 9 and i % 15 != 7]
    return arts, beads, expect, n


def test_mid_size_history() -> None:
    arts, beads, expect, commits = _synthetic()
    assert 230 <= commits <= 270
    col = run(*arts, beads=beads)
    rules = Counter(u.rule for u in col.units)
    for rule in ("review", "merge", "local-bead", "commit"):
        assert rules[rule] == expect[rule], rule
    assert_claimed_once(col)
    got = units(col)
    # every PR claimed its three commits and its merge; every merge unit its side
    assert all(len(u.artifacts) == 5 for u in got.values() if u.rule == "review")
    assert all(len(u.artifacts) == 3 for u in got.values() if u.rule == "merge")
    # areas follow the scopes; the scope-less commits fall to their path area
    epics = {u.key for u in col.units if u.rule == "area"}
    assert epics == {"area:cli", "area:hq", "area:work", "area:docs", "area:src"}
    grouped = [u for u in col.units if u.rule in ("review", "merge", "commit")]
    assert all(u.parent in epics for u in grouped)
    assert sum(len(got[e].children) for e in epics) == len(grouped)
    # no release, tracker or merge-only units; every unit lists its member artifact keys
    assert all(u.artifacts for u in col.units if u.rule != "area")
    assert dumps_collation(col) == dumps_collation(run(*arts, beads=beads))


def test_options_are_recorded() -> None:
    arts = _history()
    default = run(*arts)
    assert default.options["cluster"] == ClusterOptions().to_json()
    assert set(default.options) == {"cluster", "attach", "identify", "status"}
    variants = [
        run(*arts, include_types=["feat", "fix"]),
        run(*arts, areas={"hq": "core"}),
        run(*arts, rules=["review", "merge"]),
        run(*arts, min_epic_units=3),
    ]
    data = [dumps_collation(c) for c in (default, *variants)]
    assert len(set(data)) == len(data)
    # options alone change the bytes: same units, different recorded options
    same_units = run(*arts, path_depth=2)
    assert same_units.units == default.units and dumps_collation(same_units) != data[0]
    for col in (default, *variants):
        loaded = loads_collation(dumps_collation(col))
        assert loaded == col and loaded.options == col.options
        assert ClusterOptions.from_json(loaded.options["cluster"]) == ClusterOptions(
            **{k: v for k, v in col.options["cluster"].items()}
        )


def test_options_schema_and_strict_loading() -> None:
    obj = json.loads(dumps_collation(run(*_history(), areas={"src/hq/": "hq"})))
    assert obj["options"]["cluster"]["areas"] == {"src/hq/": "hq"}
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(collation_schema()).validate(obj)

    def load(options):
        return loads_collation(beadlog.canonical_json({**obj, "options": options}).encode() + b"\n")

    cluster_opts = obj["options"]["cluster"]
    attach_opts = obj["options"]["attach"]
    for bad in (
        {},
        {"cluster": cluster_opts},
        {"cluster": cluster_opts, "attach": {}},
        {"cluster": cluster_opts, "attach": attach_opts, "extra": {}},
        {"attach": attach_opts, "cluster": {**cluster_opts, "extra": 1}},
        {"attach": attach_opts, "cluster": {k: v for k, v in cluster_opts.items() if k != "epics"}},
        {"attach": attach_opts, "cluster": {**cluster_opts, "rules": ["guess"]}},
        {"attach": attach_opts, "cluster": {**cluster_opts, "path_depth": 0}},
    ):
        with pytest.raises(CollationFormatError):
            load(bad)
    missing = {k: v for k, v in obj.items() if k != "options"}
    with pytest.raises(CollationFormatError):
        loads_collation(beadlog.canonical_json(missing).encode() + b"\n")
