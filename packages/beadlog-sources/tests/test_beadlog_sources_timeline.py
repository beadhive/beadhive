"""beadlog-sources: the timeline a re-run appends (bh-1vkff.3).

Backfill writes a timeline, not a snapshot: created -> in_progress -> closed, each transition at
its own evidence's moment. These tests cover the re-run close, the in-progress transition on
creation and on a re-run, the guard (only an open or own-in-progress ``origin:backfill`` bead),
idempotency, the two settings and the reconcile class. Every clock is a fixed timestamp.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import beadlog
from beadlog.testing import InMemoryStore
from beadlog_sources import (
    ClusterOptions,
    Collation,
    IdentifyOptions,
    Lifecycle,
    Projection,
    ProjectOptions,
    StatusOptions,
    Unit,
    collate,
    decision_events,
    derive_lifecycle,
    dumps_collation,
    identify,
    lifecycle_of,
    loads_collation,
    project,
    reconcile,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000aa"
EXTRACTED = "2026-11-12T00:00:00Z"
DOC = "docs/design/queue.md"
DOC_KEY = f"doc:{DOC}"
OPTIONS = ProjectOptions(creation_labels=("origin:backfill",))

ADDED = "2026-01-02T10:00:00Z"  # the doc's add commit
OPENED = "2026-01-05T09:00:00Z"  # the review opens
MERGED = "2026-01-09T12:30:45Z"  # and merges


def sha(n: int) -> str:
    return f"{n:040x}"


def doc_commit():
    return ev.change(
        sha(1), occurred_at=ADDED, committed_at=ADDED, parents=[], cc_type="docs", paths=[DOC]
    )


def doc(status: str | None = "draft", *, cites_review: bool = False):
    refs = [ev.ref("#5", "pr", "body")] if cites_review else []
    return ev.design_record(
        DOC, declared_status=status, occurred_at="2026-01-01T00:00:01Z", refs=refs
    )


def review(state: str, *, merged: str | None = None, commits: Sequence[str] = ()):
    return ev.review(
        5,
        state=state,
        opened_at=OPENED,
        merged_at=merged,
        commits=list(commits),
        occurred_at=merged or OPENED,
    )


def step(
    artifacts: Sequence[Any],
    baseline: Sequence[beadlog.BeadEvent] = (),
    status: StatusOptions | None = None,
    claims: Sequence[Unit] = (),
) -> tuple[Collation, Projection]:
    """One run. ``claims`` are units the clusterer never forms on its own (a unit that holds a
    tracker item or a release marker), added before B5 and B6 are derived again."""
    corpus = ev.corpus(*artifacts)
    identify_options = IdentifyOptions(new_threshold=100)
    col = collate(
        corpus,
        baseline,
        InMemoryLedgerDirectory({}),
        local="bh",
        options=ClusterOptions(),
        identify_options=identify_options,
        status_options=status,
    )
    if claims:
        col = replace(col, units=(*col.units, *claims))
        col = derive_lifecycle(identify(col, baseline, identify_options), status)
    out = project(
        col,
        corpus,
        baseline,
        prefix="bh",
        group=GROUP,
        extracted_at=EXTRACTED,
        options=OPTIONS,
    )
    return col, out


def applied(baseline: Sequence[beadlog.BeadEvent], events: Sequence[beadlog.BeadEvent]):
    store = InMemoryStore()
    store.load(tuple(baseline), dry_run=False)
    store.load(tuple(events), dry_run=False)
    return store.export()


def state_of(events: Sequence[beadlog.BeadEvent], projection: Projection, unit: str):
    record = projection.unit(unit)
    assert record is not None and record.bead is not None
    return beadlog.fold(events)[beadlog.BeadRef(GROUP, record.bead)]


def phases(events: Sequence[beadlog.BeadEvent]) -> list[tuple[str, str | None]]:
    """``(phase, occurred_at)`` of the phase events, in time order."""
    found = [(str(e.data["phase"]), e.occurred_at) for e in events if str(e.kind) == "phase"]
    return sorted(found, key=lambda pair: beadlog.instant(pair[1]) if pair[1] else 0)


def hand(kind: str, ident: str, data: dict[str, Any], at: str) -> beadlog.BeadEvent:
    return beadlog.mint_event(
        kind,
        beadlog.BeadRef(GROUP, ident),
        data,
        occurred_at=at,
        recorded_at=at,
        extracted_at=EXTRACTED,
        source=("bd", f"issues/{ident}#{kind}"),
        provenance="observed",
    )


def seeded(*, label: bool = True, ref: str = "gh:pr/5", **fields: Any) -> list[beadlog.BeadEvent]:
    """A baseline bead ``bh-seed`` whose ``external_ref`` is the unit ``ref``, as a prior run
    (``label``) or a person (no label) would have left it, open."""
    events = [
        hand(
            "create",
            "bh-seed",
            {"type": "task", "fields": {"title": "Queue", "external_ref": ref, **fields}},
            "2026-01-01T00:00:00Z",
        )
    ]
    if label:
        events.append(hand("label_add", "bh-seed", {"label": "origin:backfill"}, ADDED))
    return events


# -- the timeline --------------------------------------------------------------------------------


def test_a_draft_doc_moves_to_in_progress_then_closes_at_the_merge() -> None:
    # run 1: a draft doc, an add commit: an open bead, no close event
    _, first = step([doc_commit(), doc("draft")])
    log = applied((), first.events)
    assert state_of(log, first, DOC_KEY).phase == "open"
    assert phases(first.events) == []

    # run 2: the review the doc cites is open: in_progress at the review's own moment
    arts = [doc_commit(), doc("draft", cites_review=True), review("open")]
    _, second = step(arts, log)
    plan = reconcile(second.events, log)
    assert {str(e.cls) for e in plan.entries} == {"DRIFT"}  # the phase facet drifts, never NEW
    assert phases(second.events) == [("in_progress", OPENED)]
    log = applied(log, plan.events)
    state = state_of(log, second, "gh:pr/5")
    assert state.phase == "in_progress"
    assert state.fields["metadata.lifecycle.in_progress_at"] == OPENED
    assert second.unit("gh:pr/5").bead == first.unit(DOC_KEY).bead  # the same bead, no second one

    # run 3: the review merges: closed at the merge's time, precision kept
    arts = [doc_commit(), doc("draft", cites_review=True), review("merged", merged=MERGED)]
    _, third = step(arts, log)
    assert phases(third.events) == [("closed", MERGED)]
    log = applied(log, reconcile(third.events, log).events)
    state = state_of(log, third, "gh:pr/5")
    assert state.phase == "closed"
    assert state.fields["metadata.lifecycle.closed_at"] == MERGED  # not the run's time

    # the bead's whole history reads open -> in_progress -> closed, in order
    mine = [e for e in log if e.bead == state.bead]
    assert phases(mine) == [("in_progress", OPENED), ("closed", MERGED)]

    # run 4 and the engine's second dry run: nothing to do
    _, fourth = step(arts, log)
    assert fourth.events == ()
    assert reconcile(fourth.events, log).empty


def test_running_the_same_evidence_twice_appends_each_transition_once() -> None:
    _, first = step([doc_commit(), doc("draft")])
    log = applied((), first.events)
    arts = [doc_commit(), doc("draft", cites_review=True), review("open")]
    _, second = step(arts, log)
    log = applied(log, second.events)
    _, again = step(arts, log)
    assert again.events == ()
    assert len([e for e in log if str(e.kind) == "phase"]) == 1


def test_a_new_closed_unit_reads_created_in_progress_closed() -> None:
    # a commit, then the PR that carries it opens, then merges: each at its own time
    commit = ev.change(
        sha(2), occurred_at=ADDED, committed_at=ADDED, parents=[], cc_type="feat", paths=["a.py"]
    )
    arts = [commit, review("merged", merged=MERGED, commits=[sha(2)])]
    col, first = step(arts)
    life = lifecycle_of(col, "gh:pr/5")
    assert life is not None and life.started is not None and life.started.at == OPENED
    log = applied((), first.events)
    state = state_of(log, first, "gh:pr/5")
    creates = [e for e in log if str(e.kind) == "create"]
    assert [e.occurred_at for e in creates] == [ADDED]
    assert phases(log) == [("in_progress", OPENED), ("closed", MERGED)]
    assert state.phase == "closed"
    assert state.fields["metadata.lifecycle.in_progress_at"] == OPENED
    assert state.fields["metadata.lifecycle.closed_at"] == MERGED


def test_a_new_unit_with_an_open_review_is_created_in_progress() -> None:
    _, first = step([review("open")])
    log = applied((), first.events)
    assert state_of(log, first, "gh:pr/5").phase == "in_progress"
    # the create and the transition are one instant: the transition is raised past the create
    (create,) = [e for e in first.events if str(e.kind) == "create"]
    (move,) = [e for e in first.events if str(e.kind) == "phase"]
    assert create.occurred_at == OPENED
    assert beadlog.instant(move.occurred_at) > beadlog.instant(create.occurred_at)
    assert move.occurred_precision is None
    assert "phase-raised" in first.unit("gh:pr/5").notes


# -- the closing evidence ------------------------------------------------------------------------


def holds(*keys: str) -> Unit:
    return Unit(keys[0], "issue", "commit", "Queue", artifacts=keys)


def test_a_closed_tracker_item_closes_at_its_closed_time() -> None:
    claim = holds("gh:issue/7")
    opened = ev.tracker_item("7", opened_at=ADDED, occurred_at=ADDED)
    _, first = step([opened], claims=[claim])
    log = applied((), first.events)
    assert state_of(log, first, "gh:issue/7").phase == "open"
    closed = ev.tracker_item(
        "7", state="closed", opened_at=ADDED, closed_at=MERGED, occurred_at=ADDED
    )
    _, second = step([closed], log, claims=[claim])
    assert phases(second.events) == [("closed", MERGED)]
    log = applied(log, second.events)
    assert state_of(log, second, "gh:issue/7").fields["metadata.lifecycle.closed_at"] == MERGED
    assert step([closed], log, claims=[claim])[1].events == ()


def test_a_plan_done_marker_closes_at_the_plan_moment() -> None:
    _, first = step([ev.plan_item("p1", occurred_at=ADDED)])
    log = applied((), first.events)
    assert state_of(log, first, "plan:p1").phase == "open"
    done = ev.plan_item("p1", occurred_at=MERGED, done_marker=True)
    _, second = step([done], log)
    assert phases(second.events) == [("closed", MERGED)]
    assert step([done], applied(log, second.events))[1].events == ()


def test_a_release_closes_a_backfilled_bead_at_the_release_time() -> None:
    marker = ev.release_marker("v1.0.0", released_at=MERGED, occurred_at=MERGED)
    key = marker.key
    base = seeded(ref=key)
    _, out = step([marker], base, claims=[holds(key)])
    assert phases(out.events) == [("closed", MERGED)]
    log = applied(base, out.events)
    assert state_of(log, out, key).fields["metadata.lifecycle.closed_at"] == MERGED


def test_a_close_that_would_not_postdate_the_baseline_is_raised_past_it() -> None:
    marker = ev.release_marker("v1.0.0", released_at=ADDED, occurred_at=ADDED)
    base = seeded(ref=marker.key)  # the seed's create is at 2026-01-01; the label at ADDED
    base.append(hand("phase", "bh-seed", {"phase": "open"}, "2026-01-03T00:00:00Z"))
    _, out = step([marker], base, claims=[holds(marker.key)])
    (move,) = [e for e in out.events if str(e.kind) == "phase"]
    assert beadlog.instant(move.occurred_at) > beadlog.instant("2026-01-03T00:00:00Z")
    assert "phase-raised" in out.unit(marker.key).notes


# -- the guard -----------------------------------------------------------------------------------

CLOSING = ev.tracker_item("7", state="closed", opened_at=ADDED, closed_at=MERGED, occurred_at=ADDED)


def no_phase(base: Sequence[beadlog.BeadEvent], artifacts=(CLOSING,), **kw) -> Projection:
    _, out = step(list(artifacts), base, claims=[holds("gh:issue/7")], **kw)
    assert [e for e in out.events if str(e.kind) == "phase"] == []
    return out


def test_a_hand_made_bead_is_never_transitioned() -> None:
    out = no_phase(seeded(label=False, ref="gh:issue/7"))
    assert out.unit("gh:issue/7").decision == "PRESENT"  # matched, its facts stamped as before


def test_a_human_closed_bead_is_never_transitioned() -> None:
    base = seeded(ref="gh:issue/7")
    base.append(hand("phase", "bh-seed", {"phase": "closed", "reason": "done"}, ADDED))
    no_phase(base)


def test_a_bead_a_human_moved_is_never_transitioned() -> None:
    base = seeded(ref="gh:issue/7")
    base.append(hand("phase", "bh-seed", {"phase": "in_progress"}, ADDED))
    no_phase(base)  # in progress, but without this backfill's own stamp
    back = [*seeded(ref="gh:issue/7"), hand("phase", "bh-seed", {"phase": "in_progress"}, ADDED)]
    back.append(hand("phase", "bh-seed", {"phase": "open"}, "2026-01-03T00:00:00Z"))
    no_phase(back)  # moved and moved back: a person touched the phase


def test_a_bead_in_progress_on_its_own_stamp_still_closes() -> None:
    base = seeded(ref="gh:issue/7", **{"metadata.lifecycle.in_progress_at": OPENED})
    base.append(hand("phase", "bh-seed", {"phase": "in_progress"}, OPENED))
    _, out = step([CLOSING], base, claims=[holds("gh:issue/7")])
    assert phases(out.events) == [("closed", MERGED)]


def test_a_doc_status_change_alone_neither_closes_nor_starts_a_bead() -> None:
    _, first = step([doc_commit(), doc("draft")])
    log = applied((), first.events)
    for status in ("accepted", "delivered", "superseded"):
        _, out = step([doc_commit(), doc(status)], log)
        assert [e for e in out.events if str(e.kind) == "phase"] == [], status
    # a doc add commit is not closing evidence even under the 0.36 closed-by-add rule
    _, out = step([doc_commit(), doc("draft")], log, StatusOptions(doc_default="closed"))
    assert [e for e in out.events if str(e.kind) == "phase"] == []


def test_a_different_closed_at_is_reported_and_never_overwritten() -> None:
    base = seeded(ref="gh:issue/7", **{"metadata.lifecycle.closed_at": "2025-12-31T00:00:00Z"})
    _, out = step([CLOSING], base, claims=[holds("gh:issue/7")])
    log = applied(base, out.events)
    assert state_of(log, out, "gh:issue/7").fields["metadata.lifecycle.closed_at"] == (
        "2025-12-31T00:00:00Z"
    )
    assert [k.field for k in out.unit("gh:issue/7").kept] == ["metadata.lifecycle.closed_at"]


def test_nothing_earlier_is_rewritten() -> None:
    base = seeded(ref="gh:issue/7")
    _, out = step([CLOSING], base, claims=[holds("gh:issue/7")])
    assert not {e.id for e in base} & {e.id for e in out.events}


# -- the settings --------------------------------------------------------------------------------


def test_close_on_evidence_false_keeps_the_current_phase() -> None:
    off = StatusOptions(close_on_evidence=False)
    opened = ev.tracker_item("7", opened_at=ADDED, occurred_at=ADDED)
    _, first = step([opened], status=off, claims=[holds("gh:issue/7")])
    log = applied((), first.events)
    no_phase(log, (CLOSING,), status=off)
    # and a review's start is not recorded for a bead the run will not close
    arts = [doc_commit(), doc("draft", cites_review=True), review("merged", merged=MERGED)]
    _, doc_run = step([doc_commit(), doc("draft")], status=off)
    base = applied((), doc_run.events)
    no_phase(base, arts, status=off)
    # the default closes the same bead
    _, closed = step(arts, base)
    assert phases(closed.events) == [("closed", MERGED)]


def test_in_progress_on_evidence_false_writes_no_in_progress() -> None:
    off = StatusOptions(in_progress_on_evidence=False)
    _, created = step([review("open")], status=off)
    assert [e for e in created.events if str(e.kind) == "phase"] == []
    assert state_of(applied((), created.events), created, "gh:pr/5").phase == "open"
    _, first = step([doc_commit(), doc("draft")], status=off)
    log = applied((), first.events)
    arts = [doc_commit(), doc("draft", cites_review=True), review("open")]
    no_phase(log, arts, status=off)
    # the close still follows
    merged = [doc_commit(), doc("draft", cites_review=True), review("merged", merged=MERGED)]
    _, out = step(merged, log, off)
    assert phases(out.events) == [("closed", MERGED)]


def test_the_options_and_the_started_moment_round_trip() -> None:
    opts = StatusOptions(close_on_evidence=False, in_progress_on_evidence=False)
    assert StatusOptions.from_json(opts.to_json()) == opts
    assert StatusOptions().close_on_evidence and StatusOptions().in_progress_on_evidence
    col, _ = step([review("open")])
    again = loads_collation(dumps_collation(col))
    life = lifecycle_of(again, "gh:pr/5")
    assert isinstance(life, Lifecycle) and life.started is not None
    assert Lifecycle.from_json(life.to_json()) == life
    plain = Lifecycle("u", "open", "none")
    assert "started" not in plain.to_json()  # a record without a start is unchanged


def test_a_decision_bead_is_never_transitioned() -> None:
    """A decision record carries origin:backfill but is not a unit's bead: B5 never matches it,
    and the transition guard refuses it besides, so it receives no event at all."""
    base = seeded(ref="gh:issue/7")
    base.append(hand("label_add", "bh-seed", {"label": "source:decision"}, ADDED))
    _, out = step([CLOSING], base, claims=[holds("gh:issue/7")])
    assert [e for e in out.events if e.bead.id == "bh-seed"] == []
    assert out.unit("gh:issue/7").bead != "bh-seed"


def test_a_skip_decided_unit_never_gets_a_transition() -> None:
    """An operator skip (B5 step 0) projects nothing, even for an open backfilled bead whose
    unit now has closing evidence."""
    base = seeded(ref="gh:issue/7")
    base += decision_events(
        prefix="bh",
        group=GROUP,
        key="gh:issue/7",
        kind="skip",
        decided_by="dev/x",
        decided_at=ADDED,
        labels=("origin:backfill",),
    )
    _, out = step([CLOSING], base, claims=[holds("gh:issue/7")])
    assert out.unit("gh:issue/7").decision == "skip"
    assert out.events == ()
