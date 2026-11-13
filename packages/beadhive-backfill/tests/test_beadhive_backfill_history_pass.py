"""The ``history`` pass in create mode through the engine and a fold store.

A commit-only fixture hive (an evidence corpus of conventional commits, no docs and no tracker):
create mode proposes area epics and one closed issue per ``feat`` commit as NEW, apply creates
them closed with the creation labels, and the second run is PRESENT only. Link-only mode
(``create=False``) proposes nothing for them, and an empty hive is allowed only in create mode.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

import beadlog
from beadhive_backfill import BackfillEngine, BackfillError, HistoryPass, Plan
from beadlog_sources import CachedLedgerDirectory, ProjectOptions, baseline_events
from beadlog_sources.testing import evidence as ev

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000ee", "wd")
STAMP = "2026-11-12T00:00:00Z"
LABELS = ("origin:backfill", "provider:github", "org:beadhive", "repo:fixture")


def _clock() -> str:
    return STAMP


def sha(n: int) -> str:
    return f"{n:040x}"


def commit(n: int, day: int, cc_type: str, path: str) -> Any:
    when = f"2026-10-{day:02d}T10:00:00Z"
    return ev.change(
        sha(n),
        occurred_at=when,
        committed_at=when,
        cc_type=cc_type,
        paths=[path],
        title=f"{cc_type}: change {n}",
    )


def corpus() -> Any:
    return ev.corpus(
        commit(1, 1, "feat", "api/a.py"),
        commit(2, 2, "feat", "api/b.py"),
        commit(3, 3, "feat", "cli/c.py"),
        commit(4, 4, "feat", "cli/d.py"),
        commit(5, 5, "fix", "api/a.py"),
        commit(6, 6, "docs", "cli/c.py"),
    )


class FoldStore:
    name = "fake"
    version = "0"
    capabilities = frozenset({"valid-time"})

    def __init__(self, rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self.events = list(baseline_events(rows, group=GROUP.id, extracted_at=STAMP))
        self.exported = False

    def export(self) -> tuple[beadlog.BeadEvent, ...]:
        return tuple(self.events)

    def export_pre_state(self, baseline: Sequence[beadlog.BeadEvent]) -> str:
        self.exported = True
        return "pre-state"

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        if not dry_run:
            self.events.extend(events)
        return {"summary": {"by_code": {}}, "losses": []}


class ListSink:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def record(self, entry: Any) -> None:
        self.entries.append(entry)


def make_pass(*, create: bool, prefix: str | None = "fx") -> HistoryPass:
    return HistoryPass(
        corpus(),
        CachedLedgerDirectory({}),
        local="fx",
        group=GROUP.id,
        prefix=prefix,
        create=create,
        project_options=ProjectOptions(creation_labels=LABELS),
    )


def engine(pass_: HistoryPass) -> BackfillEngine:
    return BackfillEngine(groups=(GROUP,), clock=_clock, passes=(pass_,))


def by_class(plan: Plan) -> dict[str, int]:
    return {k: v for k, v in plan.totals["by_class"].items() if v}


def decisions(plan: Plan) -> dict[str, int]:
    """The projector's per-unit decisions the pass recorded in the plan report."""
    return {k: v for k, v in plan.report["units"].items() if v}


def _row(iid: str) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [],
        "created_at": "2026-08-10T09:00:00Z",
        "updated_at": "2026-08-20T00:00:00Z",
    }


def test_create_proposes_epics_and_feat_issues_as_new_and_applies_them_closed() -> None:
    store = FoldStore([_row("fx-seed")])
    pass_ = make_pass(create=True)
    eng = engine(pass_)
    plan = eng.plan(pass_, store)
    assert decisions(plan) == {"NEW": 6}
    assert by_class(plan) == {"NEW": 6}  # two area epics + four feat issues; fix/docs skipped
    entry = eng.apply(plan, store, store, ListSink(), pre_state=store)
    assert entry.state.value == "applied"

    states = {ref.id: s for ref, s in beadlog.fold(store.export()).items() if ref.id != "fx-seed"}
    assert sorted(s.type for s in states.values()) == ["epic"] * 2 + ["feature"] * 4
    for state in states.values():
        assert set(LABELS) <= set(state.labels)
        assert any(label.startswith("source:") for label in state.labels)
    assert {s.phase for s in states.values() if s.type == "feature"} == {"closed"}
    assert all(ref.startswith("fx-") for ref in states)


def test_the_second_run_is_present_only() -> None:
    store = FoldStore([_row("fx-seed")])
    pass_ = make_pass(create=True)
    eng = engine(pass_)
    eng.apply(eng.plan(pass_, store), store, store, ListSink(), pre_state=store)
    again = eng.plan(make_pass(create=True), store)
    assert decisions(again) == {"PRESENT": 6}
    assert by_class(again) == {} and not again.applicable_events()


def test_link_only_proposes_nothing_for_unmatched_units() -> None:
    store = FoldStore([_row("fx-seed")])
    pass_ = make_pass(create=False, prefix=None)
    plan = engine(pass_).plan(pass_, store)
    assert by_class(plan) == {}
    assert plan.report["creates_skipped"] == 6


def test_link_only_matches_create_mode_on_beads_that_already_exist() -> None:
    store = FoldStore([_row("fx-seed")])
    created = make_pass(create=True)
    eng = engine(created)
    eng.apply(eng.plan(created, store), store, store, ListSink(), pre_state=store)
    off = eng.plan(make_pass(create=False, prefix=None), store)
    on = eng.plan(make_pass(create=True), store)
    assert off.totals["by_class"] == on.totals["by_class"]
    assert off.applicable_events() == on.applicable_events() == ()


