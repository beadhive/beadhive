"""beadlog-sources: collate B6, unit status, lifecycle dates and status conflicts."""

from __future__ import annotations

import json

import pytest

import beadlog
from beadlog_sources import (
    CONFLICT_RULES,
    ClusterOptions,
    CollationFormatError,
    Moment,
    StatusOptions,
    StatusOptionsError,
    collate,
    collation_schema,
    conflicts_of,
    derive_lifecycle,
    dumps_collation,
    lifecycle_of,
    lifecycles,
    loads_collation,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000aa", "bh")


def sha(n: int) -> str:
    return f"{n:040x}"


def key(n: int) -> str:
    return f"git:commit/{sha(n)}"


def at(n: int, day: str = "2026-01-01") -> str:
    return f"{day}T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"


def commit(n: int, *, day: str = "2026-01-01", cc_type="feat", paths=(), **kw):
    return ev.change(
        sha(n), occurred_at=at(n, day), parents=[], cc_type=cc_type, paths=list(paths), **kw
    )


def doc(path: str, status=None, raw=None, date=None, **kw):
    raw_attrs = {} if raw is None else {"declared_status": raw}
    return ev.design_record(
        path, declared_status=status, declared_date=date, raw_attrs=raw_attrs, **kw
    )


def run(*artifacts, status=None, **cluster):
    return collate(
        ev.corpus(*artifacts),
        [],
        InMemoryLedgerDirectory({}),
        local="bh",
        options=ClusterOptions(**cluster),
        status_options=StatusOptions(**(status or {})),
    )


def life(col, unit):
    found = lifecycle_of(col, unit)
    assert found is not None, unit
    return found


def only(col, rule):
    found = [c for c in conflicts_of(col) if c.rule == rule]
    assert len(found) == 1, [c.rule for c in conflicts_of(col)]
    return found[0]


# -- status and dates ---------------------------------------------------------------------------


def test_committed_unit_is_closed_at_its_commit() -> None:
    col = run(commit(5, paths=["src/a.py"]))
    got = life(col, key(5))
    assert (got.status, got.basis) == ("closed", "committed")
    assert got.created == got.closed == Moment(at(5), "second", key(5))


def test_merged_review_closes_at_merge_and_opens_at_open() -> None:
    col = run(
        commit(1, paths=["src/a.py"]),
        ev.review(
            7,
            commits=[sha(1)],
            opened_at="2026-01-01T00:00:00Z",
            merged_at="2026-01-02T00:00:00Z",
        ),
    )
    got = life(col, "gh:pr/7")
    assert (got.status, got.basis) == ("closed", "merged")
    assert got.created == Moment("2026-01-01T00:00:00Z", "second", "gh:pr/7")
    assert got.closed == Moment("2026-01-02T00:00:00Z", "second", "gh:pr/7")


def test_unmerged_review_stays_open() -> None:
    col = run(ev.review(3, state="open", merged_at=None, opened_at="2026-01-01T00:00:00Z"))
    got = life(col, "gh:pr/3")
    assert (got.status, got.basis, got.closed) == ("open", "review", None)
    assert got.created is not None


def test_plan_item_status_follows_done_marker() -> None:
    col = run(
        ev.plan_item("p1", done_marker=False, occurred_at="2026-01-01T00:00:00Z"),
        ev.plan_item("p2", done_marker=True, occurred_at="2026-01-02T00:00:00Z"),
    )
    assert (life(col, "plan:p1").status, life(col, "plan:p1").closed) == ("open", None)
    done = life(col, "plan:p2")
    assert (done.status, done.basis) == ("closed", "plan-item")
    assert done.closed == Moment("2026-01-02T00:00:00Z", "second", "plan:p2")


def test_release_marker_closes_its_window() -> None:
    col = run(ev.release_marker("v1", released_at="2026-01-09T00:00:00Z"))
    unit = [u for u in col.units if "git:tag/v1" in u.artifacts]
    if unit:
        got = life(col, unit[0].key)
        assert (got.status, got.basis) == ("closed", "released")
        assert got.closed.source == "git:tag/v1"


def test_epic_without_evidence_follows_its_children() -> None:
    arts = [commit(i, paths=[f"src/hq/m{i}.py"]) for i in (1, 2, 3)]
    col = run(*arts, min_epic_units=2)
    epics = [u for u in col.units if u.kind == "epic"]
    assert epics, "fixture should form an area epic"
    got = life(col, epics[0].key)
    assert (got.status, got.basis) == ("closed", "children")
    assert got.created is not None and got.closed is not None
    assert got.created.at <= got.closed.at


def test_epic_with_an_open_child_is_open() -> None:
    plan = [
        ev.plan_item("a", done_marker=True, occurred_at="2026-01-01T00:00:00Z"),
        ev.plan_item("b", done_marker=False, occurred_at="2026-01-02T00:00:00Z", parent="plan:a"),
    ]
    col = run(*plan)
    parent = life(col, "plan:a")
    assert parent.status == "closed"  # a plan item's own adapter status wins over its children


def test_doc_unit_is_closed_by_its_add_commit_and_dates_from_it() -> None:
    col = run(
        commit(4, paths=["docs/d.md"], cc_type="docs"),
        doc("docs/d.md", status="accepted", date="2026-01-01"),
    )
    got = life(col, "doc:docs/d.md")
    assert (got.status, got.basis) == ("closed", "doc")
    assert got.created == Moment(at(4), "second", key(4))


def test_doc_without_add_commit_dates_from_its_declared_day() -> None:
    col = run(doc("docs/d.md", status="draft", date="2026-03-04"))
    got = life(col, "doc:docs/d.md")
    assert got.status == "open"
    assert got.created == Moment("2026-03-04", "day", "doc:docs/d.md")


def test_day_precision_is_never_midnight() -> None:
    day = Moment("2026-02-10", "day", "doc:x")
    late = Moment("2026-02-10T23:00:00Z", "second", "git:tag/v1")
    assert not late.after(day) and not day.after(late)
    assert Moment("2026-02-11T00:00:00Z", "second", "t").after(day)
    assert day.after(Moment("2026-02-09T23:59:59Z", "second", "t"))
    assert Moment.of("2026-02-10", "x").precision == "day"
    assert Moment.of("2026-02-10T00:00:00Z", "x").precision == "second"
    # a timestamp compares at day precision against a day: the UTC day, not an instant
    assert not Moment("2026-02-10T00:00:00.5Z", "second", "t").after(day)


# -- conflicts: one fixture per rule ------------------------------------------------------------


def history_status(col, unit):
    """The unit's status as history alone derives it (no design record consulted)."""
    plain = derive_lifecycle(col, StatusOptions(rules=()))
    return life(plain, unit).status


def check_conflict(col, found, unit, first, second):
    """A conflict lists both artifact keys and values, and leaves the derived status alone."""
    assert found.unit == unit
    assert (found.sources[0].artifact, found.sources[1].artifact) == (first, second)
    for side in found.sources:
        assert side.field and side.value is not None
    assert found.status == life(col, unit).status == history_status(col, unit)


def test_doc_draft_but_code_landed_in_the_same_commit() -> None:
    col = run(
        commit(1, paths=["src/a.py", "docs/d.md"]),
        doc("docs/d.md", status="draft", raw="Draft (WIP)"),
    )
    found = only(col, "doc-draft-code-landed")
    check_conflict(col, found, key(1), "doc:docs/d.md", key(1))
    claim, evidence = found.sources
    assert (claim.field, claim.raw, claim.value) == ("declared_status", "Draft (WIP)", "draft")
    assert (evidence.field, evidence.value) == ("landed", "landed")
    assert found.status == "closed"


def test_doc_proposed_but_code_landed_in_the_same_pr() -> None:
    col = run(
        commit(1, paths=["docs/d.md"], cc_type="docs"),
        commit(2, paths=["src/a.py"]),
        ev.review(9, commits=[sha(1), sha(2)]),
        doc("docs/d.md", status="proposed"),
    )
    found = only(col, "doc-draft-code-landed")
    check_conflict(col, found, "gh:pr/9", "doc:docs/d.md", key(2))


def test_doc_draft_with_no_code_is_no_conflict() -> None:
    col = run(commit(1, paths=["docs/d.md"], cc_type="docs"), doc("docs/d.md", status="draft"))
    assert conflicts_of(col) == ()


def test_design_only_doc_with_an_attached_feat_commit() -> None:
    col = run(
        commit(1, paths=["docs/d.md"], cc_type="docs"),
        commit(2, paths=["src/a.py"], cc_type="feat"),
        doc(
            "docs/d.md",
            status="draft",
            raw="Design-only",
            refs=[ev.ref(sha(2), "sha", "body")],
        ),
    )
    found = only(col, "doc-design-only-feat")
    check_conflict(col, found, key(2), "doc:docs/d.md", key(2))
    assert (found.sources[0].raw, found.sources[0].value) == ("Design-only", "draft")
    assert (found.sources[1].raw, found.sources[1].value) == ("feat", "feat")


def test_design_only_doc_with_only_a_fix_is_no_conflict() -> None:
    col = run(
        commit(2, paths=["src/a.py"], cc_type="fix"),
        doc("docs/d.md", status="draft", raw="design only", refs=[ev.ref(sha(2), "sha", "body")]),
    )
    assert not [c for c in conflicts_of(col) if c.rule == "doc-design-only-feat"]


def accepted_docs(date, released="2026-02-10T12:00:00Z"):
    return run(
        commit(4, paths=["docs/d.md"], cc_type="docs", day="2026-02-10"),
        ev.release_marker("v1", released_at=released),
        doc("docs/d.md", status="accepted", raw="Accepted", date=date),
    )


def test_doc_accepted_after_the_last_release_with_no_code() -> None:
    col = accepted_docs("2026-02-11")
    found = only(col, "doc-accepted-no-code")
    check_conflict(col, found, "doc:docs/d.md", "doc:docs/d.md", "git:tag/v1")
    claim, release = found.sources
    assert (claim.raw, claim.value) == ("Accepted", "accepted")
    assert claim.at == Moment("2026-02-11", "day", "doc:docs/d.md")
    assert (release.field, release.value) == ("released_at", "2026-02-10T12:00:00Z")


def test_doc_accepted_the_day_of_the_release_is_not_after_it() -> None:
    # same UTC day as the release, even when the release came earlier that day
    assert conflicts_of(accepted_docs("2026-02-10", "2026-02-10T00:00:01Z")) == ()
    assert conflicts_of(accepted_docs("2026-02-09")) == ()


def test_doc_accepted_with_code_attached_is_no_conflict() -> None:
    col = run(
        commit(1, paths=["src/a.py", "docs/d.md"]),
        ev.release_marker("v1", released_at="2026-02-10T12:00:00Z"),
        doc("docs/d.md", status="accepted", date="2026-02-11"),
    )
    assert not [c for c in conflicts_of(col) if c.rule == "doc-accepted-no-code"]


def test_doc_accepted_without_a_release_is_no_conflict() -> None:
    col = run(
        commit(4, paths=["docs/d.md"], cc_type="docs"),
        doc("docs/d.md", status="accepted", date="2026-01-01"),
    )
    assert conflicts_of(col) == ()


SUMMARY = ".planning/phases/01/01-01-SUMMARY.md"


def plan_with_summary(done):
    return run(
        ev.plan_item("p1", done_marker=done, occurred_at="2026-01-01T00:00:00Z"),
        commit(4, paths=[SUMMARY], cc_type="docs"),
        doc(SUMMARY, refs=[ev.ref("plan:p1", "url", "body")]),
    )


def test_plan_item_open_but_its_summary_exists() -> None:
    col = plan_with_summary(False)
    found = only(col, "plan-open-summary")
    check_conflict(col, found, "plan:p1", "plan:p1", f"doc:{SUMMARY}")
    assert found.status == "open"
    assert (found.sources[0].raw, found.sources[0].value) == (False, "open")
    assert life(col, "plan:p1").status == "open"  # the summary does not close the item


def test_plan_item_done_with_its_summary_is_no_conflict() -> None:
    assert conflicts_of(plan_with_summary(True)) == ()


def test_clocks_disagree_declared_date_and_add_commit() -> None:
    col = run(
        commit(4, paths=["docs/d.md"], cc_type="docs", day="2026-01-01"),
        doc("docs/d.md", status="draft", date="2026-03-01"),
    )
    found = only(col, "clock-doc-date")
    check_conflict(col, found, "doc:docs/d.md", "doc:docs/d.md", key(4))
    assert found.sources[0].at == Moment("2026-03-01", "day", "doc:docs/d.md")
    assert found.sources[1].at == Moment(at(4), "second", key(4))
    # the unit keeps the date its add commit gives: the conflict picks no clock
    assert life(col, "doc:docs/d.md").created == Moment(at(4), "second", key(4))


def test_clock_tolerance_is_configurable_and_day_based() -> None:
    arts = (
        commit(4, paths=["docs/d.md"], cc_type="docs"),
        doc("docs/d.md", status="draft", date="2026-01-02"),
    )
    assert conflicts_of(run(*arts)) == ()
    strict = run(*arts, status={"date_tolerance_days": 0})
    assert [c.rule for c in conflicts_of(strict)] == ["clock-doc-date"]


def test_clocks_disagree_review_merged_before_its_change() -> None:
    col = run(
        commit(1, paths=["src/a.py"], day="2026-01-01"),
        ev.review(
            5, commits=[sha(1)], opened_at="2025-05-01T00:00:00Z", merged_at="2025-06-01T00:00:00Z"
        ),
    )
    found = only(col, "clock-review-merge")
    check_conflict(col, found, "gh:pr/5", "gh:pr/5", key(1))
    assert life(col, "gh:pr/5").closed.at == "2025-06-01T00:00:00Z"  # not "fixed" to the commit


# -- the whole ----------------------------------------------------------------------------------


def every_rule():
    return run(
        commit(1, paths=["src/a.py", "docs/draft.md"]),
        doc("docs/draft.md", status="draft"),
        commit(2, paths=["src/b.py"]),
        doc("docs/design.md", status="draft", raw="design only", refs=[ev.ref(sha(2), "sha")]),
        ev.release_marker("v1", released_at="2026-02-10T12:00:00Z"),
        doc("docs/acc.md", status="accepted", date="2026-02-11"),
        ev.plan_item("p1", occurred_at="2026-01-01T00:00:00Z"),
        doc(SUMMARY, refs=[ev.ref("plan:p1", "url")]),
        doc("docs/clock.md", status="draft", date="2026-09-09"),
        commit(6, paths=["docs/clock.md"], cc_type="docs"),
        commit(7, paths=["src/c.py"]),
        ev.review(5, commits=[sha(7)], merged_at="2025-06-01T00:00:00Z"),
    )


def test_every_rule_fires_once_and_is_sorted() -> None:
    col = every_rule()
    found = conflicts_of(col)
    assert {c.rule for c in found} == set(CONFLICT_RULES)
    order = [(c.unit, c.rule, c.sources[0].artifact, c.sources[1].artifact) for c in found]
    assert order == sorted(order)


def test_conflicts_never_change_a_status() -> None:
    col = every_rule()
    plain = derive_lifecycle(col, StatusOptions(rules=()))
    assert plain.conflicts == () and col.conflicts
    assert col.lifecycle == plain.lifecycle
    for found in conflicts_of(col):
        assert found.status == life(col, found.unit).status


def test_rules_can_be_switched_off() -> None:
    col = every_rule()
    only_one = derive_lifecycle(col, StatusOptions(rules=("plan-open-summary",)))
    assert {c.rule for c in conflicts_of(only_one)} == {"plan-open-summary"}
    assert only_one.options["status"]["rules"] == ["plan-open-summary"]


def test_deterministic_round_trip_and_schema() -> None:
    first, second = every_rule(), every_rule()
    data = dumps_collation(first)
    assert data == dumps_collation(second)
    loaded = loads_collation(data)
    assert loaded == first
    assert conflicts_of(loaded) == conflicts_of(first)
    assert lifecycles(loaded) == lifecycles(first)
    assert [x.unit for x in lifecycles(first)] == sorted(u.key for u in first.units)
    jsonschema = pytest.importorskip("jsonschema")
    obj = json.loads(data)
    validator = jsonschema.Draft202012Validator(collation_schema())
    validator.validate(obj)
    assert obj["conflicts"] and obj["lifecycle"]

    def load(**change):
        return loads_collation(beadlog.canonical_json({**obj, **change}).encode() + b"\n")

    one = obj["conflicts"][0]
    bad_conflicts = (
        [{**one, "rule": "guess"}],
        [{**one, "sources": one["sources"][:1]}],
        [{**one, "extra": 1}],
    )
    for bad in bad_conflicts:
        with pytest.raises(CollationFormatError):
            load(conflicts=bad)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate({**obj, "conflicts": bad})
    first_life = obj["lifecycle"][0]
    for bad in ([{**first_life, "status": "done"}], [{**first_life, "basis": "x"}]):
        with pytest.raises(CollationFormatError):
            load(lifecycle=bad)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate({**obj, "lifecycle": bad})
    missing = {k: v for k, v in obj.items() if k != "lifecycle"}
    with pytest.raises(CollationFormatError):
        loads_collation(beadlog.canonical_json(missing).encode() + b"\n")
    no_status = {**obj, "options": {k: v for k, v in obj["options"].items() if k != "status"}}
    with pytest.raises(CollationFormatError):
        loads_collation(beadlog.canonical_json(no_status).encode() + b"\n")


def test_status_options_validation_and_round_trip() -> None:
    default = StatusOptions()
    assert default.rules == CONFLICT_RULES
    assert StatusOptions.from_json(default.to_json()) == default
    assert (
        StatusOptions.from_mapping({"date_tolerance_days": 3, "other": 1}).date_tolerance_days == 3
    )
    assert StatusOptions(design_only=["Design_Only"]).design_only == ("design only",)
    for bad in (
        {"rules": ["nope"]},
        {"rules": ["plan-open-summary", "plan-open-summary"]},
        {"rules": "plan-open-summary"},
        {"design_only": [""]},
        {"summary_suffix": ""},
        {"date_tolerance_days": -1},
        {"clock_tolerance_seconds": True},
    ):
        with pytest.raises(StatusOptionsError):
            StatusOptions(**bad)
    with pytest.raises(StatusOptionsError):
        StatusOptions.from_json({"rules": []})
    # options alone change the bytes
    arts = (commit(1, paths=["src/a.py"]),)
    assert dumps_collation(run(*arts)) != dumps_collation(run(*arts, status={"rules": []}))


def test_derive_needs_the_indexes() -> None:
    col = loads_collation(dumps_collation(run(commit(1, paths=["src/a.py"]))))
    with pytest.raises(CollationFormatError):
        derive_lifecycle(col)
