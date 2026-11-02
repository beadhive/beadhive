"""The XTDB loader against a fake connection: every guard, the op mapping and the loss report.

No XTDB is needed. The fake answers the loader's few read queries (version, ``xt.txs``,
``information_schema``, the journal), records every write statement, can fail a chosen
statement, and keeps journal rows it saw committed so an incremental load can be tested. Time
comes from an injected clock; nothing depends on the wall clock.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from beadlog import (
    BeadEvent,
    BeadRef,
    Generator,
    Group,
    fold,
    mint_event,
    ordering_key,
    testing,
    write_archive,
)
from beadlog_xtdb import (
    CAPABILITIES,
    JOURNAL,
    FutureRecordedTimeError,
    Instant,
    LayoutMismatchError,
    LoadAborted,
    LoadMode,
    MonotoneOrderError,
    SplitPreconditionError,
    TargetNotFreshError,
    TransitParam,
    XtdbStoreLoader,
    load_archive,
    plan_load,
    truncate_timestamp,
)

GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"
OTHER_GROUP = "urn:uuid:11111111-2222-4333-8444-555555555555"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
EXTRACTED = "2026-10-31T00:00:00Z"
VERSION_ROW = ("PostgreSQL 16 (XTDB 2.2.0)",)


def clock() -> datetime:
    return NOW


def bead(name: str, group: str = GROUP) -> BeadRef:
    return BeadRef(group, name)


def ev(
    kind: str,
    name: str,
    data: dict[str, Any],
    occurred: str | None,
    recorded: str | None,
    *,
    ref: str | None = None,
    group: str = GROUP,
    **extra: Any,
) -> BeadEvent:
    return mint_event(
        kind,
        bead(name, group),
        data,
        occurred_at=occurred,
        recorded_at=recorded,
        extracted_at=EXTRACTED,
        source=("test", ref or f"{kind}:{name}:{occurred}:{recorded}:{sorted(data)}"),
        provenance=extra.pop("provenance", "observed"),
        actor=extra.pop("actor", "dev/test"),
        **extra,
    )


def create(name: str, occurred: str | None, recorded: str | None, **fields: Any) -> BeadEvent:
    return ev(
        "create", name, {"type": "task", "fields": {"title": name, **fields}}, occurred, recorded
    )


def setf(name: str, field: str, value: Any, occurred: str, recorded: str | None) -> BeadEvent:
    return ev("set", name, {"field": field, "value": value}, occurred, recorded)


def checkpoint_of(history: list[BeadEvent], covered: BeadEvent) -> BeadEvent:
    state = fold(history)[covered.bead]
    return mint_event(
        "checkpoint",
        covered.bead,
        {
            "state": state.to_checkpoint_state(),
            "covers_through": {
                "occurred_at": covered.occurred_at,
                "recorded_at": covered.recorded_at,
                "id": covered.id,
            },
        },
        occurred_at=covered.occurred_at,
        recorded_at=covered.recorded_at,
        extracted_at=EXTRACTED,
        source=("test", f"checkpoint:{covered.id}"),
        provenance="derived",
    )


class Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.description = [object()] if rows else None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class FakeXtdb:
    """Answers the loader's reads, records writes, and can fail the n-th matching statement."""

    def __init__(
        self,
        *,
        latest: datetime | None = None,
        tables: dict[str, bool] | None = None,
        aborted: int = 0,
        fail: Callable[[str, int], bool] | None = None,
    ) -> None:
        self.autocommit = False
        self.latest = latest
        self.latest_id: int | None = None if latest is None else 7
        self.tables = dict(tables or {})
        self.aborted = aborted
        self.fail = fail
        self.statements: list[tuple[str, Any]] = []
        self.journal: dict[str, tuple[str, datetime]] = {}
        self._pending: dict[str, tuple[str, datetime]] = {}
        self._calls = 0

    def execute(self, sql: str, params: Any = None) -> Result:
        self._calls += 1
        if self.fail is not None and self.fail(sql, self._calls):
            raise RuntimeError(f"injected failure at {sql.split()[0]}")
        if sql == "SELECT version()":
            return Result([VERSION_ROW])
        if sql.startswith("SELECT max(system_time)"):
            return Result([(self.latest, self.latest_id)])
        if sql.startswith("SELECT table_name"):
            return Result([(name,) for name in sorted(self.tables)])
        if sql.startswith("SELECT _id FROM"):
            table = sql.split()[3]
            return Result([("x",)] if self.tables.get(table) else [])
        if sql.startswith("SELECT event, valid_at"):
            return Result([value for _, value in sorted(self.journal.items())])
        if sql.startswith("SELECT COUNT(*) FROM xt.txs"):
            return Result([(self.aborted,)])
        self.statements.append((sql, params))
        if sql.startswith("INSERT INTO"):
            table = sql.split()[2]
            self.tables.setdefault(table, False)
            for param in params:
                doc = param.value
                if table == JOURNAL:
                    self._pending[doc["_id"]] = (doc["event"], doc["valid_at"].at)
                self._pending.setdefault(f"__table__{table}", ("", NOW))
        elif sql == "COMMIT":
            for key, value in self._pending.items():
                if key.startswith("__table__"):
                    self.tables[key[len("__table__") :]] = True
                else:
                    self.journal[key] = value
            self._pending = {}
            self.latest, self.latest_id = NOW, (self.latest_id or 0) + 1
        elif sql == "ROLLBACK":
            self._pending = {}
        return Result([])

    # -- views of what was written ---------------------------------------------------------

    def writes(self) -> list[str]:
        return [sql for sql, _ in self.statements]

    def transactions(self) -> list[list[tuple[str, Any]]]:
        out: list[list[tuple[str, Any]]] = []
        for sql, params in self.statements:
            if sql.startswith("BEGIN"):
                out.append([(sql, params)])
            elif out:
                out[-1].append((sql, params))
        return out

    def docs(self, table: str) -> list[dict[str, Any]]:
        out = []
        for sql, params in self.statements:
            if sql.startswith(f"INSERT INTO {table} RECORDS"):
                out += [param.value for param in params if isinstance(param, TransitParam)]
        return out


