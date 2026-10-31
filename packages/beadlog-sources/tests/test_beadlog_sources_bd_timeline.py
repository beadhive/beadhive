"""The neutral history conversion: bd ``events`` rows -> BeadLog history events.

No real ``bd`` runs and no clock is read: rows are fixtures, the extractor clock is a constant, and
the strategy's two store calls are counting fakes.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import beadlog
from beadlog import BeadEvent, BeadRef, EventKind, fold, union_merge
from beadlog_sources import (
    HISTORY_ANNOTATION,
    BdHistoryStrategy,
    ExtractRequest,
    HistoryEvent,
    baseline_events,
    history_annotation,
    history_events,
    parse_history,
    read_store_start,
    source_family,
    suppress_artifacts,
)

GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"
STAMP = "2026-11-01T00:00:00Z"
FIRST_ROW = "2026-08-07T07:25:10Z"


def row(iid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [],
        "created_at": "2026-08-10T09:00:00Z",
        "updated_at": "2026-08-10T09:00:00Z",
        **extra,
    }


def hist(
    row_id: str, bead: str, kind: str, at: str, *, actor: str | None = "dev/a", **extra: Any
) -> HistoryEvent:
    return HistoryEvent(row_id, bead, kind, at, actor=actor, **extra)


def convert(
    rows: Sequence[dict[str, Any]], events: Sequence[HistoryEvent]
) -> tuple[tuple[BeadEvent, ...], Any]:
    baseline = baseline_events(rows, group=GROUP, extracted_at=STAMP)
    result = history_events(events, baseline, group=GROUP, extracted_at=STAMP)
    return baseline, result


def by_kind(events: Sequence[BeadEvent], kind: EventKind) -> list[BeadEvent]:
    return [event for event in events if event.kind is kind]


def test_a_lifecycle_becomes_observed_bitemporal_events_with_stable_refs() -> None:
    rows = [
        row(
            "wd-1",
            status="closed",
            closed_at="2026-08-12T10:00:00Z",
            assignee="dev/a",
            labels=["review:pending"],
            dependencies=[
                {"issue_id": "wd-1", "depends_on_id": "wd-0", "type": "blocks"},
            ],
        ),
        row("wd-0"),
    ]
    events = [
        hist("u-01", "wd-1", "created", "2026-08-10T09:00:00Z"),
        hist(
            "u-02",
            "wd-1",
            "claimed",
            "2026-08-10T10:00:00Z",
            assignee="dev/a",
            new_status="in_progress",
            changes={"assignee": "dev/a", "status": "in_progress"},
        ),
        hist("u-03", "wd-1", "label_added", "2026-08-11T09:00:00Z", label="review:pending"),
        hist(
            "u-04",
            "wd-1",
            "dependency_added",
            "2026-08-10T09:00:01Z",
            dep_type="blocks",
            dep_target="wd-0",
        ),
        hist(
            "u-05", "wd-1", "closed", "2026-08-12T10:00:00Z", new_status="closed", reason="merged"
        ),
    ]
    baseline, result = convert(rows, events)

    kinds = [str(event.kind) for event in result.events]
    assert sorted(kinds) == sorted(
        ["create", "set", "phase", "phase", "label_add", "edge_add"] + ["create"]
    )  # + wd-0's inferred create
    for event in result.events:
        assert event.source.system == "bd"
        assert event.occurred_at is not None
    observed = [e for e in result.events if e.source.ref.startswith("bd:events/")]
    assert {e.source.ref for e in observed} == {f"bd:events/u-0{n}" for n in range(1, 6)}
    assert all(e.provenance == "observed" and e.recorded_at == e.occurred_at for e in observed)
    closed = next(e for e in result.events if e.data.get("phase") == "closed")
    assert dict(closed.data) == {"phase": "closed", "reason": "merged"}
    claim_set = next(e for e in result.events if e.data.get("field") == "assignee")
    assert claim_set.actor == "dev/a" and claim_set.data["value"] == "dev/a"
    # Deterministic: a second conversion mints the same ids.
    again = history_events(events, baseline, group=GROUP, extracted_at="2026-12-01T00:00:00Z")
    assert [e.id for e in again.events] == [e.id for e in result.events]


def test_a_created_row_stamped_later_than_the_issue_keeps_both_times() -> None:
    rows = [row("wd-1", created_at="2026-07-01T00:00:00Z")]
    events = [hist("u-01", "wd-1", "created", "2026-08-20T12:00:00Z")]
    _, result = convert(rows, events)
    (create,) = by_kind(result.events, EventKind.CREATE)
    assert create.occurred_at == "2026-07-01T00:00:00Z"
    assert create.recorded_at == "2026-08-20T12:00:00Z"
    assert create.provenance == "observed"
    (evidence,) = create.source.evidence
    assert evidence.type == "bd-row" and "recording wall clock" in (evidence.note or "")
    assert result.counts["created_before_events_row"] == 1


def test_a_created_row_not_later_than_the_issue_is_the_creation_time() -> None:
    # production: the events row is within a second BEFORE the issue row, never after it
    rows = [row("wd-1", created_at="2026-08-10T09:00:01Z")]
    events = [hist("u-01", "wd-1", "created", "2026-08-10T09:00:00Z")]
    _, result = convert(rows, events)
    (create,) = by_kind(result.events, EventKind.CREATE)
    assert create.occurred_at == create.recorded_at == "2026-08-10T09:00:00Z"
    assert not create.source.evidence
    assert "created_before_events_row" not in result.counts


def test_a_bead_without_a_created_row_gets_an_inferred_low_confidence_create() -> None:
    rows = [
        row("wd-old", created_at="2026-06-29T03:43:46Z", title="Predates the table"),
        row("wd-imp", created_at="2026-09-16T10:00:00Z"),
        row("wd-1"),
    ]
    events = [
        hist("u-01", "wd-1", "created", "2026-08-10T09:00:00Z"),
        hist("u-02", "wd-old", "label_added", FIRST_ROW, label="x"),  # sets the table's start
    ]
    _, result = convert(rows, events)
    creates = {e.bead.id: e for e in by_kind(result.events, EventKind.CREATE)}
    assert set(creates) == {"wd-1", "wd-old", "wd-imp"}
    old, imported = creates["wd-old"], creates["wd-imp"]
    for inferred in (old, imported):
        assert inferred.provenance == "inferred"
        assert inferred.recorded_at is None
        assert inferred.source.ref == f"bd:inferred/{inferred.bead.id}#create"
        assert inferred.source.evidence
    assert old.occurred_at == "2026-06-29T03:43:46Z" and old.confidence == 0.3
    assert dict(old.data) == {"type": "task", "fields": {"title": "Predates the table"}}
    assert imported.confidence == 0.5  # the table existed and holds no row for it
    assert creates["wd-1"].provenance == "observed"
    assert result.counts["inferred_create"] == 2


def test_closed_open_closed_in_snapshots_without_a_reopened_row_is_no_reopen() -> None:
    # Two close rows and no reopened row: the intermediate "open" exists only in a snapshot (a
    # store revert artifact), so no phase event returns the bead to open.
    rows = [row("wd-1", status="closed", closed_at="2026-08-14T10:00:00Z")]
    events = [
        hist("u-01", "wd-1", "closed", "2026-08-12T10:00:00Z", new_status="closed", reason="a"),
        hist("u-02", "wd-1", "closed", "2026-08-14T10:00:00Z", new_status="closed", reason="b"),
    ]
    _, result = convert(rows, events)
    phases = by_kind(result.events, EventKind.PHASE)
    assert [dict(p.data)["phase"] for p in phases] == ["closed", "closed"]
    assert not [e for e in result.events if e.data.get("phase") in ("open", "in_progress")]


def test_a_reopened_row_is_a_reopen() -> None:
    rows = [row("wd-1")]
    events = [
        hist("u-01", "wd-1", "closed", "2026-08-12T10:00:00Z", new_status="closed"),
        hist(
            "u-02",
            "wd-1",
            "reopened",
            "2026-08-13T10:00:00Z",
            new_status="open",
            reason="more work",
        ),
    ]
    _, result = convert(rows, events)
    assert [dict(p.data) for p in by_kind(result.events, EventKind.PHASE)] == [
        {"phase": "closed"},
        {"phase": "open", "reason": "more work"},
    ]


def test_revert_artifacts_are_suppressed_and_counted() -> None:
    claim = hist(
        "u-01",
        "wd-1",
        "claimed",
        "2026-08-10T10:00:00Z",
        assignee="dev/a",
        new_status="in_progress",
        old_status="open",
        changes={"assignee": "dev/a", "status": "in_progress"},
    )
    unclaim = hist(
        "u-02",
        "wd-1",
        "status_changed",
        "2026-08-10T10:00:00Z",
        old_status="in_progress",
        new_status="open",
    )
    later = hist(
        "u-03",
        "wd-1",
        "status_changed",
        "2026-08-10T11:00:00Z",
        old_status="open",
        new_status="in_progress",
    )
    twin = hist("u-04", "wd-1", "label_added", "2026-08-10T12:00:00Z", label="x")
    twin_again = hist("u-05", "wd-1", "label_added", "2026-08-10T12:00:00Z", label="x")
    kept, dropped = suppress_artifacts([unclaim, claim, later, twin_again, twin])
    assert [e.row_id for e in kept] == ["u-03", "u-04"]
    assert dict(dropped) == {"duplicate_row": 1, "phase_flip_flop": 2}

    # a real change and its undoing a second apart is history, not an artifact
    spread = hist("u-02", "wd-1", "status_changed", "2026-08-10T10:00:01Z", new_status="open")
    kept, dropped = suppress_artifacts([claim, spread])
    assert [e.row_id for e in kept] == ["u-01", "u-02"] and not dropped


def test_history_never_changes_the_folded_current_state() -> None:
    rows = [
        row("wd-1", title="Final title", priority=1),
        row("wd-2", status="in_progress", assignee="dev/a", started_at="2026-08-11T09:00:00Z"),
        row("wd-3", dependencies=[{"issue_id": "wd-3", "depends_on_id": "wd-1", "type": "blocks"}]),
    ]
    events = [
        hist("u-01", "wd-1", "created", "2026-08-10T09:00:00Z"),
        hist("u-02", "wd-1", "updated", "2026-08-10T09:30:00Z", changes={"title": "Draft title"}),
        hist("u-03", "wd-1", "updated", "2026-08-10T10:00:00Z", changes={"title": "Final title"}),
        hist("u-04", "wd-1", "updated", "2026-08-10T10:30:00Z", changes={"priority": "1"}),
        # a transient lease holder: not in the snapshot, so it cannot be history
        hist(
            "u-05", "wd-2", "updated", "2026-08-11T09:00:00Z", changes={"metadata": {"holder": "x"}}
        ),
        # a value the snapshot disagrees with (the table never saw the later change)
        hist("u-06", "wd-2", "updated", "2026-08-11T09:10:00Z", changes={"assignee": "dev/b"}),
        # an edge the snapshot no longer has, a comment the snapshot never had
        hist(
            "u-07",
            "wd-3",
            "dependency_added",
            "2026-08-10T09:00:00Z",
            dep_type="related",
            dep_target="wd-2",
        ),
        hist("u-08", "wd-3", "commented", "2026-08-10T09:05:00Z", reason="gone"),
        # a deleted bead's rows
        hist("u-09", "wd-gone", "created", "2026-08-10T09:00:00Z"),
        hist("u-10", "wd-gone", "label_added", "2026-08-10T09:00:01Z", label="x"),
    ]
    baseline, result = convert(rows, events)

    before = fold(baseline)
    after = fold([*baseline, *result.events])
    assert after == before
    assert not [e for e in result.events if e.bead.id == "wd-gone"]
    assert result.suppressed["orphan_row"] == 2
    assert result.counts["orphan_beads"] == 1
    assert result.suppressed["inconsistent:set"] >= 1
    assert result.suppressed["inconsistent:edge"] == 1
    assert result.suppressed["inconsistent:comment"] == 1
    # the legitimate history survived, including the integer coercion of "1"
    titles = [
        e.data["value"]
        for e in result.events
        if e.kind is EventKind.SET and e.data["field"] == "title"
    ]
    assert titles == ["Draft title", "Final title"]
    assert any(e.data == {"field": "priority", "value": 1} for e in result.events)


def test_a_dependency_removal_takes_its_type_from_the_earlier_addition() -> None:
    rows = [row("wd-1"), row("wd-2")]
    events = [
        hist(
            "u-01",
            "wd-1",
            "dependency_added",
            "2026-08-10T09:00:00Z",
            dep_type="blocks",
            dep_target="wd-2",
        ),
        hist("u-02", "wd-1", "dependency_removed", "2026-08-11T09:00:00Z", dep_target="wd-2"),
        hist("u-03", "wd-1", "dependency_removed", "2026-08-12T09:00:00Z", dep_target="wd-9"),
    ]
    _, result = convert(rows, events)
    edges = [
        (str(e.kind), e.data["type"], e.data["target"]["id"])
        for e in result.events
        if e.kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE)
    ]
    assert edges == [("edge_add", "blocks", "wd-2"), ("edge_remove", "blocks", "wd-2")]
    assert result.counts["dependency_removed_untyped"] == 1


def test_the_two_exports_union_merge_into_one_consistent_log() -> None:
    rows = [row("wd-1"), row("wd-2", status="closed", closed_at="2026-08-12T10:00:00Z")]
    events = [
        hist("u-01", "wd-1", "created", "2026-08-10T09:00:00Z"),
        hist("u-02", "wd-2", "closed", "2026-08-12T10:00:00Z", new_status="closed"),
    ]
    baseline, result = convert(rows, events)
    current = {e.id for e in baseline}
    history = {e.id for e in result.events}
    assert not current & history  # history events have distinct ids
    merged = union_merge([baseline, [*baseline, *result.events]])
    assert {e.id for e in merged} == current | history
    assert fold(merged) == fold(baseline)
    assert {e.bead.group for e in merged} == {GROUP}  # one group id for both modes


def test_the_annotation_reports_sources_the_earliest_recording_and_the_gap() -> None:
    rows = [
        row("wd-old", created_at="2026-06-29T03:43:46Z"),
        row("wd-1", created_at="2026-08-10T09:00:00Z"),
    ]
    events = [
        hist("u-00", "wd-gone", "created", FIRST_ROW),  # the table's first row, of a deleted bead
        hist("u-01", "wd-1", "created", "2026-08-10T09:00:00Z"),
        hist("u-02", "wd-1", "label_added", "2026-10-31T14:02:25Z", label="x"),
    ]
    baseline, result = convert(rows, events)
    everything = [*baseline, *result.events]
    note = history_annotation(everything, result, baseline, store_start="2026-07-25T17:02:33Z")
    body = note[HISTORY_ANNOTATION]
    assert body["sources"]["bd:events"] == sum(
        e.source.ref.startswith("bd:events/") for e in everything
    )
    assert body["sources"]["bd:issues"] == len(baseline)
    assert body["earliest_recorded"] == FIRST_ROW  # the table's first row, not the first kept
    assert body["latest_recorded"] == "2026-10-31T14:02:25Z"
    gap = body["gap"]
    assert gap["first_bead_created"] == "2026-06-29T03:43:46Z"
    assert gap["first_store_commit"] == "2026-07-25T17:02:33Z"
    assert gap["beads_without_recorded_creation"] == 1
    assert (
        "2026-06-29" in gap["note"] and "2026-07-25" in gap["note"] and "2026-08-07" in gap["note"]
    )
    json.dumps(note)  # plain JSON, ready for a manifest annotation


def test_source_families_split_current_state_from_the_events_table() -> None:
    rows = [row("wd-1")]
    baseline, result = convert(rows, [hist("u-01", "wd-1", "created", "2026-08-10T09:00:00Z")])
    assert {source_family(e) for e in baseline} == {"bd:issues"}
    assert {source_family(e) for e in result.events} == {"bd:events"}


def test_the_history_archive_round_trips_and_verifies(tmp_path: Path) -> None:
    rows = [row("wd-1"), row("wd-2", labels=["x"])]
    events = [
        hist("u-01", "wd-1", "created", "2026-08-10T09:00:00Z"),
        hist("u-02", "wd-2", "label_added", "2026-08-10T09:10:00Z", label="x"),
    ]
    baseline, result = convert(rows, events)
    everything = [*baseline, *result.events]
    path = tmp_path / "h.bdlg"
    manifest = beadlog.write_archive(
        path,
        everything,
        groups=[beadlog.Group(GROUP, "wd")],
        generator=beadlog.Generator("t", "0"),
        annotations=history_annotation(everything, result, baseline),
    )
    with beadlog.ArchiveReader(path) as reader:
        reader.verify(ids=True)
        read_back = reader.manifest.to_json()
    assert read_back["annotations"][HISTORY_ANNOTATION]["history_events"] == len(result.events)
    assert manifest.counts["events"] == len(everything)


# -- the strategy and the store reads ---------------------------------------------------------


def test_the_strategy_reads_the_store_in_two_calls_and_emits_history_only() -> None:
    export_calls: list[Sequence[str]] = []
    sql_calls: list[Sequence[str]] = []
    rows = [row("wd-1"), row("wd-2")]
    sql = [
        {
            "issue_id": "wd-1",
            "event_type": "created",
            "actor": "dev/a",
            "old_value": "",
            "new_value": "",
            "comment": None,
            "created_at": "2026-08-10T09:00:00Z",
            "id": "u-01",
        }
    ]

    def export_runner(argv: Sequence[str]) -> str:
        export_calls.append(argv)
        return "\n".join(json.dumps(r) for r in rows)

    def sql_runner(argv: Sequence[str]) -> str:
        sql_calls.append(argv)
        return json.dumps(sql)

    strategy = BdHistoryStrategy(export_runner, sql_runner)
    events = strategy.extract(ExtractRequest(root=Path("/hive"), group=GROUP, extracted_at=STAMP))
    assert len(export_calls) == len(sql_calls) == 1
    assert sql_calls[0][-3:-1] == ["sql", "--json"] or "SELECT" in sql_calls[0][-1]
    assert {e.bead.id for e in events} == {"wd-1", "wd-2"}
    assert all(e.source.ref.startswith(("bd:events/", "bd:inferred/")) for e in events)
    assert strategy.name == "bd-history"


def test_the_strategy_is_registered_as_an_entry_point() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    points = tomllib.loads(pyproject.read_text())["project"]["entry-points"]["beadlog.sources"]
    assert points["bd-history"] == "beadlog_sources.bd_timeline:BdHistoryStrategy"
    assert points["bd"] == "beadlog_sources.bd:BdBaselineStrategy"


def test_the_store_start_is_one_select_and_never_fatal() -> None:
    calls: list[Sequence[str]] = []

    def good(argv: Sequence[str]) -> str:
        calls.append(argv)
        return json.dumps([{"first": "2026-07-25T17:02:33.319Z", "n": 3}])

    assert read_store_start("/hive", runner=good) == "2026-07-25T17:02:33.319Z"
    assert len(calls) == 1 and calls[0][-1].startswith("SELECT MIN(date)")

    def broken(argv: Sequence[str]) -> str:
        return "not json"

    assert read_store_start("/hive", runner=broken) is None


def test_parse_history_feeds_the_conversion_directly() -> None:
    read = parse_history(
        [
            {
                "issue_id": "wd-1",
                "event_type": "created",
                "actor": "a",
                "old_value": "",
                "new_value": "",
                "comment": None,
                "created_at": "2026-08-10T09:00:00Z",
                "id": "u-01",
            },
            {
                "issue_id": "wd-1",
                "event_type": "bogus",
                "created_at": "2026-08-10T09:00:00Z",
                "id": "u-02",
            },
        ]
    )
    baseline = baseline_events([row("wd-1")], group=GROUP, extracted_at=STAMP)
    result = history_events(read, baseline, group=GROUP, extracted_at=STAMP)
    assert result.unknown_types == {"bogus": 1} and result.rows == 2
    assert result.first_row == "2026-08-10T09:00:00Z"
    assert BeadRef(GROUP, "wd-1") in fold(result.events)
