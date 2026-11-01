"""The as-of reader over a fake connection (no XTDB needed).

The fake returns the rows a test hands it for a table, whatever the temporal clause; what XTDB
does with ``FOR VALID_TIME AS OF`` and ``FOR ALL SYSTEM_TIME`` is the integration test's job. Here
the rows are the ones XTDB 2.2.0 returns (captured shapes: aware ``datetime`` period columns,
``None`` for an open ``_valid_to`` / ``_system_to``), and the claims are about what the reader
makes of them.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from beadlog import ArchiveReader, BeadRef, fold, ordering_key
from beadlog_xtdb import (
    BEAD_COMMENTS,
    BEAD_EDGES,
    BEAD_LABELS,
    BEADS,
    SOURCE_SYSTEM,
    ReaderError,
    ReadResult,
    group_dirs,
    read_history,
    read_state,
    write_baseline,
)
from beadlog_xtdb._reader import PERIOD_COLUMNS, as_datetime, select_statement

GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"
OTHER = "urn:uuid:11111111-2222-3333-4444-555555555555"
EXTRACTED = "2026-10-31T00:00:00Z"


def at(text: str) -> datetime:
    return as_datetime(text)


def row(
    key: str,
    valid_from: str,
    valid_to: str | None = None,
    sys_from: str | None = "2026-10-30T00:00:00Z",
    sys_to: str | None = None,
    **doc: Any,
) -> dict[str, Any]:
    return {
        "_id": key,
        **doc,
        "_valid_from": at(valid_from),
        "_valid_to": at(valid_to) if valid_to else None,
        "_system_from": at(sys_from) if sys_from else None,
        "_system_to": at(sys_to) if sys_to else None,
    }


def bead(name: str, valid_from: str, group: str = GROUP, key: str | None = None, **kw: Any):
    """A ``beads`` row; ``key`` defaults to the plain bead id (a single-group store)."""
    doc = {"group_id": group, "bead_id": f"bh-{name}", "issue_type": "task", "title": name.upper()}
    doc.update({k: v for k, v in kw.items() if k not in ("valid_to", "sys_from", "sys_to")})
    return row(
        key or f"bh-{name}",
        valid_from,
        kw.get("valid_to"),
        kw.get("sys_from", "2026-10-30T00:00:00Z"),
        kw.get("sys_to"),
        **doc,
    )


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        names: list[str] = []
        for record in rows:
            names += [name for name in record if name not in names]
        self.description = [SimpleNamespace(name=name) for name in names]
        self._rows = [tuple(record.get(name) for name in names) for record in rows]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    """Returns the canned rows of whichever table the statement selects from."""

    def __init__(self, **tables: list[dict[str, Any]]) -> None:
        self.tables = tables
        self.statements: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: list[Any] | None = None) -> FakeCursor:
        self.statements.append((sql, list(params or [])))
        table = sql.split(" FROM ", 1)[1].split()[0]
        return FakeCursor(self.tables.get(table, []))


def events_by_kind(result: ReadResult, kind: str) -> list[Any]:
    return [event for event in result.events if str(event.kind) == kind]


# -- statements ----------------------------------------------------------------------------


def test_select_statements_carry_the_requested_cut() -> None:
    cut = at("2026-10-05T00:00:00Z")
    columns = ", ".join(PERIOD_COLUMNS)
    assert select_statement(BEADS, valid_at=None, system_at=None, history=False) == (
        f"SELECT *, {columns} FROM beads",
        [],
    )
    assert select_statement(BEADS, valid_at=cut, system_at=None, history=False) == (
        f"SELECT *, {columns} FROM beads FOR VALID_TIME AS OF %s",
        [cut],
    )
    assert select_statement(BEAD_LABELS, valid_at=cut, system_at=cut, history=False) == (
        f"SELECT *, {columns} FROM bead_labels FOR SYSTEM_TIME AS OF %s FOR VALID_TIME AS OF %s",
        [cut, cut],
    )
    assert select_statement(BEAD_EDGES, valid_at=cut, system_at=cut, history=True) == (
        f"SELECT *, {columns} FROM bead_edges FOR ALL SYSTEM_TIME FOR ALL VALID_TIME",
        [],
    )


def test_a_table_name_is_never_interpolated_unchecked() -> None:
    with pytest.raises(ValueError, match="not a safe table name"):
        select_statement("beads; DROP", valid_at=None, system_at=None, history=False)


def test_a_cut_must_be_an_aware_canonical_instant() -> None:
    with pytest.raises(ReaderError, match="canonical timestamp"):
        as_datetime("2026-10-05")
    with pytest.raises(ReaderError, match="aware"):
        as_datetime(datetime(2026, 10, 5))  # noqa: DTZ001 - the naive value is the point
    assert as_datetime("2026-10-05T00:00:00.5Z").microsecond == 500000


def test_read_state_passes_the_cuts_to_every_table() -> None:
    conn = FakeConnection()
    read_state(conn, extracted_at=EXTRACTED, valid_at="2026-10-05T00:00:00Z", system_at=None)
    tables = [sql.split(" FROM ", 1)[1].split()[0] for sql, _ in conn.statements]
    assert tables == [BEADS, BEAD_LABELS, BEAD_EDGES, BEAD_COMMENTS]
    assert all(params == [at("2026-10-05T00:00:00Z")] for _, params in conn.statements)


# -- state ---------------------------------------------------------------------------------


def test_a_state_read_emits_observed_xtdb_events_that_fold_to_the_stored_state() -> None:
    conn = FakeConnection(
        beads=[
            bead(
                "a",
                "2026-10-03T00:00:00Z",
                priority=2,
                due_at=at("2026-12-01T09:30:00.123456Z"),
                metadata={"git.commits": '["abc"]'},
            ),
            bead("b", "2026-10-02T00:00:00Z", issue_type="bug"),
        ],
        bead_labels=[
            row("bh-a|area:core", "2026-10-02T09:00:00Z", bead_id="bh-a", label="area:core")
        ],
        bead_edges=[
            row(
                "bh-a|blocks|bh-b",
                "2026-10-03T09:00:00Z",
                from_id="bh-a",
                type="blocks",
                to_id="bh-b",
                created_by="alice",
            )
        ],
        bead_comments=[
            row(
                "sha256:" + "c" * 64,
                "2026-10-04T11:00:00Z",
                bead_id="bh-a",
                author="alice",
                text="looks ~odd",
            )
        ],
    )
    result = read_state(conn, extracted_at=EXTRACTED)
    assert result.notes == ()
    assert {str(e.provenance) for e in result.events} == {"observed"}
    assert {e.source.system for e in result.events} == {SOURCE_SYSTEM}
    assert {e.extracted_at for e in result.events} == {EXTRACTED}
    states = fold(result.events)
    a = states[BeadRef(GROUP, "bh-a")]
    assert (a.type, a.phase, a.labels) == ("task", "open", ("area:core",))
    assert dict(a.fields) == {
        "due_at": "2026-12-01T09:30:00.123456Z",
        "metadata.git.commits": '["abc"]',
        "priority": 2,
        "title": "A",
    }
    assert [(e.type, e.target) for e in a.edges] == [("blocks", BeadRef(GROUP, "bh-b"))]
    assert [(c.actor, c.body) for c in a.comments] == [("alice", "looks ~odd")]
    assert states[BeadRef(GROUP, "bh-b")].type == "bug"


def test_a_state_read_is_dated_by_the_store_and_never_by_the_clock() -> None:
    conn = FakeConnection(
        beads=[bead("a", "2026-10-03T00:00:00Z", sys_from="2026-10-30T01:02:03.5Z")],
        bead_labels=[
            row("bh-a|x", "2026-10-03T00:00:00Z", sys_from=None, bead_id="bh-a", label="x")
        ],
    )
    result = read_state(conn, extracted_at=EXTRACTED)
    create = events_by_kind(result, "create")[0]
    assert (create.occurred_at, create.recorded_at) == (
        "2026-10-03T00:00:00Z",
        "2026-10-30T01:02:03.5Z",
    )
    assert events_by_kind(result, "label_add")[0].recorded_at is None  # unknown stays null
    again = read_state(conn, extracted_at=EXTRACTED)
    assert again.events == result.events  # the same store reads to the same ids


def test_a_non_open_phase_follows_its_create_by_one_microsecond() -> None:
    """A create and a phase share a facet: the reader orders them, not the fold's id tie-break."""
    conn = FakeConnection(
        beads=[bead("a", "2026-10-03T00:00:00Z", phase="closed", metadata=None)],
    )
    result = read_state(conn, extracted_at=EXTRACTED)
    create, phase = events_by_kind(result, "create")[0], events_by_kind(result, "phase")[0]
    assert (create.occurred_at, phase.occurred_at) == (
        "2026-10-03T00:00:00Z",
        "2026-10-03T00:00:00.000001Z",
    )
    assert dict(phase.annotations[SOURCE_SYSTEM]) == {
        "us_split": 1,
        "valid_from": "2026-10-03T00:00:00Z",
    }
    assert result.note_codes() == {"x-xtdb.us-split": 1}
    assert fold(result.events)[BeadRef(GROUP, "bh-a")].phase == "closed"
    # Whatever the ids, the order is the same: the shift makes it causal.
    ordered = sorted(result.events, key=ordering_key)
    assert [str(e.kind) for e in ordered] == ["create", "phase"]