def loader(fake: FakeXtdb, **options: Any) -> XtdbStoreLoader:
    options.setdefault("clock", clock)
    return XtdbStoreLoader(fake, target="xtdb://test/fake", **options)


def losses(report: dict[str, Any], code: str | None = None) -> list[dict[str, Any]]:
    return [loss for loss in report["losses"] if code is None or loss["code"] == code]


def at(text: str) -> Instant:
    return Instant.parse(text)


# -- the report contract ------------------------------------------------------------------

# A byte-for-byte JSON copy of docs/schemas/beadlog/v0.1/loss-report.schema.json: package tests
# run in a sandbox without docs/, so the schema travels with the test and the drift test below
# compares it with the repo copy whenever the repo is reachable.
LOSS_REPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["loss_report_version", "archive", "loader", "target", "mode", "summary", "losses"],
    "additionalProperties": False,
    "properties": {
        "loss_report_version": {"type": "string", "pattern": "^0\\.1\\.(0|[1-9][0-9]*)$"},
        "archive": {
            "type": "object",
            "required": ["format_version", "manifest_sha256"],
            "additionalProperties": False,
            "properties": {
                "format_version": {"type": "string"},
                "manifest_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
        "loader": {
            "type": "object",
            "required": ["name", "version", "capabilities"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "version": {"type": "string", "minLength": 1},
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[a-z][a-z0-9:._-]*$"},
                    "uniqueItems": True,
                },
            },
        },
        "target": {"type": "string", "minLength": 1},
        "mode": {"enum": ["dry-run", "apply"]},
        "summary": {
            "type": "object",
            "required": ["events", "loaded", "degraded", "dropped"],
            "additionalProperties": False,
            "properties": {
                "events": {"type": "integer", "minimum": 0},
                "loaded": {"type": "integer", "minimum": 0},
                "degraded": {"type": "integer", "minimum": 0},
                "dropped": {"type": "integer", "minimum": 0},
                "by_code": {
                    "type": "object",
                    "additionalProperties": {"type": "integer", "minimum": 0},
                },
            },
        },
        "losses": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["event_id", "code", "member", "disposition"],
                "additionalProperties": False,
                "properties": {
                    "event_id": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "code": {
                        "type": "string",
                        "pattern": "^([a-z][a-z-]*\\.[a-z][a-z-]*|x-[a-z0-9.-]+)$",
                    },
                    "member": {"type": "string", "pattern": "^(/([^~/]|~[01])*)*$"},
                    "disposition": {"enum": ["degraded", "reassigned", "approximated", "dropped"]},
                    "stored_as": {"type": ["string", "null"]},
                    "detail": {"type": "string"},
                },
            },
        },
    },
}
_DESCRIPTIVE = {"$id", "title", "description"}


