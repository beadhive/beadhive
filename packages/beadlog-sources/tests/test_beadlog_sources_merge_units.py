"""beadlog-sources: merge-unit titles, merge types, ``merge_types`` and branch-sync noise.

The decisions are D2 to D4 and D9 of ``docs/design/backfill-merge-heavy-repos-adr.md``. Set
``BEADLOG_SOURCES_UPDATE_GOLDENS=1`` to rewrite ``golden/merge-units.golden`` (only when an output
change is intended).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import beadlog
from beadlog_sources import (
    ClusterOptions,
    ClusterOptionsError,
    CollationFormatError,
    collate,
    collation_schema,
    dumps_collation,
    loads_collation,
)
from beadlog_sources.cluster import branch_sync, merge_branch
from beadlog_sources.collate import Unit
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GOLDEN = Path(__file__).parent / "golden" / "merge-units.golden"
UPDATE = os.environ.get("BEADLOG_SOURCES_UPDATE_GOLDENS") == "1"
# The 0.37 behaviour: both default-on options turned off.
LEGACY = {"noise": (), "merge_titles": "subject"}
BASE_MEMBERS = {"rules", "include_types", "epics", "areas", "path_depth", "min_epic_units"}


def sha(n: int) -> str:
    return f"{n:040x}"


def key(n: int) -> str:
    return f"git:commit/{sha(n)}"


def at(n: int) -> str:
    return f"2026-01-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"


def commit(n, parents=(), *, cc_type="feat", **kw):
    return ev.change(
        sha(n),
        occurred_at=at(n),
        parents=[sha(p) for p in parents],
        cc_type=cc_type,
        kind="merge" if len(parents) > 1 else "commit",
        **kw,
    )


def run(*artifacts, **options):
    return collate(
        ev.corpus(*artifacts),
        [],
        InMemoryLedgerDirectory({"hq": ["hq-9"]}),
        local="bh",
        options=ClusterOptions(**options),
    )


def units(collation):
    return {u.key: u for u in collation.units}


def merge_world(base, subject, *, body_head=None, kids=(("Add widget", "feat"),), refs=()):
    """A mainline root and one merge (sha ``base``) bringing in ``kids`` (title, cc_type), oldest
    first, on a branch off the root. The root and the mainline commit are ``chore``."""
    root, main = base - 2, base - 1
    arts = [commit(root, cc_type="chore"), commit(main, [root], cc_type="chore")]
    parent = root
    for i, (title, cc_type) in enumerate(kids):
        n = base + 1 + i
        arts.append(commit(n, [parent], cc_type=cc_type, title=title))
        parent = n
    extra = {} if body_head is None else {"body_head": body_head}
    arts.append(commit(base, [main, parent], cc_type=None, title=subject, refs=list(refs), **extra))
    return arts


# -- D4: merge titles ---------------------------------------------------------------------


def test_title_sources_in_order() -> None:
    body = units(
        run(*merge_world(100, "Merge pull request #7 from o/fix/x", body_head="Real PR title"))
    )
    assert body[key(100)].title == "Real PR title"
    # a body head that is itself a merge line is ignored: the first commit wins
    skipped = units(run(*merge_world(100, "Merge pull request #7 from o/x", body_head="Merge foo")))
    assert skipped[key(100)].title == "Add widget"
    # no body: the oldest included commit
    kids = (("Oldest change", "feat"), ("Newer change", "fix"))
    first = units(run(*merge_world(100, "Merge pull request #7 from o/feat/x", kids=kids)))
    assert first[key(100)].title == "Oldest change"
    # no body, a blank commit subject: the branch name made readable
    blank = units(
        run(
            *merge_world(
                100, "Merge pull request #7 from o/fix/render-crash_now", kids=(("", "fix"),)
            )
        )
    )
    assert blank[key(100)].title == "Render crash now"
    plain = units(
        run(*merge_world(100, "Merge pull request #7 from o/patch-1", kids=(("", "fix"),)))
    )
    assert plain[key(100)].title == "Patch 1"
    # nothing to derive from: the merge subject
    none = units(run(*merge_world(100, "Merge something odd", kids=(("", "fix"),))))
    assert none[key(100)].title == "Merge something odd"


def test_subject_titles_are_the_0_37_titles() -> None:
    arts = merge_world(100, "Merge pull request #7 from o/fix/x", body_head="Real PR title")
    got = units(run(*arts, merge_titles="subject"))
    assert got[key(100)].title == "Merge pull request #7 from o/fix/x"


def test_merge_branch_parsing() -> None:
    assert merge_branch("Merge pull request #7 from org/feat/x") == "feat/x"
    assert merge_branch("Merge pull request #7 from org/patch-1") == "patch-1"
    assert merge_branch("Merge branch 'feat/x' into main") == "feat/x"
    assert merge_branch("Merge remote-tracking branch 'origin/fix/y'") == "fix/y"
    assert merge_branch("Merge tag 'v1'") is None


# -- D3: branch-sync noise ----------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "Merge branch 'main' into next",
        "Merge branch 'main'",
        "Merge remote-tracking branch 'origin/main'",
        "Merge remote-tracking branch 'origin/main' into next",
    ],
)
def test_branch_sync_shapes_are_noise_and_release_their_commits(subject) -> None:
    col = run(*merge_world(100, subject, kids=(("Add widget", "feat"), ("Fix widget", "fix"))))
    got = units(col)
    merge = got[key(100)]
    assert (merge.rule, merge.noise) == ("merge", "branch-sync")
    assert merge.artifacts == (key(100),)  # only the merge itself
    assert merge.title == subject and merge.area is None and merge.parent is None
    assert got[key(101)].rule == "commit"  # the feat commit is released to the commit rule
    assert key(102) not in {a for u in col.units for a in u.artifacts}  # fix: not in include_types
    verdict = next(v for v in col.verdicts if v["unit"] == key(100))
    assert (verdict["step"], verdict["class"], verdict["provenance"], verdict["review"]) == (
        "branch-sync",
        "SKIP",
        "derived",
        "none",
    )
    assert not col.residual or all(r["unit"] != key(100) for r in col.residual)


@pytest.mark.parametrize(
    "subject",
    [
        "Merge pull request #7 from org/main",
        "Merge branch 'x' of github.com:o/r",
        "Merge feature",
    ],
)
def test_other_merges_are_never_noise(subject) -> None:
    col = run(*merge_world(100, subject))
    assert units(col)[key(100)].noise is None
    assert all(v["step"] != "branch-sync" for v in col.verdicts)


def test_a_merge_that_refs_a_pr_is_not_noise() -> None:
    arts = merge_world(100, "Merge branch 'main' into next", refs=[ev.ref("#7", "pr")])
    assert units(run(*arts))[key(100)].noise is None
    assert branch_sync(arts[-1]) is False


def test_noise_opt_out_restores_the_unit_and_its_sweep() -> None:
    arts = merge_world(100, "Merge branch 'main' into next", kids=(("Add widget", "feat"),))
    got = units(run(*arts, noise=()))
    assert got[key(100)].noise is None and got[key(100)].artifacts == (key(100), key(101))


def test_noise_units_never_found_an_area_epic() -> None:
    arts = [
        *merge_world(100, "Merge branch 'a' into main"),
        *[a for a in merge_world(200, "Merge branch 'b' into main")],
    ]
    # the two merges share the mainline commits' keys only by construction: rebuild distinct roots
    col = run(*arts, min_epic_units=1)
    assert all(u.parent is None for u in col.units if u.noise)


def _nested_world(outer_subject):
    """M(outer) brings in N, a PR merge of two side commits (N is not a first-parent merge)."""
    return [
        commit(1, cc_type="chore"),
        commit(2, [1], cc_type="chore"),
        commit(3, [1], title="Side one"),
        commit(4, [1], title="Side two"),
        commit(
            5,
            [3, 4],
            cc_type=None,
            title="Merge pull request #9 from o/fix/y",
            body_head="Nested PR",
        ),
        commit(6, [2, 5], cc_type=None, title=outer_subject),
    ]


def test_a_released_merge_hands_its_inner_merges_a_unit_of_their_own() -> None:
    got = units(run(*_nested_world("Merge branch 'next' into main")))
    assert got[key(6)].noise == "branch-sync" and got[key(6)].artifacts == (key(6),)
    inner = got[key(5)]  # the inner PR merge is a unit now, not swallowed or lost
    assert (inner.rule, inner.title, inner.noise) == ("merge", "Nested PR", None)
    assert inner.artifacts == (key(5), key(4))
    assert got[key(3)].rule == "commit"
    # without the sync flag the outer merge keeps sweeping, as in 0.37
    legacy = units(run(*_nested_world("Merge branch 'next' into main"), noise=()))
    assert key(5) not in legacy and set(legacy[key(6)].artifacts) >= {key(5), key(3), key(4)}
    # a merge filtered out by merge_types releases its inner merges the same way
    filtered = units(run(*_nested_world("Merge something odd"), merge_types=["fix"]))
    assert key(6) not in filtered and filtered[key(5)].type == "fix"


def test_an_operator_decision_beats_branch_sync() -> None:
    # step order: decision (0) and external-ref (1) come before branch-sync
    from beadlog_sources.identify import STEPS

    assert STEPS.index("decision") < STEPS.index("branch-sync")
    assert STEPS.index("external-ref") < STEPS.index("branch-sync") < STEPS.index("no-candidate")


# -- D2: merge types ----------------------------------------------------------------------


def test_type_derivation_order() -> None:
    star = {"merge_types": ["*"]}
    by_prefix = units(
        run(
            *merge_world(100, "Merge pull request #7 from o/feature/x", kids=(("a", "docs"),)),
            **star,
        )
    )
    assert by_prefix[key(100)].type == "feat"  # alias, and the prefix beats the commits
    hotfix = units(
        run(*merge_world(100, "Merge branch 'hotfix/x' into main", kids=(("a", "docs"),)), **star)
    )
    assert hotfix[key(100)].type == "fix"
    mixed = (("a", "docs"), ("b", "fix"), ("c", "chore"))
    by_commits = units(
        run(*merge_world(100, "Merge pull request #7 from o/patch-1", kids=mixed), **star)
    )
    assert by_commits[key(100)].type == "fix"  # most significant: fix over docs and chore
    breaking = merge_world(100, "Merge pull request #7 from o/patch-1", kids=(("a", "chore"),))
    breaking[2] = commit(101, [98], cc_type="chore", title="a", breaking=True)
    assert units(run(*breaking, **star))[key(100)].type == "feat"  # breaking counts as feat
    untyped = units(
        run(*merge_world(100, "Merge pull request #7 from o/patch-1", kids=(("a", None),)), **star)
    )
    assert untyped[key(100)].type == "untyped"


def test_merge_types_filters_and_releases_commits() -> None:
    arts = merge_world(100, "Merge pull request #7 from o/fix/x", kids=(("Add widget", "feat"),))
    kept = units(run(*arts, merge_types=["fix"]))
    assert kept[key(100)].type == "fix" and kept[key(100)].artifacts == (key(100), key(101))
    out = units(run(*arts, merge_types=["feat"]))
    assert key(100) not in out  # no merge unit...
    assert out[key(101)].rule == "commit"  # ...its feat commit is a commit unit
    only_untyped = units(
        run(*merge_world(100, "Merge something odd", kids=(("a", None),)), merge_types=["untyped"])
    )
    assert key(100) in only_untyped


def test_star_records_the_type_without_filtering() -> None:
    arts = merge_world(100, "Merge pull request #7 from o/fix/x")
    star = run(*arts, merge_types=["*"])
    plain = run(*arts)
    assert units(star)[key(100)].type == "fix"
    stripped = [Unit(**{**u.__dict__, "type": None}) for u in star.units]
    assert stripped == list(plain.units)


def test_include_types_stays_commit_only() -> None:
    arts = merge_world(100, "Merge pull request #7 from o/docs/x", kids=(("a", "docs"),))
    got = units(run(*arts, include_types=["feat"]))
    assert key(100) in got  # a docs merge is still a unit: include_types never filters merges


def test_merge_types_validation() -> None:
    for bad in (["nope"], "feat", [""]):
        with pytest.raises(ClusterOptionsError):
            ClusterOptions(merge_types=bad)
    for bad in ({"merge_titles": "x"}, {"noise": ["x"]}, {"noise": ["branch-sync"] * 2}):
        with pytest.raises(ClusterOptionsError):
            ClusterOptions(**bad)
    mapped = ClusterOptions.from_mapping(
        {"merge_types": ["fix"], "merge_titles": "subject", "noise": []}
    )
    assert (mapped.merge_types, mapped.merge_titles, mapped.noise) == (("fix",), "subject", ())


# -- D9: one additive schema change -------------------------------------------------------


def _history():
    return [
        *merge_world(100, "Merge pull request #7 from o/fix/x", body_head="PR title"),
        *merge_world(200, "Merge branch 'main' into next"),
    ]


def test_opted_out_run_writes_the_0_37_collation() -> None:
    col = run(*_history(), **LEGACY)
    obj = json.loads(dumps_collation(col))
    assert set(obj["options"]["cluster"]) == BASE_MEMBERS
    assert all(set(u) == set(Unit.__dataclass_fields__) - {"type", "noise"} for u in obj["units"])
    assert not any(v["step"] == "branch-sync" for v in obj["verdicts"])
    assert units(col)[key(100)].title == "Merge pull request #7 from o/fix/x"
    assert loads_collation(dumps_collation(col)) == col


def test_new_members_are_written_only_when_set() -> None:
    default = json.loads(dumps_collation(run(*_history())))
    cluster = default["options"]["cluster"]
    assert cluster["merge_titles"] == "derived" and cluster["noise"] == ["branch-sync"]
    assert "merge_types" not in cluster
    assert any("noise" in u for u in default["units"]) and not any(
        "type" in u for u in default["units"]
    )
    typed = json.loads(dumps_collation(run(*_history(), merge_types=["*"])))
    assert typed["options"]["cluster"]["merge_types"] == ["*"]
    assert {u["type"] for u in typed["units"] if "type" in u} == {"fix", "feat"}


def test_loader_reads_a_0_37_collation_and_the_new_one() -> None:
    legacy = run(*_history(), **LEGACY)
    loaded = loads_collation(dumps_collation(legacy))
    assert ClusterOptions.from_json(loaded.options["cluster"]) == ClusterOptions(**LEGACY)
    for options in ({}, {"merge_types": ["fix", "untyped"]}):
        col = run(*_history(), **options)
        again = loads_collation(dumps_collation(col))
        assert again.units == col.units and again.options == col.options


def test_loader_still_refuses_unknown_members() -> None:
    obj = json.loads(dumps_collation(run(*_history(), merge_types=["*"])))

    def load(patched):
        return loads_collation(beadlog.canonical_json(patched).encode() + b"\n")

    for bad_unit in ({"extra": 1}, {"type": ""}, {"noise": 3}):
        units_ = [{**obj["units"][0], **bad_unit}, *obj["units"][1:]]
        with pytest.raises(CollationFormatError):
            load({**obj, "units": units_})
    opts = obj["options"]
    for bad_cluster in (
        {"extra": 1},
        {"noise": ["guess"]},
        {"merge_titles": "x"},
        {"merge_types": ["x"]},
    ):
        with pytest.raises(CollationFormatError):
            load({**obj, "options": {**opts, "cluster": {**opts["cluster"], **bad_cluster}}})


def test_schema_accepts_the_new_members() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(collation_schema())
    for options in (LEGACY, {}, {"merge_types": ["fix", "*"]}):
        validator.validate(json.loads(dumps_collation(run(*_history(), **options))))


def test_reader_attr_does_not_break_the_evidence_shape() -> None:
    # body_head is an extra attr: the change shape only requires the 0.37 members
    art = commit(5, [1, 2], cc_type=None, body_head=None)
    assert art.attrs["body_head"] is None


# -- goldens ------------------------------------------------------------------------------


def _rows(collation):
    verdicts = {v["unit"]: v["step"] for v in collation.verdicts if v["class"]}
    return [
        {
            "key": u.key.rsplit("/", 1)[-1].lstrip("0") or "0",
            "rule": u.rule,
            "title": u.title,
            "type": u.type,
            "noise": u.noise,
            "step": verdicts.get(u.key),
            "artifacts": len(u.artifacts),
        }
        for u in collation.units
        if u.rule != "area"
    ]


def _golden_history():
    kids = (("Oldest change", "feat"), ("Newer change", "fix"))
    return [
        *merge_world(100, "Merge pull request #7 from o/fix/x", body_head="Body title"),
        *merge_world(200, "Merge pull request #8 from o/feature/y", kids=kids),
        *merge_world(300, "Merge pull request #9 from o/fix/render-crash", kids=(("", "fix"),)),
        *merge_world(400, "Merge branch 'main' into next", kids=(("Synced change", "feat"),)),
        *merge_world(500, "Merge remote-tracking branch 'origin/main'", kids=(("Other", "docs"),)),
    ]


def test_merge_unit_golden() -> None:
    arts = _golden_history()
    cases = {
        "default": run(*arts),
        "subject-and-no-noise": run(*arts, **LEGACY),
        "merge-types-star": run(*arts, merge_types=["*"]),
        "merge-types-fix": run(*arts, merge_types=["fix"], include_types=["*"]),
    }
    text = (
        json.dumps({name: _rows(col) for name, col in cases.items()}, indent=2, sort_keys=True)
        + "\n"
    )
    if UPDATE:
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
    assert GOLDEN.is_file(), "missing golden: run with BEADLOG_SOURCES_UPDATE_GOLDENS=1"
    assert text == GOLDEN.read_text(encoding="utf-8")