def test_retired_and_deleted_beads_fold_back() -> None:
    conn = FakeConnection(
        beads=[
            bead(
                "r",
                "2026-10-03T00:00:00Z",
                phase="retired",
                retired={"successor": {"group": GROUP, "id": "bh-a"}, "reason": "merged"},
            ),
            bead("d", "2026-10-03T00:00:00Z", deleted=True),
        ]
    )
    states = fold(read_state(conn, extracted_at=EXTRACTED).events)
    retired, deleted = states[BeadRef(GROUP, "bh-r")], states[BeadRef(GROUP, "bh-d")]
    assert retired.phase == "retired" and retired.exists
    assert dict(retired.retired) == {
        "successor": {"group": GROUP, "id": "bh-a"},
        "reason": "merged",
    }
    assert deleted.deleted and not deleted.exists


def test_a_multi_group_store_is_detected_and_split_by_group() -> None:
    conn = FakeConnection(
        beads=[
            bead("a", "2026-10-03T00:00:00Z", key=f"{GROUP}:bh-a"),
            bead("a", "2026-10-03T00:00:00Z", group=OTHER, key=f"{OTHER}:bh-a"),
        ],
        bead_edges=[
            row(
                f"{GROUP}:bh-a|related|{OTHER}:bh-a",
                "2026-10-03T09:00:00Z",
                from_id=f"{GROUP}:bh-a",
                type="related",
                to_id=f"{OTHER}:bh-a",
            )
        ],
    )
    result = read_state(conn, extracted_at=EXTRACTED)
    assert result.multi_group and result.groups == (OTHER, GROUP)
    edge = events_by_kind(result, "edge_add")[0]
    assert edge.bead == BeadRef(GROUP, "bh-a")
    assert edge.data["target"] == {"group": OTHER, "id": "bh-a"}
    only = read_state(conn, extracted_at=EXTRACTED, groups=[OTHER])
    assert only.groups == (OTHER,) and only.notes == ()  # the other group's rows vanish quietly