def _normative(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _normative(v) for k, v in schema.items() if k not in _DESCRIPTIVE}
    if isinstance(schema, list):
        return [_normative(item) for item in schema]
    return schema


def _repo_schema() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs/schemas/beadlog/v0.1/loss-report.schema.json"
        if candidate.is_file():
            return candidate
    return None


def test_the_embedded_loss_report_schema_matches_the_repo_copy() -> None:
    path = _repo_schema()
    if path is None:
        pytest.skip("the repo's docs/ tree is not in this sandbox")
    repo = json.loads(path.read_text(encoding="utf-8"))
    assert _normative(repo) == LOSS_REPORT_SCHEMA


def validate(report: dict[str, Any]) -> None:
    jsonschema.validate(report, LOSS_REPORT_SCHEMA)
    assert report["summary"]["loaded"] + report["summary"]["dropped"] == report["summary"]["events"]


def test_capabilities_declare_erase_logical_and_never_erase() -> None:
    assert "erase-logical" in CAPABILITIES and "erase" not in CAPABILITIES
    assert {"valid-time", "per-element-valid-time", "recorded-time-monotone-load"} <= CAPABILITIES
    assert "recorded-time" not in CAPABILITIES and "metadata-fallback" not in CAPABILITIES


def test_conformance_kit_passes_against_the_fake() -> None:
    def factory() -> XtdbStoreLoader:
        return loader(FakeXtdb())

    testing.assert_conformant(factory)
    testing.assert_conformant(factory, load_archive)


# -- op mapping and the monotone replay -----------------------------------------------------


def test_in_order_history_is_one_statement_per_event_in_its_recorded_at_transaction() -> None:
    events = [
        create("b1", "2026-01-01T10:00:00Z", "2026-01-01T10:00:01Z", assignee="dev/a"),
        ev("label_add", "b1", {"label": "area:x"}, "2026-01-02T10:00:00Z", "2026-01-02T10:00:01Z"),
        ev(
            "label_remove",
            "b1",
            {"label": "area:x"},
            "2026-01-03T10:00:00Z",
            "2026-01-03T10:00:01Z",
        ),
        setf("b1", "assignee", None, "2026-01-04T10:00:00Z", "2026-01-04T10:00:01Z"),
    ]
    fake = FakeXtdb()
    report = loader(fake).load(events, dry_run=False)
    validate(report)
    assert report["losses"] == []
    txs = fake.transactions()
    assert [tx[0][0] for tx in txs] == [
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2026-01-01T10:00:01Z')",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2026-01-02T10:00:01Z')",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2026-01-03T10:00:01Z')",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2026-01-04T10:00:01Z')",
    ]
    assert all(tx[-1][0] == "COMMIT" for tx in txs)
    removal = [(sql, params) for sql, params in fake.statements if sql.startswith("DELETE")]
    assert removal == [
        (
            "DELETE FROM bead_labels FOR PORTION OF VALID_TIME FROM %s TO NULL WHERE _id = %s",
            [datetime(2026, 1, 3, 10, tzinfo=UTC), "b1|area:x"],
        )
    ]
    beads = fake.docs("beads")
    assert [doc["_valid_from"] for doc in beads] == [
        at("2026-01-01T10:00:00Z"),
        at("2026-01-04T10:00:00Z"),
    ]
    assert "_valid_to" not in beads[0] and "_valid_to" not in beads[1]
    assert beads[0]["assignee"] == "dev/a" and "assignee" not in beads[1]  # cleared, no PATCH
    assert not any(sql.startswith(("PATCH", "UPDATE")) for sql in fake.writes())
    journal = fake.docs(JOURNAL)
    assert [doc["_id"] for doc in journal] == [event.id for event in events]
    assert json.loads(journal[0]["event"]) == events[0].to_json()


def test_ties_across_entities_share_one_transaction() -> None:
    events = [
        create("b1", "2026-01-01T10:00:00Z", "2026-01-01T10:00:05Z"),
        create("b2", "2026-01-01T10:00:00Z", "2026-01-01T10:00:05Z"),
    ]
    fake = FakeXtdb()
    report = loader(fake).load(events, dry_run=False)
    assert report["losses"] == []
    assert len(fake.transactions()) == 1
    assert {doc["_id"] for doc in fake.docs("beads")} == {"b1", "b2"}