def test_off_with_a_history_ignores_creation_labels_and_never_writes() -> None:
    store = FoldStore([_row("fx-seed")])
    before = store.export()
    pass_ = make_pass(create=False, prefix=None)
    eng = engine(pass_)
    plan = eng.plan(pass_, store)
    assert store.export() == before and not plan.applicable_events()


def test_an_empty_hive_is_refused_without_create() -> None:
    pass_ = make_pass(create=False, prefix="fx")
    with pytest.raises(BackfillError, match="empty hive"):
        engine(pass_).plan(pass_, FoldStore())


def test_an_empty_hive_is_allowed_in_create_mode_with_an_explicit_prefix() -> None:
    store = FoldStore()
    pass_ = make_pass(create=True)
    eng = engine(pass_)
    plan = eng.plan(pass_, store)
    assert by_class(plan) == {"NEW": 6}
    eng.apply(plan, store, store, ListSink(), pre_state=store)
    assert len(beadlog.fold(store.export())) == 6
    assert decisions(eng.plan(make_pass(create=True), store)) == {"PRESENT": 6}


def test_create_mode_needs_an_explicit_prefix() -> None:
    for prefix in (None, ""):
        with pytest.raises(BackfillError, match="prefix"):
            make_pass(create=True, prefix=prefix)


def test_create_mode_needs_no_new_threshold_and_off_still_writes_nothing() -> None:
    # No identify options at all: no threshold, no judge. The units have no baseline candidate
    # (the only bead is unrelated), so B5 decides them NEW by rule.
    store = FoldStore([_row("fx-seed")])
    on = make_pass(create=True)
    plan = engine(on).plan(on, store)
    assert decisions(plan) == {"NEW": 6}
    creates = [e for e in plan.events if e.kind == "create"]
    assert len(creates) == 6
    for event in creates:
        verdict = event.to_json()["annotations"]["bdlg.collate"]["verdict"]
        assert verdict["step"] == "no-candidate" and verdict["class"] == "NEW"
        assert event.provenance == "derived" and event.confidence is None
    off = make_pass(create=False, prefix=None)
    assert not engine(off).plan(off, store).applicable_events()


# -- a failed create-mode apply (bh-12hka, #152) ----------------------------------------------


class FailingFoldStore(FoldStore):
    """A fold store that raises on the nth non-inverse write (every later write works)."""

    def __init__(self, rows: Sequence[Mapping[str, Any]] = (), *, fail_at: int = 0) -> None:
        super().__init__(rows)
        self.fail_at = fail_at
        self.seen = 0

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        if not dry_run and self.fail_at and events[0].source.system != "beadhive-backfill":
            self.seen += 1
            if self.seen == self.fail_at:
                raise RuntimeError("injected mid-apply failure")
        return super().load(events, dry_run=dry_run)


def _rolled_back_store() -> tuple[FailingFoldStore, Any, ListSink]:
    store = FailingFoldStore([_row("fx-seed")], fail_at=4)
    pass_ = make_pass(create=True)
    eng = engine(pass_)
    plan = eng.plan(pass_, store)
    sink = ListSink()
    with pytest.raises(BackfillError, match="apply failed and was audited"):
        eng.apply(plan, store, store, sink, pre_state=store)
    return store, eng, sink


def _created(store: FoldStore) -> dict[str, beadlog.BeadState]:
    return {ref.id: s for ref, s in beadlog.fold(store.export()).items() if ref.id != "fx-seed"}


def test_a_rolled_back_apply_retires_every_created_bead_and_the_audit_names_them() -> None:
    store, _, sink = _rolled_back_store()
    created = _created(store)
    assert created, "the failure happened after some beads were created"
    assert {s.phase for s in created.values()} == {"retired"}  # none left open

    final = sink.entries[-1]
    assert final.state.value == "rolled_back"
    assert set(final.rollback_retired) == set(created)
    body = final.to_json()
    assert body["rollback_retired_count"] == len(created)
    assert sorted(body["rollback_retired"]) == sorted(created)
    assert "retired" in body["recovery"]["instruction"]


def test_a_rerun_after_a_rollback_revives_the_retired_beads_without_duplicates() -> None:
    store, _, _ = _rolled_back_store()
    retired = _created(store)
    assert {s.phase for s in retired.values()} == {"retired"}

    pass_ = make_pass(create=True)
    eng = engine(pass_)
    plan = eng.plan(pass_, store)
    assert plan.applicable_events(), "a retired leftover must not read as already present"
    entry = eng.apply(plan, store, store, ListSink(), pre_state=store)
    assert entry.state.value == "applied"

    states = {ref.id: s for ref, s in beadlog.fold(store.export()).items() if ref.id != "fx-seed"}
    assert set(retired) <= set(states) and len(states) == 6  # revived in place, none re-minted
    assert "retired" not in {s.phase for s in states.values()}
    assert {s.phase for s in states.values() if s.type == "feature"} == {"closed"}
    for state in states.values():
        assert set(LABELS) <= set(state.labels)
    refs = [s.fields.get("external_ref") for s in states.values()]
    assert len(refs) == len(set(refs)), "no duplicate external_ref"
    assert decisions(eng.plan(make_pass(create=True), store)) == {"PRESENT": 6}
