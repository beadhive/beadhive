"""Round trips against a real XTDB node (opt-in, integration).

Skipped unless ``BEADLOG_XTDB_TEST_DSN`` names a node, for example a throwaway single-node
container started by the operator::

    docker run -d --name xtdb-test --memory 2g --user "$(id -u):$(id -g)" \\
      -p 127.0.0.1:15432:5432 -v /data/<real-disk>/xtdb:/var/lib/xtdb \\
      ghcr.io/xtdb/xtdb@sha256:<pinned digest>
    BEADLOG_XTDB_TEST_DSN="host=127.0.0.1 port=15432 user=xtdb dbname=xtdb" pytest -m integration

The data directory must be on real disk (never tmpfs). Every bead id and table row carries a
per-run token, so the tests need no clean-up beyond stopping the container, and none depends on
wall-clock timing.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from beadlog import BeadRef, fold, mint_event
from beadlog_xtdb import (
    BEAD_EDGES,
    BEADS,
    ColumnTypes,
    Instant,
    Layout,
    TransitParam,
    connect,
    decode_bead_row,
    find_dangling_edges,
    insert_records,
    require_supported_version,
    state_rows,
)

pytestmark = pytest.mark.integration

DSN = os.environ.get("BEADLOG_XTDB_TEST_DSN", "")
GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"


@pytest.fixture
def conn() -> Any:
    if not DSN:
        pytest.skip("BEADLOG_XTDB_TEST_DSN is not set (no XTDB node to test against)")
    try:
        connection = connect(DSN, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connect failure means no usable node
        pytest.skip(f"no XTDB node reachable at BEADLOG_XTDB_TEST_DSN: {exc}")
    try:
        yield connection
    finally:
        connection.close()


def test_the_node_passes_the_version_gate(conn: Any) -> None:
    assert require_supported_version(conn).release[:2] == (2, 2)


def test_a_bead_round_trips_through_xtdb_field_equal(conn: Any) -> None:
    token = uuid.uuid4().hex[:8]
    ref = BeadRef(GROUP, f"it-{token}")
    other = BeadRef(GROUP, f"it-{token}-t")
    events = [
        mint_event(
            "create",
            ref,
            {
                "type": "gate",
                "fields": {
                    "title": "~tilde ^caret `tick — 中文",
                    "priority": 2,
                    "due_at": "2026-12-01T09:30:00.123456Z",
                    "await_type": "human",
                    "metadata.git.commits": '["abc"]',
                },
            },
            occurred_at="2026-10-01T10:00:00Z",
            recorded_at="2026-10-01T10:00:01Z",
            extracted_at="2026-10-31T00:00:00Z",
            source=("it", "create"),
            provenance="observed",
        ),
        mint_event(
            "label_add",
            ref,
            {"label": "~odd|label"},
            occurred_at="2026-10-02T10:00:00Z",
            recorded_at="2026-10-02T10:00:01Z",
            extracted_at="2026-10-31T00:00:00Z",
            source=("it", "label"),
            provenance="observed",
        ),
        mint_event(
            "edge_add",
            ref,
            {"type": "blocks", "target": other.to_json()},
            occurred_at="2026-10-03T10:00:00Z",
            recorded_at="2026-10-03T10:00:01Z",
            extracted_at="2026-10-31T00:00:00Z",
            source=("it", "edge"),
            provenance="observed",
        ),
    ]
    state = fold(events)[ref]
    rows = state_rows(Layout(), state, ColumnTypes(), valid_from="2026-10-01T10:00:00Z")
    insert_records(conn, BEADS, [rows.bead])
    insert_records(conn, BEAD_EDGES, rows.edges)

    row = conn.execute(f"SELECT * FROM {BEADS} WHERE _id = %s", [ref.id]).fetchone()
    columns = [
        d.name for d in conn.execute(f"SELECT * FROM {BEADS} WHERE _id = %s", [ref.id]).description
    ]
    decoded = decode_bead_row(dict(zip(columns, row, strict=True)))
    assert decoded.bead == ref and decoded.type == "gate"
    assert decoded.fields == dict(sorted(state.fields.items()))

    dangling = find_dangling_edges(conn)
    assert (ref.id, "blocks", other.id) in {(e.from_id, e.type, e.to_id) for e in dangling.edges}


def test_a_set_and_nested_instants_are_typed_on_read(conn: Any) -> None:
    key = f"it-nested-{uuid.uuid4().hex[:8]}"
    doc = {
        "_id": key,
        "labels": frozenset({"a", "~b"}),
        "deps": [{"created_at": Instant.parse("2026-10-22T00:06:00Z")}],
    }
    conn.execute("INSERT INTO beadlog_xtdb_it RECORDS %s", [TransitParam(doc)])
    labels, deps = conn.execute(
        "SELECT labels, deps FROM beadlog_xtdb_it WHERE _id = %s", [key]
    ).fetchone()
    assert labels == frozenset({"a", "~b"})
    assert deps == [{"created_at": datetime(2026, 10, 22, 0, 6, tzinfo=UTC)}]
