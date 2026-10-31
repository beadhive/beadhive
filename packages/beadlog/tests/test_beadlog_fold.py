"""``fold`` against the worked example archive and hand-built edge cases (format ADR section 6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import beadlog

EXAMPLES = (
    Path(__file__).resolve().parents[3] / "docs" / "schemas" / "beadlog" / "v0.1" / "examples"
)
GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"
ERASED_COMMENT = "sha256:c935ef49a4d0c644a7d60b15527b128f4e5347f93da55737d113835fb9d63087"


def ref(name: str) -> beadlog.BeadRef:
    return beadlog.BeadRef(GROUP, name)


@pytest.fixture(scope="module")
def archive() -> tuple[beadlog.BeadEvent, ...]:
    events = []
    for part in ("events/bh/part-0000.jsonl", "checkpoints/bh/part-0000.jsonl"):
        for line in (EXAMPLES / "archive" / part).read_text(encoding="utf-8").splitlines():
            events.append(beadlog.BeadEvent.from_json(json.loads(line)))
    return tuple(events)


def test_open_cut_reproduces_every_example_bead(archive: tuple[beadlog.BeadEvent, ...]) -> None:
    states = beadlog.fold(archive)
    assert sorted(bead.id for bead in states) == ["bh-ex1", "bh-ex2", "bh-ex3", "bh-ex4"]

    task = states[ref("bh-ex1")]
    assert (task.exists, task.deleted, task.type, task.phase) == (True, False, "task", "closed")
    assert dict(task.fields) == {
        "metadata.estimate_minutes": 90,
        "priority": 2,
        "title": "Write the BeadLog v0 format ADR",
    }
    assert "assignee" not in task.fields  # set to null, so omitted
    assert task.labels == ("component:backfill", "size:s")
    assert task.edges == (beadlog.Edge("parent-child", ref("bh-ex2")),)
    assert [comment.body for comment in task.comments] == [
        "Schemas live under docs/schemas/beadlog/v0.1.\nNaïve café check: ✓"
    ]
    assert ERASED_COMMENT not in {comment.id for comment in task.comments}

    epic = states[ref("bh-ex2")]
    assert (epic.type, epic.phase, epic.labels, epic.edges) == ("epic", "open", (), ())

    decision = states[ref("bh-ex3")]
    assert decision.phase == "retired"
    assert decision.retired is not None
    assert dict(decision.retired["successor"]) == ref("bh-ex1").to_json()

    mistake = states[ref("bh-ex4")]
    assert (mistake.exists, mistake.deleted) == (False, True)


@pytest.mark.parametrize(
    ("cut", "labels", "edges", "title"),
    [
        ("2026-07-03T00:00:00Z", (), ("bh-ex2",), "Draft the BeadLog ADR"),
        ("2026-08-08T14:30:00Z", ("component:backfill",), ("bh-ex2",), "Draft the BeadLog ADR"),
        (
            "2026-08-09T08:05:00Z",
            ("component:backfill",),
            ("bh-ex2", "bh-ex3"),
            "Write the BeadLog v0 format ADR",
        ),
        ("2026-08-10T11:05:00Z", ("component:backfill", "size:m"), ("bh-ex2",), None),
        ("2026-08-11T09:00:00Z", ("component:backfill", "size:s"), ("bh-ex2",), None),
    ],
)
def test_valid_time_cuts_reproduce_labels_edges_and_fields(
    archive: tuple[beadlog.BeadEvent, ...],
    cut: str,
    labels: tuple[str, ...],
    edges: tuple[str, ...],
    title: str | None,
) -> None:
    task = beadlog.fold(archive, at=cut)[ref("bh-ex1")]
    assert task.labels == labels
    assert sorted(edge.target.id for edge in task.edges) == sorted(edges)
    assert task.fields["title"] == (title or "Write the BeadLog v0 format ADR")


def test_cut_before_a_bead_exists_leaves_it_out(archive: tuple[beadlog.BeadEvent, ...]) -> None:
    states = beadlog.fold(archive, at="2026-07-02T23:59:59Z")
    assert list(states) == [ref("bh-ex2")]
    assert beadlog.fold(archive, at="2026-06-01T00:00:00Z") == {}


def test_fractional_instant_orders_after_the_whole_second(
    archive: tuple[beadlog.BeadEvent, ...],
) -> None:
    # The title set happened at 14:30:00.25Z: not yet at 14:30:00Z, visible at 14:30:00.25Z.
    before = beadlog.fold(archive, at="2026-08-08T14:30:00Z")[ref("bh-ex1")]
    after = beadlog.fold(archive, at="2026-08-08T14:30:00.25Z")[ref("bh-ex1")]
    assert before.fields["title"] == "Draft the BeadLog ADR"
    assert after.fields["title"] == "Write the BeadLog v0 format ADR"


def test_example_checkpoint_matches_the_refold(archive: tuple[beadlog.BeadEvent, ...]) -> None:
    (checkpoint,) = [event for event in archive if event.kind is beadlog.EventKind.CHECKPOINT]
    state = beadlog.fold(archive, at=beadlog.checkpoint_key(checkpoint))[checkpoint.bead]
    expected = json.loads(json.dumps(checkpoint.to_json()["data"]["state"]))
    assert state.to_checkpoint_state() == expected


def test_recorded_as_of_drops_unrecorded_and_later_recorded_events(
    archive: tuple[beadlog.BeadEvent, ...],
) -> None:
    states = beadlog.fold(archive, recorded_as_of="2026-08-09T08:00:00Z")
    # Day-precision doc events and the git-derived epic have no recorded time.
    assert ref("bh-ex2") not in states and ref("bh-ex3") not in states
    task = states[ref("bh-ex1")]
    assert task.exists is False  # its create came from a doc with recorded_at null
    assert task.phase == "in_progress"
    assert task.labels == ("component:backfill",)
    assert task.edges == ()  # the parent edge is inferred and unrecorded
    assert len(task.comments) == 1


def _event(kind: str, bead: str, data: dict, at: str | None, *, ref_: str) -> beadlog.BeadEvent:
    return beadlog.mint_event(
        kind,
        ref(bead),
        data,
        occurred_at=at,
        recorded_at=at,
        extracted_at="2026-10-30T00:00:00Z",
        source=("test", ref_),
        provenance="observed",
    )


def test_scope_bead_tombstone_erases_every_event_and_keeps_identity() -> None:
    create = _event(
        "create",
        "bh-a",
        {"type": "task", "fields": {"title": "x"}},
        "2026-01-01T00:00:00Z",
        ref_="1",
    )
    label = _event("label_add", "bh-a", {"label": "secret"}, "2026-01-02T00:00:00Z", ref_="2")
    tomb = _event(
        "tombstone",
        "bh-a",
        {"scope": "bead", "reason": "personal-data"},
        "2026-01-03T00:00:00Z",
        ref_="3",
    )
    other = _event(
        "create",
        "bh-b",
        {"type": "task", "fields": {"title": "y"}},
        "2026-01-01T00:00:00Z",
        ref_="4",
    )
    for cut in (None, "2026-01-02T00:00:00Z"):
        states = beadlog.fold([create, label, tomb, other], at=cut)
        assert states[ref("bh-a")] == beadlog.BeadState(bead=ref("bh-a"), erased=True)
        assert states[ref("bh-b")].exists
    assert beadlog.union_merge([[create, label, other], [tomb]]) == beadlog.sort_events(
        [tomb, other]
    )


def test_events_scope_tombstone_applies_to_earlier_cuts_too() -> None:
    comment = _event("comment", "bh-a", {"body": "token=hunter2"}, "2026-01-01T00:00:00Z", ref_="1")
    tomb = _event(
        "tombstone",
        "bh-a",
        {"scope": "events", "events": [comment.id], "reason": "secret"},
        "2026-02-01T00:00:00Z",
        ref_="2",
    )
    assert beadlog.fold([comment], at="2026-01-15T00:00:00Z")[ref("bh-a")].comments
    erased = beadlog.fold([comment, tomb], at="2026-01-15T00:00:00Z")
    assert ref("bh-a") not in erased


def _checkpoint(
    bead: str, covers: beadlog.BeadEvent, state: dict, *, ref_: str
) -> beadlog.BeadEvent:
    return beadlog.mint_event(
        "checkpoint",
        ref(bead),
        {
            "state": state,
            "covers_through": {
                "occurred_at": covers.occurred_at,
                "recorded_at": covers.recorded_at,
                "id": covers.id,
            },
        },
        occurred_at=covers.occurred_at,
        recorded_at=covers.recorded_at,
        extracted_at="2026-10-30T00:00:00Z",
        source=("beadlog", ref_),
        provenance="derived",
    )


def test_checkpoint_seeds_a_bead_whose_history_starts_after_it() -> None:
    lost = _event(
        "create",
        "bh-a",
        {"type": "task", "fields": {"title": "old"}},
        "2026-01-01T00:00:00Z",
        ref_="lost",
    )
    seed_state = {
        "type": "task",
        "phase": "in_progress",
        "fields": {"title": "kept", "priority": 1},
        "labels": ["a", "b"],
        "edges": [{"type": "blocks", "target": ref("bh-z").to_json()}],
        "deleted": False,
    }
    seed = _checkpoint("bh-a", lost, seed_state, ref_="seed")
    later = _event("label_remove", "bh-a", {"label": "a"}, "2026-02-01T00:00:00Z", ref_="later")
    retitle = _event(
        "set", "bh-a", {"field": "title", "value": "new"}, "2026-03-01T00:00:00Z", ref_="t"
    )

    state = beadlog.fold([seed, later, retitle])[ref("bh-a")]
    assert (state.exists, state.type, state.phase) == (True, "task", "in_progress")
    assert dict(state.fields) == {"priority": 1, "title": "new"}
    assert state.labels == ("b",)
    assert state.edges == (beadlog.Edge("blocks", ref("bh-z")),)
    assert beadlog.fold([seed, later, retitle], at="2025-12-31T00:00:00Z") == {}

    # With the covered history present the checkpoint is only an assertion.
    asserted = beadlog.fold([lost, seed, later, retitle])[ref("bh-a")]
    assert dict(asserted.fields) == {"title": "new"}
    assert asserted.phase == "open"


def test_facets_are_independent_and_last_writer_wins_per_facet() -> None:
    day = "2026-05-01T00:00:00Z"
    edge = _event(
        "edge_add", "bh-a", {"type": "parent-child", "target": ref("bh-p").to_json()}, day, ref_="e"
    )
    create = _event(
        "create", "bh-a", {"type": "task", "fields": {"title": "t", "owner": "x"}}, day, ref_="c"
    )
    retype = _event(
        "set", "bh-a", {"field": "type", "value": "bug"}, "2026-05-02T00:00:00Z", ref_="r"
    )
    retire = _event("retire", "bh-a", {"reason": "dup"}, "2026-05-03T00:00:00Z", ref_="x")
    reopen = _event("phase", "bh-a", {"phase": "open"}, "2026-05-04T00:00:00Z", ref_="o")
    delete = _event("delete", "bh-a", {}, "2026-05-05T00:00:00Z", ref_="d")
    recreate = _event(
        "create",
        "bh-a",
        {"type": "task", "fields": {"title": "t2"}},
        "2026-05-06T00:00:00Z",
        ref_="c2",
    )
    log = [recreate, delete, reopen, retire, retype, create, edge]

    assert beadlog.fold(log, at=day)[ref("bh-a")].edges == (
        beadlog.Edge("parent-child", ref("bh-p")),
    )
    assert beadlog.fold(log, at="2026-05-02T00:00:00Z")[ref("bh-a")].type == "bug"
    retired = beadlog.fold(log, at="2026-05-03T00:00:00Z")[ref("bh-a")]
    assert (retired.phase, dict(retired.retired or {})) == ("retired", {"reason": "dup"})
    assert retired.to_checkpoint_state()["retired"] == {"reason": "dup"}
    reopened = beadlog.fold(log, at="2026-05-04T00:00:00Z")[ref("bh-a")]
    assert (reopened.phase, reopened.retired) == ("open", None)
    deleted = beadlog.fold(log, at="2026-05-05T00:00:00Z")[ref("bh-a")]
    assert (deleted.exists, deleted.deleted) == (False, True)
    final = beadlog.fold(log)[ref("bh-a")]
    assert (final.exists, final.deleted, final.type, final.phase) == (True, False, "task", "open")
    assert dict(final.fields) == {"owner": "x", "title": "t2"}


def test_undated_proposals_apply_only_at_the_open_cut() -> None:
    dated = _event(
        "create",
        "bh-a",
        {"type": "task", "fields": {"title": "t"}},
        "2026-01-01T00:00:00Z",
        ref_="1",
    )
    proposal = _event("label_add", "bh-a", {"label": "complexity:SIMPLE"}, None, ref_="2")
    assert beadlog.fold([dated, proposal])[ref("bh-a")].labels == ("complexity:SIMPLE",)
    assert beadlog.fold([dated, proposal], at="9999-12-31T23:59:59Z")[ref("bh-a")].labels == ()


def test_duplicate_ids_fold_once() -> None:
    comment = _event("comment", "bh-a", {"body": "once"}, "2026-01-01T00:00:00Z", ref_="1")
    assert len(beadlog.fold([comment, comment])[ref("bh-a")].comments) == 1