def test_what_a_read_cannot_keep_is_reported_not_dropped_silently() -> None:
    conn = FakeConnection(
        beads=[
            bead("a", "2026-10-03T00:00:00Z", ratio=0.5, big=2**60, Bad_Name="x", priority=3),
            bead("b", "2026-10-03T00:00:00Z"),
        ],
        bead_labels=[row("bh-z|x", "2026-10-03T00:00:00Z", bead_id="bh-z", label="x")],
        bead_edges=[
            row(
                "bh-a|blocks|bh-b",
                "2026-10-03T00:00:00Z",
                from_id="bh-a",
                type="blocks",
                to_id="bh-b",
                metadata={"why": "x"},
            )
        ],
        bead_comments=[row("sha256:" + "d" * 64, "2026-10-04T00:00:00Z", bead_id="bh-a")],
    )
    result = read_state(conn, extracted_at=EXTRACTED)
    assert result.note_codes() == {
        "comment.body-missing": 1,
        "field.unsupported": 3,
        "row.orphan": 1,
        "x-xtdb.edge-metadata": 1,
    }
    a = fold(result.events)[BeadRef(GROUP, "bh-a")]
    assert dict(a.fields) == {"priority": 3, "title": "A"}
    assert "bh-a" in str(result.notes[0]) or result.notes[0].bead is None