def test_same_instant_same_entity_writes_split_by_zero_based_k_microseconds() -> None:
    t, r = "2026-01-01T10:00:00Z", "2026-01-01T10:00:05Z"
    first = create("b1", t, r)
    second = setf("b1", "title", "renamed", t, r)
    third = setf("b1", "priority", 2, t, r)
    label = ev("label_add", "b1", {"label": "l"}, t, r)  # another entity: k = 0
    events = [first, second, third, label]
    fake = FakeXtdb()
    report = loader(fake).load(events, dry_run=False)
    validate(report)
    begins = [tx[0][0] for tx in fake.transactions()]
    assert begins == [
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2026-01-01T10:00:05Z')",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2026-01-01T10:00:05.000001Z')",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2026-01-01T10:00:05.000002Z')",
    ]
    ordered = sorted([first, second, third], key=ordering_key)
    beads = fake.docs("beads")
    assert [doc["_valid_from"] for doc in beads] == [
        at("2026-01-01T10:00:00Z"),
        at("2026-01-01T10:00:00.000001Z"),
        at("2026-01-01T10:00:00.000002Z"),
    ]
    assert beads[-1]["title"] == "renamed" and beads[-1]["priority"] == 2
    split = losses(report, "x-xtdb.us-split")
    by_event: dict[str, list[tuple[str, str]]] = {}
    for loss in split:
        assert loss["disposition"] == "approximated"
        by_event.setdefault(loss["event_id"], []).append((loss["member"], loss["stored_as"]))
    assert ordered[0].id not in by_event and label.id not in by_event
    assert sorted(by_event[ordered[1].id]) == [
        ("/occurred_at", "2026-01-01T10:00:00.000001Z"),
        ("/recorded_at", "2026-01-01T10:00:05.000001Z"),
    ]
    assert sorted(by_event[ordered[2].id]) == [
        ("/occurred_at", "2026-01-01T10:00:00.000002Z"),
        ("/recorded_at", "2026-01-01T10:00:05.000002Z"),
    ]


def test_a_split_that_would_reach_the_next_recorded_at_is_refused_before_any_write() -> None:
    t = "2026-01-01T10:00:00Z"
    events = [
        create("b1", t, "2026-01-01T10:00:05Z"),
        setf("b1", "title", "x", t, "2026-01-01T10:00:05Z"),
        create("b2", t, "2026-01-01T10:00:05.000001Z"),
    ]
    fake = FakeXtdb()
    with pytest.raises(SplitPreconditionError, match="reaches the next recorded_at"):
        loader(fake).load(events, dry_run=False)
    assert fake.statements == []


def test_an_assertion_checkpoint_never_counts_toward_the_split() -> None:
    t, r = "2026-01-01T10:00:00Z", "2026-01-01T10:00:05Z"
    born = create("b1", "2026-01-01T09:00:00Z", "2026-01-01T09:00:01Z")
    covered = setf("b1", "title", "final", t, r)
    check = checkpoint_of([born, covered], covered)
    fake = FakeXtdb()
    report = loader(fake).load([born, covered, check], dry_run=False)
    validate(report)
    assert losses(report, "x-xtdb.us-split") == []
    txs = fake.transactions()
    assert len(txs) == 2  # no split transaction at r + 1 µs
    second = [p for sql, p in txs[1] if sql.startswith("INSERT INTO beads")]
    assert len(second) == 1 and second[0][0].value["_valid_from"] == at(t)
    assert check.id in {doc["_id"] for doc in fake.docs(JOURNAL)}  # journaled, writes no row


def test_a_seeding_checkpoint_is_write_zero_and_a_same_instant_change_shifts() -> None:
    t, r = "2026-01-01T10:00:00Z", "2026-01-01T10:00:05Z"
    absent = create("b1", t, r, priority=1)  # retained elsewhere: only its checkpoint ships
    seed = checkpoint_of([absent], absent)
    change = setf("b1", "priority", 3, t, r)  # same instant, same bead, sorts after the seed
    if ordering_key(change) < ordering_key(absent):
        change = setf("b1", "priority", 4, t, r)
    assert ordering_key(change) > ordering_key(absent), "fixture: the change must follow the seed"
    fake = FakeXtdb()
    report = loader(fake).load([seed, change], dry_run=False)
    validate(report)
    begins = [tx[0][0] for tx in fake.transactions()]
    assert begins[1].endswith("'2026-01-01T10:00:05.000001Z')")
    beads = fake.docs("beads")
    assert [doc["_valid_from"] for doc in beads] == [
        at(t),
        at("2026-01-01T10:00:00.000001Z"),
    ]
    assert beads[0]["priority"] == 1 and beads[1]["priority"] == change.data["value"]
    shifted = {loss["event_id"] for loss in losses(report, "x-xtdb.us-split")}
    assert shifted == {change.id}


