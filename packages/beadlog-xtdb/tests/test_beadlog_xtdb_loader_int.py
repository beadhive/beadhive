"""The XTDB loader against a real XTDB 2.2.x node (opt-in, integration).

Skipped unless ``BEADLOG_XTDB_TEST_DSN`` names a node, for example a throwaway single-node
container started by the operator::

    docker run -d --name xtdb-test --memory 2g --user "$(id -u):$(id -g)" \\
      -p 127.0.0.1:15440:5432 -v /data/<real-disk>/xtdb:/var/lib/xtdb \\
      --health-cmd 'wget -q -O /dev/null http://127.0.0.1:8080/healthz/alive' \\
      ghcr.io/xtdb/xtdb@sha256:a6a426505b95a36e4e0e1d0de88efcf4220cb6bde8f1f5e594b0a1f92a948a77
    BEADLOG_XTDB_TEST_DSN="host=127.0.0.1 port=15440 user=xtdb dbname=xtdb" pytest -m integration

A monotone load needs a database with no later transaction, so every test (and every store the
conformance kit asks for) gets its own ``ATTACH DATABASE`` with a fresh clock (``bh-3jp2g``
S13); the node itself needs no clean-up beyond stopping the container. No test depends on
wall-clock timing: the fixture's times are fixed and in the past.
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from beadlog import (
    BeadEvent,
    BeadRef,
    Generator,
    Group,
    fold,
    format_timestamp,
    mint_event,
    testing,
    write_archive,
)
from beadlog_xtdb import (
    FutureRecordedTimeError,
    LoadAborted,
    LoadMode,
    MonotoneOrderError,
    TargetNotFreshError,
    XtdbStoreLoader,
    connect,
    decode_bead_row,
    load_archive,
)

pytestmark = pytest.mark.integration

DSN = os.environ.get("BEADLOG_XTDB_TEST_DSN", "")
GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"
EXTRACTED = "2026-10-31T00:00:00Z"
SECRET = "content that must not survive"


def _dsn_for(database: str) -> str:
    parts = [part for part in DSN.split() if not part.startswith("dbname=")]
    return " ".join([*parts, f"dbname={database}"])


@pytest.fixture
def fresh_db() -> Iterator[Callable[[], Any]]:
    """A factory of connections, each to its own freshly attached database."""
    if not DSN:
        pytest.skip("BEADLOG_XTDB_TEST_DSN is not set (no XTDB node to test against)")
    try:
        admin = connect(DSN, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connect failure means no usable node
        pytest.skip(f"no XTDB node reachable at BEADLOG_XTDB_TEST_DSN: {exc}")
    opened: list[Any] = [admin]

    def make() -> Any:
        name = "bdlg_" + uuid.uuid4().hex[:12]
        admin.execute(f"ATTACH DATABASE {name}")
        connection = connect(_dsn_for(name), autocommit=True)
        opened.append(connection)
        return connection

    try:
        yield make
    finally:
        for connection in opened:
            connection.close()


# -- the conformance kit ----------------------------------------------------------------------


@pytest.mark.parametrize("driver", [testing.load_archive, load_archive], ids=["kit", "index"])
@pytest.mark.parametrize("case", testing.conformance_cases(), ids=lambda case: case.name)
def test_conformance(fresh_db: Callable[[], Any], case: Any, driver: Any) -> None:
    case.run(lambda: XtdbStoreLoader(fresh_db()), driver)


# -- the fixture log ----------------------------------------------------------------------------


def _ref(name: str) -> BeadRef:
    return BeadRef(GROUP, name)


def _ev(kind: str, name: str, data: dict[str, Any], occurred: str | None, recorded: str | None):
    return mint_event(
        kind,
        _ref(name),
        data,
        occurred_at=occurred,
        recorded_at=recorded,
        extracted_at=EXTRACTED,
        source=("it", f"{kind}:{name}:{occurred}:{recorded}:{sorted(data)}"),
        provenance="observed",
        actor="dev/it",
    )


def fixture_events() -> tuple[list[BeadEvent], BeadEvent]:
    """Ties, a same-instant same-entity write, a backdated correction, a null recorded_at
    (before an existing version) and a tombstone of a comment; returns (events, secret)."""
    t1, r1 = "2026-03-01T10:00:00Z", "2026-03-01T10:00:01Z"
    t2, r2 = "2026-03-02T10:00:00Z", "2026-03-02T10:00:01Z"
    t3, r3 = "2026-03-03T10:00:00Z", "2026-03-03T10:00:01Z"
    t0, t2_late, t6 = "2026-02-15T00:00:00Z", "2026-03-02T12:00:00Z", "2026-03-06T10:00:00Z"
    secret = _ev("comment", "x", {"body": SECRET}, "2026-03-04T11:00:00Z", "2026-03-04T11:00:01Z")
    events = [
        _ev("create", "x", {"type": "task", "fields": {"title": "X", "priority": 2}}, t1, r1),
        _ev("create", "y", {"type": "bug", "fields": {"title": "Y"}}, t1, r1),  # tie
        _ev("set", "x", {"field": "title", "value": "X renamed"}, t2, r2),  # same instant,
        _ev("set", "x", {"field": "priority", "value": 0}, t2, r2),  # same entity: split
        _ev("label_add", "x", {"label": "area:a"}, t2, r2),
        _ev("phase", "y", {"phase": "closed", "reason": "done"}, t3, r3),
        _ev("edge_add", "x", {"type": "blocks", "target": _ref("y").to_json()}, t3, r3),
        _ev("comment", "x", {"body": "kept"}, "2026-03-04T10:00:00Z", "2026-03-04T10:00:01Z"),
        secret,
        _ev(
            "tombstone",
            "x",
            {"scope": "events", "events": [secret.id], "reason": "pii"},
            "2026-03-05T10:00:00Z",
            "2026-03-05T10:00:01Z",
        ),
        _ev("set", "z", {"field": "title", "value": "Z late"}, t6, "2026-03-06T10:00:01Z"),
        _ev("label_remove", "x", {"label": "area:a"}, t6, "2026-03-06T10:00:02Z"),
        # recorded a day later, valid earlier: rewrites y's later version with an explicit end
        _ev("set", "y", {"field": "assignee", "value": "dev/b"}, t2_late, "2026-03-07T00:00:00Z"),
        # null recorded_at, valid before z's backfilled version: the wall-clock tail
        _ev("create", "z", {"type": "task", "fields": {"title": "Z", "priority": 1}}, t0, None),
    ]  # fmt: skip
    return events, secret


def fixture_archive(events: list[BeadEvent]) -> bytes:
    buffer = io.BytesIO()
    write_archive(
        buffer, events, groups=[Group(GROUP, "it")], generator=Generator("it", "0.1.0"), index=True
    )
    return buffer.getvalue()


def _iso(moment: datetime) -> str:
    return format_timestamp(moment)


def read_cut(
    conn: Any, *, valid: datetime | None = None, system: datetime | None = None
) -> dict[str, Any]:
    clause, params = "", []
    if system is not None:
        clause, params = clause + " FOR SYSTEM_TIME AS OF %s", [*params, system]
    if valid is not None:
        clause, params = clause + " FOR VALID_TIME AS OF %s", [*params, valid]
    cursor = conn.execute(f"SELECT * FROM beads{clause}", params)
    columns = [column.name for column in cursor.description]
    beads = {}
    for row in cursor.fetchall():
        decoded = decode_bead_row(dict(zip(columns, row, strict=True)))
        beads[decoded.bead.id] = (decoded.type, decoded.phase, decoded.fields, decoded.deleted)
    labels = set(conn.execute(f"SELECT bead_id, label FROM bead_labels{clause}", params).fetchall())
    edges = set(
        conn.execute(f"SELECT from_id, type, to_id FROM bead_edges{clause}", params).fetchall()
    )
    comments = set(
        conn.execute(f"SELECT bead_id, _id FROM bead_comments{clause}", params).fetchall()
    )
    return {"beads": beads, "labels": labels, "edges": edges, "comments": comments}


def expected_cut(
    events: list[BeadEvent], *, valid: datetime | None = None, system: datetime | None = None
) -> dict[str, Any]:
    states = fold(
        events,
        at=_iso(valid) if valid is not None else None,
        recorded_as_of=_iso(system) if system is not None else None,
    )
    beads, labels, edges, comments = {}, set(), set(), set()
    for ref, state in states.items():
        if state.erased:
            continue
        if state.exists or state.deleted or state.type or state.phase or state.fields:
            fields = {name: value for name, value in state.fields.items() if value is not None}
            beads[ref.id] = (state.type, state.phase, dict(sorted(fields.items())), state.deleted)
        labels |= {(ref.id, label) for label in state.labels}
        edges |= {(ref.id, edge.type, edge.target.id) for edge in state.edges}
        comments |= {(ref.id, comment.id) for comment in state.comments}
    return {"beads": beads, "labels": labels, "edges": edges, "comments": comments}


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


VALID_CUTS = [
    "2026-02-20T00:00:00Z",  # only the tail's z
    "2026-03-01T12:00:00Z",
    "2026-03-02T11:00:00Z",  # after both split versions, before y's backdated assignee
    "2026-03-02T13:00:00Z",
    "2026-03-04T12:00:00Z",
    "2026-03-10T00:00:00Z",
]
SYSTEM_CUTS = [
    "2026-03-01T10:00:01.5Z",
    "2026-03-02T10:00:02Z",  # after the split transactions at r2 + 1 µs
    "2026-03-06T12:00:00Z",  # before the backdated correction was recorded
    "2026-03-08T00:00:00Z",  # after it, before the wall-clock tail
]


def test_fixture_log_loads_with_every_guard_and_matches_the_fold_on_both_axes(
    fresh_db: Callable[[], Any],
) -> None:
    events, secret = fixture_events()
    loaded = [event for event in events if event.id != secret.id]  # the writer drops it
    conn = fresh_db()
    store = XtdbStoreLoader(conn)
    report = load_archive(store, io.BytesIO(fixture_archive(events)))
    assert store.last_plan is not None and store.last_plan.mode is LoadMode.INITIAL

    codes = {(loss["code"], loss["member"]) for loss in report["losses"]}
    assert ("x-xtdb.us-split", "/occurred_at") in codes
    assert ("x-xtdb.us-split", "/recorded_at") in codes
    assert ("recorded-time.null", "/recorded_at") in codes
    assert ("erasure.incomplete", "") in codes
    assert report["summary"]["dropped"] == 0

    txs = conn.execute("SELECT system_time, committed FROM xt.txs ORDER BY _id").fetchall()
    assert all(committed for _, committed in txs)
    backfill = [moment for moment, _ in txs if moment < datetime(2026, 4, 1, tzinfo=UTC)]
    assert backfill[0] == _at("2026-03-01T10:00:01Z")  # recorded time preserved exactly
    assert _at("2026-03-02T10:00:01.000001Z") in backfill  # the split transaction
    assert backfill == sorted(set(backfill))  # strictly increasing
    assert len(txs) - len(backfill) == 2  # the null-recorded tail, then the ERASE

    for valid in VALID_CUTS:
        assert read_cut(conn, valid=_at(valid)) == expected_cut(loaded, valid=_at(valid)), valid
    for system in SYSTEM_CUTS:
        for valid in VALID_CUTS:
            got = read_cut(conn, valid=_at(valid), system=_at(system))
            assert got == expected_cut(loaded, valid=_at(valid), system=_at(system)), (
                system,
                valid,
            )
        got = read_cut(conn, system=_at(system))
        assert got == expected_cut(loaded, system=_at(system)), system
    assert read_cut(conn) == expected_cut(loaded)

    # the split kept the intermediate version (title renamed, priority still 2) for one µs
    rows = conn.execute(
        "SELECT title, priority, _valid_from FROM beads FOR ALL VALID_TIME WHERE _id = 'x' "
        "ORDER BY _valid_from"
    ).fetchall()
    assert [(title, priority) for title, priority, _ in rows[:3]] == [
        ("X", 2),
        ("X renamed", 2),
        ("X renamed", 0),
    ]
    assert {event.id for event in store.export()} == {event.id for event in loaded}


def test_refusals_happen_before_any_write(fresh_db: Callable[[], Any]) -> None:
    events, secret = fixture_events()
    loaded = [event for event in events if event.id != secret.id]

    def tx_count(conn: Any) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM xt.txs").fetchone()[0])

    unordered = fresh_db()
    order = [event.id for event in sorted(loaded, key=lambda e: e.id)]
    with pytest.raises(MonotoneOrderError):
        XtdbStoreLoader(unordered).load(loaded, dry_run=False, order=order)
    assert tx_count(unordered) == 0

    early = fresh_db()
    in_the_past = XtdbStoreLoader(early, clock=lambda: datetime(2026, 3, 3, tzinfo=UTC))
    with pytest.raises(FutureRecordedTimeError):
        in_the_past.load(loaded, dry_run=False)
    assert tx_count(early) == 0

    full = fresh_db()
    XtdbStoreLoader(full).load(loaded, dry_run=False)
    before = tx_count(full)
    with pytest.raises(TargetNotFreshError):
        XtdbStoreLoader(full, mode="initial").load(loaded, dry_run=False)
    assert tx_count(full) == before


class _Intruder:
    """A connection whose database gets a wall-clock write after the second COMMIT, so the
    next backfill transaction genuinely aborts (``bh-3jp2g`` S2: not later than current tx)."""

    def __init__(self, inner: Any, other: Any) -> None:
        self._inner, self._other, self._commits = inner, other, 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, sql: str, params: Any = None) -> Any:
        result = self._inner.execute(sql, params)
        if sql == "COMMIT":
            self._commits += 1
            if self._commits == 2:
                self._other.execute("INSERT INTO intruder RECORDS {_id: 'wall-clock'}")
        return result


def test_an_abort_fails_with_discard_and_reload_never_partial_success(
    fresh_db: Callable[[], Any],
) -> None:
    events, secret = fixture_events()
    loaded = [event for event in events if event.id != secret.id]
    conn = fresh_db()
    other = connect(conn.info.dsn, autocommit=True)
    try:
        store = XtdbStoreLoader(_Intruder(conn, other), target="xtdb://it/intruded")
        with pytest.raises(LoadAborted, match="Discard this database") as caught:
            store.load(loaded, dry_run=False)
    finally:
        other.close()
    assert "transaction 3 of" in str(caught.value)
    aborted = conn.execute("SELECT COUNT(*) FROM xt.txs WHERE committed = false").fetchone()[0]
    assert aborted == 1


def test_incremental_tombstone_erases_earlier_content_from_every_read(
    fresh_db: Callable[[], Any],
) -> None:
    events, secret = fixture_events()
    base = [event for event in events if event.kind.value != "tombstone"]  # secret is loaded
    conn = fresh_db()
    store = XtdbStoreLoader(conn)
    store.load(base, dry_run=False)
    renamed = next(e for e in base if e.data.get("value") == "X renamed")
    erase = _ev(
        "tombstone",
        "x",
        {"scope": "events", "events": [secret.id, renamed.id], "reason": "pii"},
        "2026-03-09T10:00:00Z",
        "2026-03-09T10:00:01Z",
    )
    report = store.load([erase], dry_run=False)
    assert store.last_plan is not None and store.last_plan.mode is LoadMode.INCREMENTAL
    assert [loss["code"] for loss in report["losses"]] == [
        "erasure.incomplete",
        "recorded-time.reassigned",
    ]
    everywhere = "FOR ALL SYSTEM_TIME FOR ALL VALID_TIME"
    assert (
        conn.execute(
            f"SELECT _id FROM bead_comments {everywhere} WHERE _id = %s", [secret.id]
        ).fetchall()
        == []
    )
    assert (
        conn.execute(
            f"SELECT _id FROM bead_events {everywhere} WHERE _id = %s", [secret.id]
        ).fetchall()
        == []
    )
    titles = {
        row[0]
        for row in conn.execute(f"SELECT title FROM beads {everywhere} WHERE _id = 'x'").fetchall()
    }
    assert "X renamed" not in titles and "X" in titles  # fmt: skip
    survivors = [*[e for e in base if e.id not in {secret.id, renamed.id}], erase]
    for valid in VALID_CUTS:
        assert read_cut(conn, valid=_at(valid)) == expected_cut(survivors, valid=_at(valid)), valid