def test_a_bead_row_without_its_identity_columns_is_refused() -> None:
    conn = FakeConnection(beads=[row("bh-a", "2026-10-03T00:00:00Z", title="bd-export shaped")])
    with pytest.raises(ReaderError, match="group_id and bead_id"):
        read_state(conn, extracted_at=EXTRACTED)


# -- history -------------------------------------------------------------------------------


def test_history_diffs_document_versions_into_changes() -> None:
    conn = FakeConnection(
        beads=[
            bead(
                "a",
                "2026-10-01T00:00:00Z",
                valid_to="2026-10-03T00:00:00Z",
                description="d",
                priority=2,
                sys_from="2026-10-01T00:00:01Z",
            ),
            bead(
                "a",
                "2026-10-03T00:00:00Z",
                valid_to="2026-10-05T00:00:00Z",
                priority=1,
                title="A2",
                issue_type="bug",
                sys_from="2026-10-03T00:00:01Z",
            ),
            bead(
                "a",
                "2026-10-05T00:00:00Z",
                priority=1,
                title="A2",
                issue_type="bug",
                phase="closed",
                sys_from="2026-10-05T00:00:01Z",
            ),
        ]
    )
    result = read_history(conn, extracted_at=EXTRACTED)
    assert result.notes == ()
    changes = {
        (str(e.kind), e.occurred_at, e.recorded_at, repr(e.to_json()["data"]))
        for e in result.events
    }
    first, second, third = (
        ("2026-10-01T00:00:00Z", "2026-10-01T00:00:01Z"),
        ("2026-10-03T00:00:00Z", "2026-10-03T00:00:01Z"),
        ("2026-10-05T00:00:00Z", "2026-10-05T00:00:01Z"),
    )
    created = {"type": "task", "fields": {"description": "d", "priority": 2, "title": "A"}}
    assert changes == {
        ("create", *first, repr(created)),
        ("set", *second, repr({"field": "type", "value": "bug"})),
        ("set", *second, repr({"field": "description", "value": None})),
        ("set", *second, repr({"field": "priority", "value": 1})),
        ("set", *second, repr({"field": "title", "value": "A2"})),
        ("phase", *third, repr({"phase": "closed"})),
    }


def test_history_folds_to_the_state_at_every_valid_time_cut() -> None:
    conn = FakeConnection(
        beads=[
            bead("a", "2026-10-01T00:00:00Z", valid_to="2026-10-03T00:00:00Z", priority=2),
            bead("a", "2026-10-03T00:00:00Z", priority=1, phase="in_progress"),
        ],
        bead_labels=[
            row(
                "bh-a|x", "2026-10-02T00:00:00Z", "2026-10-04T00:00:00Z", bead_id="bh-a", label="x"
            ),
            row("bh-a|x", "2026-10-06T00:00:00Z", bead_id="bh-a", label="x"),
        ],
    )
    result = read_history(conn, extracted_at=EXTRACTED)
    ref = BeadRef(GROUP, "bh-a")

    def at_cut(cut: str):
        return fold(result.events, at=cut)[ref]

    assert (
        dict(at_cut("2026-10-01T12:00:00Z").fields)["priority"],
        at_cut("2026-10-01T12:00:00Z").labels,
    ) == (2, ())
    assert (at_cut("2026-10-03T12:00:00Z").phase, at_cut("2026-10-03T12:00:00Z").labels) == (
        "in_progress",
        ("x",),
    )
    assert at_cut("2026-10-05T00:00:00Z").labels == ()
    assert at_cut("2026-10-07T00:00:00Z").labels == ("x",)


