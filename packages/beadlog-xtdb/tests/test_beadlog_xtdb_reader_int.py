"""Load -> read-back round trip against a real XTDB node (opt-in, integration).

Skipped unless ``BEADLOG_XTDB_TEST_DSN`` names a node; see ``test_beadlog_xtdb_int.py`` for the
container to start (single node, ``--memory 2g``, ``127.0.0.1`` bind, real-disk data directory,
pinned image digest, GET-based healthcheck).

The loader (bh-mil0x) is a separate bead, so this test writes the fixture log with plain
statements: a full ``beads`` document version per change (``_valid_from`` = ``occurred_at``), one
row per label, edge and comment, and ``DELETE ... FOR PORTION OF VALID_TIME`` for a removal.
Every write is its own wall-clock transaction in valid-time order, so the reader is verified
independently of the loader. Nothing here waits on or compares to the clock: the system-time
cut is the ``system_time`` XTDB itself recorded for a transaction (``xt.txs``).

Each run uses a fresh group id and fresh bead ids, so rows of earlier runs (and of the other
integration test) are never read.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

import pytest

from beadlog import (
    ArchiveReader,
    BeadEvent,
    BeadRef,
    BeadState,
    Edge,
    fold,
    mint_event,
    ordering_key,
    read_archive,
)
from beadlog_xtdb import (
    BEAD_COMMENTS,
    BEAD_EDGES,
    BEAD_LABELS,
    BEADS,
    ColumnTypes,
    Instant,
    Layout,
    bead_row,
    comment_row,
    connect,
    edge_row,
    insert_records,
    label_row,
    read_history,
    read_state,
    require_supported_version,
    write_baseline,
)

pytestmark = pytest.mark.integration

DSN = os.environ.get("BEADLOG_XTDB_TEST_DSN", "")
EXTRACTED = "2026-10-31T00:00:00Z"
FAR_FUTURE = "2100-01-01T00:00:00Z"
ANCIENT = "2000-01-01T00:00:00Z"
# Three valid-time cuts: after the first create, in the middle, and after everything.
CUTS = ("2026-10-02T12:00:00Z", "2026-10-05T12:00:00.5Z", "2026-10-09T00:00:00Z")


@pytest.fixture
def conn() -> Iterator[Any]:
    if not DSN:
        pytest.skip("BEADLOG_XTDB_TEST_DSN is not set (no XTDB node to test against)")
    try:
        connection = connect(DSN, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connect failure means no usable node
        pytest.skip(f"no XTDB node reachable at BEADLOG_XTDB_TEST_DSN: {exc}")
    try:
        require_supported_version(connection)
        yield connection
    finally:
        connection.close()


def fixture_log(group: str) -> list[BeadEvent]:
    """Three beads over eight days: fields, a clear, labels (one re-added), an edge removed,
    a comment, a phase, a retirement and a delete."""
    token = uuid.uuid4().hex[:8]  # ids are not group-qualified in a single-group store
    a, b, c = (BeadRef(group, f"bh-{token}-{name}") for name in "abc")
    sources = iter(range(1000))

    def event(kind: str, bead: BeadRef, data: Mapping[str, Any], at: str, **kw: Any) -> BeadEvent:
        return mint_event(
            kind,
            bead,
            data,
            occurred_at=at,
            recorded_at=at,
            extracted_at=EXTRACTED,
            source=("fixture", f"e{next(sources)}"),
            provenance="observed",
            **kw,
        )

    return [
        event(
            "create",
            a,
            {
                "type": "task",
                "fields": {
                    "title": "~tilde ^caret `tick — 中文",
                    "priority": 2,
                    "description": "first",
                    "due_at": "2026-12-01T09:30:00.123456Z",
                    "metadata.git.commits": '["abc"]',
                },
            },
            "2026-10-01T10:00:00Z",
        ),
        event("label_add", a, {"label": "area:core"}, "2026-10-02T09:00:00Z"),
        event("create", b, {"type": "bug", "fields": {"title": "B"}}, "2026-10-02T10:00:00Z"),
        event("set", a, {"field": "title", "value": "A renamed"}, "2026-10-03T08:00:00Z"),
        event(
            "edge_add",
            a,
            {"type": "blocks", "target": b.to_json()},
            "2026-10-03T09:00:00.25Z",
        ),
        event("create", c, {"type": "chore", "fields": {"title": "C"}}, "2026-10-03T10:00:00Z"),
        event("comment", a, {"body": "looks ~odd"}, "2026-10-04T11:00:00Z", actor="alice"),
        event("phase", a, {"phase": "in_progress"}, "2026-10-04T12:00:00Z"),
        event("label_remove", a, {"label": "area:core"}, "2026-10-05T09:00:00Z"),
        event("set", a, {"field": "description", "value": None}, "2026-10-05T10:00:00Z"),
        event("set", a, {"field": "priority", "value": 1}, "2026-10-05T11:00:00Z"),
        event("edge_remove", a, {"type": "blocks", "target": b.to_json()}, "2026-10-06T09:00:00Z"),
        event("label_add", a, {"label": "area:core"}, "2026-10-06T10:00:00Z"),
        event("retire", b, {"successor": a.to_json(), "reason": "merged"}, "2026-10-07T09:00:00Z"),
        event("delete", c, {"reason": "mistake"}, "2026-10-07T10:00:00Z"),
        event("phase", a, {"phase": "closed", "reason": "done"}, "2026-10-08T09:00:00Z"),
    ]


def write_log(conn: Any, events: list[BeadEvent], group: str) -> list[datetime]:
    """Write ``events`` one wall-clock transaction each, in valid-time order.

    Returns, per event, the ``system_time`` XTDB recorded for its transaction.
    """
    layout, types = Layout(), ColumnTypes()
    applied: list[BeadEvent] = []
    stamps: list[datetime] = []
    for event in sorted(events, key=ordering_key):
        applied.append(event)
        moment = Instant.parse(event.occurred_at or "")
        kind, data, bead = str(event.kind), event.data, event.bead
        if kind in ("create", "set", "phase", "retire", "delete"):
            state = fold(applied)[bead]
            insert_records(conn, BEADS, [bead_row(layout, state, types, valid_from=moment)])
        elif kind == "label_add":
            insert_records(
                conn, BEAD_LABELS, [label_row(layout, bead, data["label"], valid_from=moment)]
            )
        elif kind == "label_remove":
            conn.execute(
                f"DELETE FROM {BEAD_LABELS} FOR PORTION OF VALID_TIME FROM %s TO NULL "
                "WHERE _id = %s",
                [moment.at, layout.label_id(bead, data["label"])],
            )
        elif kind in ("edge_add", "edge_remove"):
            target = BeadRef(data["target"]["group"], data["target"]["id"])
            edge = Edge(data["type"], target)
            if kind == "edge_add":
                insert_records(
                    conn, BEAD_EDGES, [edge_row(layout, bead, edge, types, valid_from=moment)]
                )
            else:
                conn.execute(
                    f"DELETE FROM {BEAD_EDGES} FOR PORTION OF VALID_TIME FROM %s TO NULL "
                    "WHERE _id = %s",
                    [moment.at, layout.edge_id(bead, edge.type, target)],
                )
        elif kind == "comment":
            comment = fold(applied)[bead].comments[-1]
            insert_records(conn, BEAD_COMMENTS, [comment_row(layout, bead, comment, types)])
        else:  # pragma: no cover - the fixture uses no other kind
            raise AssertionError(kind)
        stamps.append(
            conn.execute("SELECT system_time FROM xt.txs ORDER BY _id DESC LIMIT 1").fetchone()[0]
        )
    return stamps


def shape(states: Mapping[BeadRef, BeadState]) -> dict[BeadRef, dict[str, Any]]:
    """A folded state as comparable plain data. A re-read comment is a new event (new id), so
    comments compare by author, body and valid time."""
    return {
        ref: {
            "exists": state.exists,
            "deleted": state.deleted,
            "type": state.type,
            "phase": state.phase,
            "fields": dict(state.fields),
            "labels": list(state.labels),
            "edges": [edge.to_json() for edge in state.edges],
            "retired": dict(state.retired) if state.retired is not None else None,
            "comments": [(c.actor, c.body, c.occurred_at) for c in state.comments],
        }
        for ref, state in sorted(states.items())
    }


def assert_same(actual: Mapping[BeadRef, Any], expected: Mapping[BeadRef, Any]) -> None:
    """Compare per bead and per facet so a failure names what differs."""
    assert sorted(actual) == sorted(expected)
    for ref in expected:
        for facet in expected[ref]:
            assert actual[ref][facet] == expected[ref][facet], (ref.id, facet)


@pytest.fixture
def loaded(conn: Any) -> tuple[Any, str, list[BeadEvent], list[datetime]]:
    group = f"urn:uuid:{uuid.uuid4()}"
    events = fixture_log(group)
    stamps = write_log(conn, events, group)
    return conn, group, events, stamps


@pytest.mark.parametrize("cut", CUTS)
def test_state_read_folds_to_the_loaded_log_at_a_valid_time_cut(
    loaded: tuple[Any, str, list[BeadEvent], list[datetime]], cut: str
) -> None:
    conn, group, events, _ = loaded
    expected = shape(fold(events, at=cut))
    assert expected, "the fixture must have beads at every cut"
    result = read_state(conn, extracted_at=EXTRACTED, valid_at=cut, groups=[group])
    assert {str(e.provenance) for e in result.events} == {"observed"}
    assert {e.source.system for e in result.events} == {"xtdb"}
    assert_same(shape(fold(result.events)), expected)


@pytest.mark.parametrize("cut", CUTS)
def test_history_read_folds_to_the_loaded_log_at_a_valid_time_cut(
    loaded: tuple[Any, str, list[BeadEvent], list[datetime]], cut: str
) -> None:
    conn, group, events, _ = loaded
    result = read_history(conn, extracted_at=EXTRACTED, groups=[group])
    assert_same(shape(fold(result.events, at=cut)), shape(fold(events, at=cut)))
    assert_same(shape(fold(result.events)), shape(fold(events)))


def test_history_dates_each_change_with_valid_and_recorded_time(
    loaded: tuple[Any, str, list[BeadEvent], list[datetime]],
) -> None:
    conn, group, events, stamps = loaded
    result = read_history(conn, extracted_at=EXTRACTED, groups=[group])
    ordered = sorted(events, key=ordering_key)
    recorded = {
        (e.kind, e.bead, e.occurred_at): stamp for e, stamp in zip(ordered, stamps, strict=True)
    }
    seen = {(e.kind, e.bead, e.occurred_at): e for e in result.events}
    # Every label and edge change comes back at its own valid time, recorded when written.
    for key, stamp in recorded.items():
        kind = str(key[0])
        if kind in ("label_add", "label_remove", "edge_add", "edge_remove", "comment"):
            read = seen[key]
            assert read.recorded_at is not None
            assert Instant.parse(read.recorded_at).at == stamp, key
    # A document change is recorded when its version was written (the `create` shares an
    # instant with nothing here, so no microsecond shift is reported).
    assert result.note_codes() == {}


@pytest.mark.parametrize("keep", [3, 8, 14])
def test_system_time_cut_reads_the_store_as_it_was_recorded(
    loaded: tuple[Any, str, list[BeadEvent], list[datetime]], keep: int
) -> None:
    conn, group, events, stamps = loaded
    ordered = sorted(events, key=ordering_key)
    expected = shape(fold(ordered[:keep]))
    # State as recorded at the keep-th transaction, valid time at its far end.
    result = read_state(
        conn,
        extracted_at=EXTRACTED,
        system_at=stamps[keep - 1],
        valid_at=FAR_FUTURE,
        groups=[group],
    )
    assert_same(shape(fold(result.events)), expected)
    # The same cut through the history read.
    history = read_history(conn, extracted_at=EXTRACTED, system_at=stamps[keep - 1], groups=[group])
    assert_same(shape(fold(history.events)), expected)


def test_system_time_cuts_outside_the_recording_are_empty_or_complete(
    loaded: tuple[Any, str, list[BeadEvent], list[datetime]],
) -> None:
    conn, group, events, _ = loaded
    before = read_state(
        conn, extracted_at=EXTRACTED, system_at=ANCIENT, valid_at=FAR_FUTURE, groups=[group]
    )
    assert before.events == ()
    after = read_state(
        conn, extracted_at=EXTRACTED, system_at=FAR_FUTURE, valid_at=FAR_FUTURE, groups=[group]
    )
    assert_same(shape(fold(after.events)), shape(fold(events)))


def test_a_read_is_reproducible_and_a_baseline_archive_verifies(
    loaded: tuple[Any, str, list[BeadEvent], list[datetime]], tmp_path: Any
) -> None:
    conn, group, events, _ = loaded
    first = read_history(conn, extracted_at=EXTRACTED, groups=[group], checkpoints=True)
    second = read_history(conn, extracted_at=EXTRACTED, groups=[group], checkpoints=True)
    assert first.all_events == second.all_events
    path = tmp_path / "baseline.bdlg"
    write_baseline(path, first)
    with ArchiveReader(path) as reader:
        reader.verify(ids=True)
    _, reread = read_archive(path)
    assert_same(shape(fold(reread)), shape(fold(events)))