def test_null_recorded_at_tail_loads_last_in_wall_clock_with_an_explicit_valid_to() -> None:
    later = setf("b1", "title", "renamed", "2026-01-05T10:00:00Z", "2026-01-05T10:00:01Z")
    seed = create("b1", "2026-01-01T00:00:00Z", None, priority=1)
    fake = FakeXtdb()
    report = loader(fake).load([seed, later], dry_run=False)
    validate(report)
    txs = fake.transactions()
    assert txs[0][0][0].startswith("BEGIN READ WRITE WITH (SYSTEM_TIME")
    assert txs[-1][0][0] == "BEGIN READ WRITE"  # wall clock, after the backfill
    tail = [p[0].value for sql, p in txs[-1] if sql.startswith("INSERT INTO beads")]
    windows = sorted((doc["_valid_from"], doc.get("_valid_to")) for doc in tail)
    assert windows[0] == (at("2026-01-01T00:00:00Z"), at("2026-01-05T10:00:00Z"))
    assert all(doc.get("priority") == 1 for doc in tail)  # the later version keeps the seed field
    null = losses(report, "recorded-time.null")
    assert [loss["event_id"] for loss in null] == [seed.id]
    assert null[0]["disposition"] == "reassigned" and null[0]["member"] == "/recorded_at"


def test_tombstones_journal_in_the_replay_and_erase_only_in_the_final_wall_clock_phase() -> None:
    secret = ev("comment", "b1", {"body": "pii"}, "2026-01-02T00:00:00Z", "2026-01-02T00:00:01Z")
    events = [
        create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
        ev("tombstone", "b1", {"scope": "events", "events": [secret.id], "reason": "pii"},
           "2026-01-03T00:00:00Z", "2026-01-03T00:00:01Z"),
        create("b2", "2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z"),
        ev("tombstone", "b2", {"scope": "bead", "reason": "gdpr"},
           "2026-01-04T00:00:00Z", "2026-01-04T00:00:01Z"),
    ]  # fmt: skip
    fake = FakeXtdb()
    report = loader(fake).load(events, dry_run=False)
    validate(report)
    txs = fake.transactions()
    erases = [i for i, tx in enumerate(txs) if any(sql.startswith("ERASE") for sql, _ in tx)]
    assert erases == [len(txs) - 1] and txs[-1][0][0] == "BEGIN READ WRITE"
    assert all(not sql.startswith("ERASE") for tx in txs[:-1] for sql, _ in tx)
    erase_sql = [(sql, p) for sql, p in txs[-1] if sql.startswith("ERASE")]
    assert ("ERASE FROM bead_comments WHERE _id IN (%s)", [secret.id]) in erase_sql
    assert ("ERASE FROM bead_events WHERE bead_id IN (%s) AND kind <> %s", ["b2", "tombstone"]) in (
        erase_sql
    )
    assert ("ERASE FROM beads WHERE _id IN (%s)", ["b2"]) in erase_sql
    incomplete = losses(report, "erasure.incomplete")
    assert {loss["event_id"] for loss in incomplete} == {events[1].id, events[3].id}
    assert all("erase-logical" in loss["detail"] for loss in incomplete)
    assert all(loss["disposition"] != "dropped" for loss in incomplete)
    # the bead-scope tombstone erased b2's create from this very load: never written
    assert [loss["event_id"] for loss in losses(report, "x-xtdb.erased")] == [events[2].id]
    assert "b2" not in {doc["_id"] for doc in fake.docs("beads")}


# -- refusals before any write ----------------------------------------------------------------


def test_an_out_of_order_index_is_refused_before_any_write() -> None:
    events = [
        create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
        create("b2", "2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z"),
    ]
    fake = FakeXtdb()
    with pytest.raises(MonotoneOrderError, match="out of recorded-time order"):
        loader(fake).load(events, dry_run=False, order=[events[1].id, events[0].id])
    assert fake.statements == []