def test_a_removal_is_recorded_when_its_closing_rectangle_stopped_being_believed() -> None:
    """DELETE ... FOR PORTION OF VALID_TIME FROM t TO NULL leaves [from, t) believed with its
    original system time, and [t, inf) closed at the removal's system time."""
    conn = FakeConnection(
        bead_labels=[
            row(
                "bh-a|x",
                "2026-10-02T00:00:00Z",
                "2026-10-04T00:00:00Z",
                sys_from="2026-10-02T00:00:01Z",
                bead_id="bh-a",
                label="x",
            ),
            row(
                "bh-a|x",
                "2026-10-04T00:00:00Z",
                None,
                sys_from="2026-10-02T00:00:01Z",
                sys_to="2026-10-04T00:00:07Z",
                bead_id="bh-a",
                label="x",
            ),
        ],
        beads=[bead("a", "2026-10-01T00:00:00Z", sys_from="2026-10-01T00:00:00Z")],
    )
    result = read_history(conn, extracted_at=EXTRACTED)
    add, remove = events_by_kind(result, "label_add")[0], events_by_kind(result, "label_remove")[0]
    assert (add.occurred_at, add.recorded_at) == ("2026-10-02T00:00:00Z", "2026-10-02T00:00:01Z")
    assert (remove.occurred_at, remove.recorded_at) == (
        "2026-10-04T00:00:00Z",
        "2026-10-04T00:00:07Z",
    )
    # As believed before the removal was recorded, the label is still held.
    early = read_history(conn, extracted_at=EXTRACTED, system_at="2026-10-04T00:00:01Z")
    assert events_by_kind(early, "label_remove") == []
    assert events_by_kind(early, "label_add")[0].recorded_at == "2026-10-02T00:00:01Z"


def test_system_time_history_keeps_only_what_was_believed_then() -> None:
    first = bead(
        "a",
        "2026-10-01T00:00:00Z",
        title="old",
        sys_from="2026-10-01T00:00:00Z",
        sys_to="2026-10-09T00:00:00Z",
    )
    later = bead("a", "2026-10-01T00:00:00Z", title="corrected", sys_from="2026-10-09T00:00:00Z")
    conn = FakeConnection(beads=[first, later])
    now = fold(read_history(conn, extracted_at=EXTRACTED).events)[BeadRef(GROUP, "bh-a")]
    then = fold(
        read_history(conn, extracted_at=EXTRACTED, system_at="2026-10-05T00:00:00Z").events
    )[BeadRef(GROUP, "bh-a")]
    assert (now.fields["title"], then.fields["title"]) == ("corrected", "old")


def test_touching_fragments_of_one_period_are_one_period() -> None:
    """XTDB splits a row when a write trims it; the halves are not an add, a remove and an add."""
    conn = FakeConnection(
        beads=[bead("a", "2026-10-01T00:00:00Z")],
        bead_labels=[
            row(
                "bh-a|x", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z", bead_id="bh-a", label="x"
            ),
            row("bh-a|x", "2026-10-03T00:00:00Z", bead_id="bh-a", label="x"),
        ],
    )
    result = read_history(conn, extracted_at=EXTRACTED)
    assert [str(e.kind) for e in result.events if "label" in str(e.kind)] == ["label_add"]


