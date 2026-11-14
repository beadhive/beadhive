"""beadlog-sources: hidden-commit reporting, the opt-in merge fallback and commit-based identity.

The decisions are D1, D5 and D9 of ``docs/design/backfill-merge-heavy-repos-adr.md`` (#154 R4 to
R6). Nothing here depends on a clock: the fixtures are in-memory evidence and baselines.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import beadlog
from beadlog.testing import InMemoryStore
from beadlog_sources import (
    ClusterOptions,
    CollationFormatError,
    IdentifyOptions,
    IdentifyOptionsError,
    ProjectOptions,
    collate,
    collation_schema,
    decision_events,
    dumps_collation,
    hidden_commits,
    hidden_summary,
    loads_collation,
    project,
    reconcile,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000aa"
PREFIX = "bh"
EXTRACTED = "2026-11-14T00:00:00Z"
LABELS = ("origin:backfill", "provider:github", "org:o", "repo:r")


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
        committed_at=at(n),
        parents=[sha(p) for p in parents],
        cc_type=cc_type,
        kind="merge" if len(parents) > 1 else "commit",
        **kw,
    )


def bead(ident: str, title: str, *, type_: str = "task", ref: str | None = None):
    fields: dict[str, Any] = {"title": title}
    if ref is not None:
        fields["external_ref"] = ref
    return beadlog.mint_event(
        "create",
        beadlog.BeadRef(GROUP, ident),
        {"type": type_, "fields": fields},
        occurred_at="2025-06-01T00:00:00Z",
        recorded_at="2025-06-01T00:00:00Z",
        extracted_at=EXTRACTED,
        source=("bd", f"bd:create/{ident}"),
        provenance="observed",
    )


def merge_world(base, subject, *, body_head=None, kids=(("Add widget", "feat"),)):
    """A mainline root and one merge (sha ``base``) bringing in ``kids`` (title, cc_type), oldest
    first, on a branch off the root."""
    root, main = base - 2, base - 1
    arts = [commit(root, cc_type="chore"), commit(main, [root], cc_type="chore")]
    parent = root
    for i, (title, cc_type) in enumerate(kids):
        n = base + 1 + i
        arts.append(commit(n, [parent], cc_type=cc_type, title=title))
        parent = n
    extra = {} if body_head is None else {"body_head": body_head}
    arts.append(commit(base, [main, parent], cc_type=None, title=subject, **extra))
    return arts


def run(arts, baseline=(), *, identify=None, cluster=None):
    return collate(
        ev.corpus(*arts),
        baseline,
        InMemoryLedgerDirectory({"hq": ["hq-9"]}),
        local=PREFIX,
        options=ClusterOptions(**(cluster or {})),
        identify_options=IdentifyOptions(**(identify or {})),
    )


def plan(col, arts, baseline=()):
    return project(
        col,
        ev.corpus(*arts),
        baseline,
        prefix=PREFIX,
        group=GROUP,
        extracted_at=EXTRACTED,
        options=ProjectOptions(),
    )


def decision_of(col, unit):
    got = [v["class"] for v in col.verdicts if v["unit"] == unit and v["class"] is not None]
    return got[-1] if got else "residual"


# The 0.37.1 shape: a PR whose title shares a token with an existing bead, and two feat commits
# folded into it. The lexical step does not accept the weak match, so the merge goes residual.
KIDS = (("Add widget parser", "feat"), ("Fix widget edge", "feat"))
BASELINE = (bead("bh-1", "Widget renderer"),)


def residual_world():
    return merge_world(
        100, "Merge pull request #7 from o/feat/x", body_head="Parse widget", kids=KIDS
    )


# -- D5: hidden commits are always reported -----------------------------------------------


def test_a_residual_merge_reports_the_commits_it_folds() -> None:
    arts = residual_world()
    col = run(arts, BASELINE)
    assert decision_of(col, key(100)) == "residual"
    (record,) = hidden_commits(col.units, col.verdicts)
    assert record == {"unit": key(100), "decision": "residual", "commits": [key(101), key(102)]}
    assert hidden_summary([record]) == {"count": 2, "units": 1}
    report = plan(col, arts, BASELINE).to_json()
    assert report["hidden_commits"] == {"count": 2, "units": 1}
    assert report["counts"]["residual"] == 1  # the existing counts are untouched


def test_noise_and_operator_skip_hide_their_commits_too() -> None:
    arts = residual_world()
    first = run(arts, BASELINE)
    for kind, target in (("skip", None), ("noise", "bh-epic")):
        baseline = (
            *BASELINE,
            bead("bh-epic", "Rendering", type_="epic"),
            *decision_events(
                prefix=PREFIX,
                group=GROUP,
                key=key(100),
                kind=kind,
                target=target,
                decided_by="dev/x",
                labels=LABELS,
                decided_at="2026-11-13T12:00:00Z",
            ),
        )
        col = run(arts, baseline)
        (record,) = hidden_commits(col.units, col.verdicts)
        assert record["unit"] == key(100) and record["decision"] in ("SKIP", "NOISE")
        assert plan(col, arts, baseline).to_json()["hidden_commits"]["count"] == 2
    assert first.verdicts != col.verdicts


def test_nothing_hidden_means_no_report_key() -> None:
    # a merge nothing matches is NEW: its commits are attached to a created bead, not hidden
    arts = residual_world()
    col = run(arts, ())
    assert decision_of(col, key(100)) == "NEW"
    assert hidden_commits(col.units, col.verdicts) == ()
    assert "hidden_commits" not in plan(col, arts).to_json()
    assert plan(col, arts).hidden_commits() == {}


def test_a_branch_sync_hides_nothing_and_its_commits_are_released() -> None:
    arts = merge_world(100, "Merge branch 'main' into next", kids=KIDS)
    col = run(arts, BASELINE)
    sync = col.unit(key(100))
    assert sync.noise == "branch-sync" and sync.artifacts == (key(100),)
    assert decision_of(col, key(100)) == "SKIP"
    # its commits are commit units under the commit rule: never "hidden" by the sync
    assert {u.key for u in col.units if u.rule == "commit"} == {key(101), key(102)}
    assert hidden_commits(col.units, col.verdicts) == ()


def test_a_decision_skip_and_a_branch_sync_skip_are_told_apart() -> None:
    arts = merge_world(100, "Merge branch 'main' into next", kids=KIDS)
    col = run(arts, BASELINE)
    (sync,) = [v for v in col.verdicts if v["unit"] == key(100)]
    assert sync["step"] == "branch-sync"  # the producer; only step decision is an operator's


# -- D1: the opt-in fallback ---------------------------------------------------------------


def test_the_fallback_emits_the_commits_of_a_residual_merge_as_units() -> None:
    arts = residual_world()
    off = run(arts, BASELINE)
    on = run(arts, BASELINE, identify={"merge_fallback": "commits"})
    assert {u.key for u in off.units} == {key(100)}
    assert {u.key for u in on.units} == {key(101), key(102)}
    assert {u.rule for u in on.units} == {"commit"}
    assert hidden_commits(on.units, on.verdicts) == ()
    assert "hidden_commits" not in plan(on, arts, BASELINE).to_json()
    # the new units were identified in the same run
    assert {v["unit"] for v in on.verdicts} | {r["unit"] for r in on.residual} == {
        key(101),
        key(102),
    }
    assert on.options["identify"]["merge_fallback"] == "commits"


def _operator_baseline(kind, target):
    return (
        *BASELINE,
        bead("bh-epic", "Rendering", type_="epic"),
        *decision_events(
            prefix=PREFIX,
            group=GROUP,
            key=key(100),
            kind=kind,
            target=target,
            decided_by="dev/x",
            labels=LABELS,
            decided_at="2026-11-13T12:00:00Z",
        ),
    )


@pytest.mark.parametrize(("kind", "target"), [("skip", None), ("noise", "bh-epic")])
def test_the_fallback_never_releases_an_operator_decision(kind, target) -> None:
    arts = residual_world()
    baseline = _operator_baseline(kind, target)
    col = run(arts, baseline, identify={"merge_fallback": "commits"})
    assert key(100) in {u.key for u in col.units}  # the merge is kept
    assert not any(u.rule == "commit" for u in col.units)  # no commit units
    (verdict,) = [v for v in col.verdicts if v["unit"] == key(100)]
    assert verdict["step"] == "decision"
    (record,) = hidden_commits(col.units, col.verdicts)  # still reported (D5)
    assert record["commits"] == [key(101), key(102)]
    assert plan(col, arts, baseline).to_json()["hidden_commits"]["count"] == 2


def test_branch_sync_and_plain_residual_merges_are_still_released() -> None:
    arts = [
        *merge_world(100, "Merge branch 'main' into next", kids=(("Sync feature", "feat"),)),
        *merge_world(
            200, "Merge pull request #7 from o/x", body_head="Parse widget", kids=KIDS[:1]
        ),
    ]
    col = run(arts, BASELINE, identify={"merge_fallback": "commits"})
    units = {u.key: u for u in col.units}
    assert units[key(100)].noise == "branch-sync"  # the sync stays a SKIP unit
    assert key(200) not in units  # the residual merge was replaced...
    assert units[key(201)].rule == "commit" and units[key(101)].rule == "commit"
    assert hidden_commits(col.units, col.verdicts) == ()


def test_the_fallback_leaves_a_merge_that_identity_decided_alone() -> None:
    arts = residual_world()
    col = run(arts, (), identify={"merge_fallback": "commits"})  # NEW: nothing to fall back from
    assert [u.key for u in col.units] == [key(100)] and decision_of(col, key(100)) == "NEW"


def test_the_fallback_keeps_a_merge_none_of_whose_commits_is_admitted() -> None:
    arts = merge_world(
        100, "Merge pull request #7 from o/x", body_head="Parse widget", kids=(("Docs", "docs"),)
    )
    col = run(arts, BASELINE, identify={"merge_fallback": "commits"})
    assert key(100) in {u.key for u in col.units}  # still residual and still reported
    assert hidden_commits(col.units, col.verdicts)[0]["commits"] == [key(101)]
    admitted = run(
        arts, BASELINE, identify={"merge_fallback": "commits"}, cluster={"include_types": ["*"]}
    )
    assert key(100) not in {u.key for u in admitted.units}


def test_the_fallback_second_pass_exposes_inner_merges_as_units() -> None:
    root, main = 1, 2
    arts = [
        commit(root, cc_type="chore"),
        commit(main, [root], cc_type="chore"),
        commit(3, [root], title="Add widget outer"),
        commit(4, [3], title="Add widget inner"),
        commit(5, [3, 4], cc_type=None, title="Merge pull request #5 from o/inner"),
        commit(6, [main, 5], cc_type=None, title="Merge pull request #6 from o/outer"),
    ]
    col = run(arts, BASELINE, identify={"merge_fallback": "commits"})
    rules = {u.key: u.rule for u in col.units}
    assert key(6) not in rules  # the outer merge was replaced...
    assert rules[key(5)] == "merge"  # ...and the merge inside it is a first-parent merge now
    # a merge the second pass exposes is not released again: if it is held, its commits are reported
    held = {h["unit"] for h in hidden_commits(col.units, col.verdicts)}
    assert held <= {key(5)}
    assert (key(5) in held) == (decision_of(col, key(5)) == "residual")


def test_the_fallback_is_deterministic_and_round_trips() -> None:
    arts = [
        *residual_world(),
        *merge_world(200, "Merge pull request #8 from o/y", body_head="Widget x", kids=KIDS),
    ]
    a = run(arts, BASELINE, identify={"merge_fallback": "commits"})
    b = run(arts, BASELINE, identify={"merge_fallback": "commits", "merge_identity": "unit"})
    assert dumps_collation(a) == dumps_collation(b)
    assert loads_collation(dumps_collation(a)) == a


def test_a_second_run_after_the_fallback_proposes_nothing() -> None:
    arts = residual_world()
    opts = {"merge_fallback": "commits", "merge_identity": "commits", "new_threshold": 100}
    col = run(arts, BASELINE, identify=opts)
    first = plan(col, arts, BASELINE)
    assert first.events
    store = InMemoryStore()
    store.load(BASELINE, dry_run=False)
    store.load(tuple(reconcile(first.events, BASELINE).events), dry_run=False)
    applied = store.export()
    again = plan(run(arts, applied, identify=opts), arts, applied)
    assert again.events == ()


# -- D1: commit-based identity -------------------------------------------------------------

# the merge title matches "Render queue"; none of its commits does
IDENTITY_KIDS = (("Add parser", "feat"), ("Fix lexer", "feat"))


def identity_world():
    return merge_world(
        100, "Merge pull request #7 from o/feat/x", body_head="Render pipeline", kids=IDENTITY_KIDS
    )


IDENTITY_BASELINE = (bead("bh-1", "Render queue"),)


def test_a_merge_whose_commits_are_each_new_is_not_held_by_a_candidate() -> None:
    arts = identity_world()
    held = run(arts, IDENTITY_BASELINE)
    assert decision_of(held, key(100)) == "residual"
    col = run(arts, IDENTITY_BASELINE, identify={"merge_identity": "commits"})
    assert decision_of(col, key(100)) == "NEW"
    (verdict,) = [v for v in col.verdicts if v["unit"] == key(100)]
    assert (verdict["step"], verdict["provenance"], verdict["review"]) == (
        "no-candidate",
        "derived",
        "required",
    )
    assert verdict["confidence_permille"] is None
    assert verdict["commit_candidates"] == {
        "with_candidate": [],
        "without_candidate": [key(101), key(102)],
    }
    assert hidden_commits(col.units, col.verdicts) == ()


def test_a_mixed_merge_keeps_the_flow_and_reports_which_commits_matched() -> None:
    kids = (("Add parser", "feat"), ("Tune render cache", "feat"))
    arts = merge_world(
        100, "Merge pull request #7 from o/x", body_head="Render pipeline", kids=kids
    )
    col = run(arts, IDENTITY_BASELINE, identify={"merge_identity": "commits"})
    assert decision_of(col, key(100)) == "residual"  # the usual flow, unchanged
    (residual,) = col.residual
    assert residual["commit_candidates"] == {
        "with_candidate": [key(102)],
        "without_candidate": [key(101)],
    }


def test_all_matching_commits_keep_the_flow() -> None:
    kids = (("Render the queue", "feat"), ("Tune render cache", "feat"))
    arts = merge_world(
        100, "Merge pull request #7 from o/x", body_head="Render pipeline", kids=kids
    )
    col = run(arts, IDENTITY_BASELINE, identify={"merge_identity": "commits"})
    assert decision_of(col, key(100)) == "residual"
    assert col.residual[0]["commit_candidates"]["without_candidate"] == []


def test_a_merge_without_commits_of_its_own_is_unchanged() -> None:
    arts = identity_world()
    base = run(arts, IDENTITY_BASELINE, identify={"merge_identity": "unit"})
    # a unit with no commit artifacts gets no report and no NEW rule
    assert not any("commit_candidates" in r for r in (*base.verdicts, *base.residual))


def test_identity_is_off_by_default_and_the_default_bytes_are_unchanged() -> None:
    arts = identity_world()
    default = run(arts, IDENTITY_BASELINE)
    explicit = run(arts, IDENTITY_BASELINE, identify={"merge_identity": "unit"})
    assert dumps_collation(default) == dumps_collation(explicit)
    assert "commit_candidates" not in dumps_collation(default).decode()
    assert set(json.loads(dumps_collation(default))["options"]["identify"]) == {
        "new_threshold",
        "max_candidates",
    }


def test_identity_only_applies_to_merge_units() -> None:
    # a commit unit whose title matches a candidate is untouched by merge_identity
    arts = [commit(1, cc_type="chore"), commit(2, [1], title="Render the queue")]
    col = run(arts, IDENTITY_BASELINE, identify={"merge_identity": "commits"})
    assert not any("commit_candidates" in r for r in (*col.verdicts, *col.residual))


# -- D9: vocabulary ------------------------------------------------------------------------


def test_options_validate_and_serialize_only_when_set() -> None:
    assert IdentifyOptions().to_json() == {"new_threshold": None, "max_candidates": 5}
    opts = IdentifyOptions(merge_fallback="commits", merge_identity="commits")
    assert IdentifyOptions.from_json(opts.to_json()) == opts
    assert (
        IdentifyOptions.from_json({"new_threshold": None, "max_candidates": 5}) == IdentifyOptions()
    )
    assert IdentifyOptions.from_mapping({"merge_fallback": "commits"}).merge_fallback == "commits"
    for bad in ({"merge_fallback": "all"}, {"merge_identity": "pr"}):
        with pytest.raises(IdentifyOptionsError):
            IdentifyOptions(**bad)
    for raw in ({"new_threshold": None}, {"new_threshold": None, "max_candidates": 5, "x": 1}):
        with pytest.raises(IdentifyOptionsError):
            IdentifyOptions.from_json(raw)


def test_loader_reads_old_and_new_and_refuses_bad_members() -> None:
    arts = identity_world()
    col = run(arts, IDENTITY_BASELINE, identify={"merge_identity": "commits"})
    assert loads_collation(dumps_collation(col)) == col
    obj = json.loads(dumps_collation(col))

    def load(patched):
        return loads_collation(beadlog.canonical_json(patched).encode() + b"\n")

    broken = [
        {**v, "commit_candidates": {"with_candidate": []}} if v["unit"] == key(100) else v
        for v in obj["verdicts"]
    ]
    with pytest.raises(CollationFormatError):
        load({**obj, "verdicts": broken})
    opts = obj["options"]
    with pytest.raises(CollationFormatError):
        load({**obj, "options": {**opts, "identify": {**opts["identify"], "merge_identity": "x"}}})


def test_schema_accepts_the_new_members() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(collation_schema())
    for identify in ({}, {"merge_identity": "commits"}, {"merge_fallback": "commits"}):
        validator.validate(
            json.loads(dumps_collation(run(identity_world(), IDENTITY_BASELINE, identify=identify)))
        )
    mixed = merge_world(
        100,
        "Merge pull request #7 from o/x",
        body_head="Render pipeline",
        kids=(("Add parser", "feat"), ("Tune render cache", "feat")),
    )
    validator.validate(
        json.loads(
            dumps_collation(run(mixed, IDENTITY_BASELINE, identify={"merge_identity": "commits"}))
        )
    )


# -- R6: one bead per PR, its commits linked, nothing proposed twice -----------------------


def test_merge_heavy_run_yields_one_bead_per_pr_with_its_commits_linked() -> None:
    arts = [
        *merge_world(
            100,
            "Merge pull request #1 from o/a",
            body_head="First change",
            kids=(("One", "feat"), ("Two", "fix")),
        ),
        *merge_world(
            200,
            "Merge pull request #2 from o/b",
            body_head="Second change",
            kids=(("Three", "feat"),),
        ),
        *merge_world(300, "Merge branch 'main' into next", kids=(("Sync", "feat"),)),
    ]
    opts = {"merge_identity": "commits"}
    col = run(arts, (), identify=opts)
    first = plan(col, arts)
    created = [e for e in first.events if str(e.kind) == "create"]
    titles = {e.data["fields"]["title"] for e in created if e.data["type"] != "epic"}
    assert {"First change", "Second change"} <= titles
    assert not any(t.startswith("Merge pull request") for t in titles)
    links = {
        e.bead.id: json.loads(e.data["value"])
        for e in first.events
        if str(e.kind) == "set" and e.data.get("field") == "metadata.git.commits"
    }
    by_unit = {u.unit: u.bead for u in first.units}
    assert set(links[by_unit[key(100)]]) == {sha(100), sha(101), sha(102)}  # the merge and its two
    assert set(links[by_unit[key(200)]]) == {sha(200), sha(201)}
    assert "hidden_commits" not in first.to_json()
    store = InMemoryStore()
    store.load(tuple(reconcile(first.events, ()).events), dry_run=False)
    applied = store.export()
    again = plan(run(arts, applied, identify=opts), arts, applied)
    assert again.events == ()