def test_a_future_recorded_at_is_refused_before_any_write() -> None:
    future = (NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = [
        create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
        create("b2", None, future),
    ]
    fake = FakeXtdb()
    with pytest.raises(FutureRecordedTimeError, match="later than the wall clock"):
        loader(fake).load(events, dry_run=False)
    assert fake.statements == []
    loader(FakeXtdb(), clock=lambda: NOW + timedelta(seconds=2)).load(events, dry_run=True)


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeXtdb(tables={"beads": True}), "already hold rows"),
        (FakeXtdb(latest=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)), "at or after its first"),
    ],
    ids=["non-empty-tables", "transaction-at-first-recorded-at"],
)
def test_an_initial_load_into_a_target_that_is_not_fresh_is_refused(
    fake: FakeXtdb, message: str
) -> None:
    events = [create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")]
    with pytest.raises(TargetNotFreshError, match=message):
        loader(fake, mode="initial").load(events, dry_run=False)
    assert fake.statements == []


def test_an_older_empty_target_still_takes_a_monotone_load() -> None:
    fake = FakeXtdb(latest=datetime(2025, 1, 1, tzinfo=UTC))
    report = loader(fake, mode="initial").load(
        [create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")], dry_run=False
    )
    assert report["losses"] == []
    assert fake.transactions()[0][0][0].startswith("BEGIN READ WRITE WITH (SYSTEM_TIME")


def test_layout_rows_without_a_journal_are_refused_in_incremental_mode() -> None:
    fake = FakeXtdb(tables={"beads": True})
    with pytest.raises(TargetNotFreshError, match="no bead_events journal"):
        loader(fake).load([create("b1", "2026-01-01T00:00:00Z", None)], dry_run=False)


# -- aborts: discard and reload, never partial success ----------------------------------------


def test_an_abort_at_commit_fails_with_discard_and_reload_and_never_retries() -> None:
    events = [
        create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
        create("b2", "2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z"),
        create("b3", "2026-01-01T00:00:00Z", "2026-01-01T00:00:03Z"),
    ]
    commits = 0

    def fail(sql: str, _: int) -> bool:
        nonlocal commits
        if sql == "COMMIT":
            commits += 1
            return commits == 2
        return False

    fake = FakeXtdb(fail=fail)
    with pytest.raises(LoadAborted) as caught:
        loader(fake).load(events, dry_run=False)
    message = str(caught.value)
    assert "Discard this database" in message and "never retries" in message
    assert "transaction 2 of 3" in message and "Recorded-time preservation is lost" in message
    writes = fake.writes()
    assert writes[-1] == "ROLLBACK"
    assert sum(sql.startswith("BEGIN") for sql in writes) == 2  # no retry, no third transaction


def test_a_failing_statement_aborts_the_same_way() -> None:
    fake = FakeXtdb(fail=lambda sql, _: sql.startswith("INSERT INTO beads"))
    with pytest.raises(LoadAborted, match="Discard this database"):
        loader(fake).load(
            [create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")], dry_run=False
        )
    assert fake.writes()[-1] == "ROLLBACK"


def test_an_abort_recorded_only_in_xt_txs_is_not_reported_as_success() -> None:
    fake = FakeXtdb(aborted=1)
    with pytest.raises(LoadAborted, match="aborted transaction"):
        loader(fake).load(
            [create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")], dry_run=False
        )


# -- dry run, incremental mode, idempotence ---------------------------------------------------


def _history() -> list[BeadEvent]:
    t = "2026-01-01T10:00:00Z"
    return [
        create("b1", t, "2026-01-01T10:00:01Z"),
        setf("b1", "title", "renamed", t, "2026-01-01T10:00:01Z"),
        create("b2", "2026-01-02T00:00:00Z", None, provenance="inferred"),
        ev("edge_add", "b1", {"type": "blocks", "target": bead("b9").to_json()},
           "2026-01-03T00:00:00Z", "2026-01-03T00:00:01Z"),
    ]  # fmt: skip


def test_a_dry_run_writes_nothing_and_reports_exactly_what_the_apply_reports() -> None:
    preview_fake, apply_fake = FakeXtdb(), FakeXtdb()
    preview = loader(preview_fake).load(_history(), dry_run=True)
    applied = loader(apply_fake).load(_history(), dry_run=False)
    validate(preview)
    validate(applied)
    assert preview_fake.statements == []
    assert preview["mode"] == "dry-run" and applied["mode"] == "apply"
    assert preview["losses"] == applied["losses"] and preview["summary"] == applied["summary"]


def test_auto_mode_goes_incremental_on_a_loaded_target_and_reloads_idempotently() -> None:
    fake = FakeXtdb()
    store = loader(fake)
    store.load(_history()[:2], dry_run=False)
    assert store.last_plan is not None and store.last_plan.mode is LoadMode.INITIAL
    statements = len(fake.statements)
    report = store.load(_history(), dry_run=False)
    validate(report)
    assert store.last_plan.mode is LoadMode.INCREMENTAL
    assert set(store.last_plan.skipped) == {e.id for e in _history()[:2]}
    new = fake.statements[statements:]
    assert all(sql == "BEGIN READ WRITE" for sql, _ in new if sql.startswith("BEGIN"))
    reassigned = {loss["event_id"] for loss in losses(report, "recorded-time.reassigned")}
    assert reassigned == {_history()[3].id}
    assert {loss["event_id"] for loss in losses(report, "recorded-time.null")} == {_history()[2].id}
    before = len(fake.statements)
    again = store.load(_history(), dry_run=False)
    assert len(fake.statements) == before  # nothing to write the second time
    assert again["losses"] == []
    assert {event.id for event in store.export()} == {event.id for event in _history()}


def test_incremental_load_continues_the_written_timeline() -> None:
    fake = FakeXtdb()
    store = loader(fake)
    store.load([create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")], dry_run=False)
    store.load(
        [setf("b1", "title", "x", "2026-02-01T00:00:00Z", "2026-02-01T00:00:01Z")], dry_run=False
    )
    beads = fake.docs("beads")
    assert [doc["_valid_from"] for doc in beads] == [
        at("2026-01-01T00:00:00Z"),
        at("2026-02-01T00:00:00Z"),
    ]


def test_an_incremental_tombstone_erases_earlier_rows_and_rewrites_what_survives() -> None:
    fake = FakeXtdb()
    store = loader(fake)
    secret_title = setf("b1", "title", "secret", "2026-01-02T00:00:00Z", "2026-01-02T00:00:01Z")
    note = ev("comment", "b1", {"body": "pii"}, "2026-01-03T00:00:00Z", "2026-01-03T00:00:01Z")
    store.load(
        [
            create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", priority=1),
            secret_title,
            note,
        ],
        dry_run=False,
    )
    written = len(fake.statements)
    erased = {"scope": "events", "events": [secret_title.id, note.id], "reason": "pii"}
    erase = ev("tombstone", "b1", erased, "2026-01-04T00:00:00Z", "2026-01-04T00:00:01Z")
    report = store.load([erase], dry_run=False)
    validate(report)
    txs = []
    for sql, params in fake.statements[written:]:
        if sql.startswith("BEGIN"):
            txs.append([])
        txs[-1].append((sql, params))
    erase_tx = next(tx for tx in txs if any(sql.startswith("ERASE") for sql, _ in tx))
    assert ("ERASE FROM beads WHERE _id IN (%s)", ["b1"]) in erase_tx
    assert (
        "ERASE FROM bead_comments WHERE _id IN (%s, %s)",
        sorted([secret_title.id, note.id]),
    ) in (erase_tx)
    rewrite = txs[txs.index(erase_tx) + 1]
    rewritten = [p[0].value for sql, p in rewrite if sql.startswith("INSERT INTO beads")]
    assert rewritten and all(doc["title"] == "b1" for doc in rewritten)  # the secret is gone
    assert "rewritten at load time" in losses(report, "erasure.incomplete")[0]["detail"]


def test_a_multi_group_batch_into_a_single_group_store_is_refused() -> None:
    fake = FakeXtdb()
    store = loader(fake)
    store.load([create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")], dry_run=False)
    other = ev(
        "create",
        "b1",
        {"type": "task", "fields": {}},
        "2026-01-02T00:00:00Z",
        None,
        group=OTHER_GROUP,
    )
    with pytest.raises(LayoutMismatchError):
        store.load([other], dry_run=False)


# -- degradations ------------------------------------------------------------------------------


def test_sub_microsecond_times_are_truncated_and_reported_never_a_crash() -> None:
    event = create("b1", "2026-01-01T00:00:00.123456789Z", "2026-01-01T00:00:01.000000001Z")
    assert truncate_timestamp(event.occurred_at) == "2026-01-01T00:00:00.123456Z"
    assert truncate_timestamp(event.recorded_at) == "2026-01-01T00:00:01Z"
    fake = FakeXtdb()
    report = loader(fake).load([event], dry_run=False)
    validate(report)
    cut = sorted(
        (loss["member"], loss["stored_as"]) for loss in losses(report, "x-xtdb.sub-microsecond")
    )
    assert cut == [
        ("/occurred_at", "2026-01-01T00:00:00.123456Z"),
        ("/recorded_at", "2026-01-01T00:00:01Z"),
    ]
    assert fake.docs("beads")[0]["_valid_from"] == at("2026-01-01T00:00:00.123456Z")
    assert json.loads(fake.docs(JOURNAL)[0]["event"])["occurred_at"] == event.occurred_at


def test_column_coercions_refusals_and_unsupported_fields_are_reported() -> None:
    events = [
        create("b1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", priority="3", group_id="g"),
        setf("b1", "priority", "high", "2026-01-02T00:00:00Z", "2026-01-02T00:00:01Z"),
        setf("b1", "estimate.hours", 3, "2026-01-03T00:00:00Z", "2026-01-03T00:00:01Z"),
    ]
    fake = FakeXtdb()
    report = loader(fake).load(events, dry_run=False)
    validate(report)
    coerced = losses(report, "x-xtdb.column-coerced")
    assert [(loss["member"], loss["stored_as"]) for loss in coerced] == [
        ("/data/fields/priority", "beads.priority")
    ]
    refused = losses(report, "x-xtdb.column-refused")
    assert [loss["event_id"] for loss in refused] == [events[1].id]
    unsupported = losses(report, "field.unsupported")
    assert sorted(loss["member"] for loss in unsupported) == [
        "/data/fields/group_id",
        "/data/value",
    ]
    beads = fake.docs("beads")
    assert beads[0]["priority"] == 3 and beads[0]["group_id"] == GROUP
    assert "priority" not in beads[1]


def test_valid_time_missing_precision_and_dangling_edges_are_reported() -> None:
    events = [
        ev("create", "b1", {"type": "task", "fields": {}}, None, "2026-01-01T00:00:01Z"),
        create("b2", "2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z"),
        ev("create", "b3", {"type": "task", "fields": {}}, "2026-01-01T00:00:00Z",
           "2026-01-01T00:00:03Z", occurred_precision="day"),
        ev("edge_add", "b2", {"type": "blocks", "target": bead("ghost").to_json()},
           "2026-01-02T00:00:00Z", "2026-01-02T00:00:01Z"),
        ev("edge_add", "b2", {"type": "related", "target": bead("b1").to_json()},
           "2026-01-02T00:00:00Z", "2026-01-02T00:00:02Z"),
    ]  # fmt: skip
    report = loader(FakeXtdb()).load(events, dry_run=False)
    validate(report)
    missing = losses(report, "valid-time.missing")
    assert [(loss["event_id"], loss["stored_as"]) for loss in missing] == [
        (events[0].id, "2026-01-01T00:00:01Z")
    ]
    assert [loss["event_id"] for loss in losses(report, "precision.lost")] == [events[2].id]
    assert [loss["event_id"] for loss in losses(report, "edge.dangling-target")] == [events[3].id]


def test_an_id_the_layout_cannot_spell_is_dropped_and_reported() -> None:
    bad = create("bad|id", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
    fake = FakeXtdb()
    report = loader(fake).load(
        [bad, create("ok", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")], dry_run=False
    )
    validate(report)
    dropped = losses(report, "x-xtdb.id-unrepresentable")
    assert [(loss["event_id"], loss["disposition"]) for loss in dropped] == [(bad.id, "dropped")]
    assert report["summary"]["dropped"] == 1
    assert {doc["_id"] for doc in fake.docs("beads")} == {"ok"}


def test_the_archive_driver_replays_the_index_order_and_names_the_manifest() -> None:
    buffer = io.BytesIO()
    write_archive(
        buffer,
        _history(),
        groups=[Group(GROUP, "g")],
        generator=Generator("test", "0.1.0"),
        index=True,
    )
    store = loader(FakeXtdb())
    report = load_archive(store, io.BytesIO(buffer.getvalue()))
    validate(report)
    assert report["archive"]["manifest_sha256"] != report["loader"]["name"]
    assert store.last_plan is not None and store.last_plan.mode is LoadMode.INITIAL


def test_plan_load_is_pure_and_refuses_auto() -> None:
    with pytest.raises(ValueError, match="resolve"):
        plan_load([], mode=LoadMode.AUTO, now=NOW)
    plan = plan_load(_history(), mode=LoadMode.INITIAL, now=NOW)
    assert [tx.phase for tx in plan.transactions] == ["backfill", "split", "backfill", "tail"]
    assert plan.first_system_time == datetime(2026, 1, 1, 10, 0, 1, tzinfo=UTC)