def test_a_document_that_ends_without_a_successor_is_a_delete() -> None:
    conn = FakeConnection(
        beads=[
            bead("a", "2026-10-01T00:00:00Z", valid_to="2026-10-03T00:00:00Z"),
            bead("a", "2026-10-05T00:00:00Z", title="again"),
        ]
    )
    result = read_history(conn, extracted_at=EXTRACTED)
    assert [(str(e.kind), e.occurred_at) for e in sorted(result.events, key=ordering_key)] == [
        ("create", "2026-10-01T00:00:00Z"),
        ("delete", "2026-10-03T00:00:00Z"),
        ("create", "2026-10-05T00:00:00Z"),
    ]
    ref = BeadRef(GROUP, "bh-a")
    assert fold(result.events, at="2026-10-04T00:00:00Z")[ref].deleted
    assert fold(result.events)[ref].exists


def test_checkpoints_are_derived_assertions_that_agree_with_the_events() -> None:
    conn = FakeConnection(
        beads=[bead("a", "2026-10-01T00:00:00Z", phase="in_progress")],
        bead_labels=[row("bh-a|x", "2026-10-02T00:00:00Z", bead_id="bh-a", label="x")],
    )
    plain = read_history(conn, extracted_at=EXTRACTED)
    with_marks = read_history(conn, extracted_at=EXTRACTED, checkpoints=True)
    assert plain.checkpoints == () and len(with_marks.checkpoints) == 1
    mark = with_marks.checkpoints[0]
    assert (str(mark.provenance), mark.source.system) == ("derived", SOURCE_SYSTEM)
    assert mark.data["state"]["labels"] == ("x",) and mark.data["state"]["phase"] == "in_progress"
    last = max(with_marks.events, key=ordering_key)
    assert mark.data["covers_through"]["id"] == last.id
    # Folding with the checkpoint changes nothing: it is an assertion, not a seed.
    assert fold(with_marks.all_events) == fold(with_marks.events)


# -- the baseline archive ------------------------------------------------------------------


def test_group_directories_are_archive_safe_and_unique() -> None:
    groups = group_dirs([GROUP, OTHER, "Weird Group!", "weird-group-"])
    assert [g.id for g in groups] == sorted({GROUP, OTHER, "Weird Group!", "weird-group-"})
    dirs = [g.dir for g in groups]
    assert len(set(dirs)) == len(dirs)
    assert all(d and d[0].isalnum() and d == d.lower() and " " not in d for d in dirs)


def test_the_baseline_archive_verifies_and_reads_back_to_the_same_events(tmp_path: Any) -> None:
    conn = FakeConnection(
        beads=[
            bead("a", "2026-10-01T00:00:00Z", phase="closed"),
            bead("b", "2026-10-02T00:00:00Z"),
        ],
        bead_labels=[row("bh-a|x", "2026-10-02T00:00:00Z", bead_id="bh-a", label="x")],
    )
    result = read_state(conn, extracted_at=EXTRACTED, checkpoints=True)
    path = tmp_path / "baseline.bdlg"
    manifest = write_baseline(path, result, description="xtdb baseline")
    assert manifest.created_at == EXTRACTED
    with ArchiveReader(path) as reader:
        reader.verify(ids=True)
        assert {e.id for e in reader.events()} == {e.id for e in result.all_events}
        assert manifest.generator.name == "beadlog-xtdb"
    again = tmp_path / "again.bdlg"
    write_baseline(
        again,
        read_state(conn, extracted_at=EXTRACTED, checkpoints=True),
        description="xtdb baseline",
    )
    assert path.read_bytes() == again.read_bytes()  # same store, same bytes


def test_an_empty_read_has_nothing_to_archive(tmp_path: Any) -> None:
    with pytest.raises(ReaderError, match="nothing to write"):
        write_baseline(tmp_path / "x.bdlg", read_state(FakeConnection(), extracted_at=EXTRACTED))


def test_reading_never_writes() -> None:
    conn = FakeConnection(beads=[bead("a", "2026-10-01T00:00:00Z")])
    read_history(conn, extracted_at=EXTRACTED)
    read_state(conn, extracted_at=EXTRACTED)
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in conn.statements)
