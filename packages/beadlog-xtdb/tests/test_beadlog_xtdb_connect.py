"""Connection setup against fakes: adapters, the transit dumper and loader, batched INSERTs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.adapt import AdaptersMap, PyFormat
from psycopg.pq import Format

import beadlog_xtdb
from beadlog_xtdb import (
    DEFAULT_PARAMS,
    READ_FORMAT_SQL,
    TRANSIT_OID,
    Instant,
    TransitDumper,
    TransitLoader,
    TransitParam,
    configure_adapters,
    configure_connection,
    connect,
    insert_records,
    insert_statement,
)


class FakeConnection:
    """What ``configure_connection`` touches of a psycopg connection."""

    def __init__(self) -> None:
        self.adapters = AdaptersMap(psycopg.adapters)
        self.autocommit = False
        self.statements: list[tuple[str, Any, bool]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((sql, params, self.autocommit))


def dump(adapters: AdaptersMap, value: Any) -> tuple[int, bytes]:
    dumper = adapters.get_dumper(type(value), PyFormat.TEXT)(type(value))
    return dumper.oid, bytes(dumper.dump(value))


def test_adapters_send_typed_text_int8_and_transit_parameters() -> None:
    adapters = AdaptersMap(psycopg.adapters)
    configure_adapters(adapters)
    text_oid, _ = dump(adapters, "bead-1")
    assert text_oid == psycopg.postgres.types["text"].oid  # never OID 0: XTDB refuses untyped DML
    int_oid, _ = dump(adapters, 2)
    assert int_oid == psycopg.postgres.types["int8"].oid  # never int2: it would make a smallint
    oid, payload = dump(adapters, TransitParam({"_id": "x", "n": 2}))
    assert oid == TRANSIT_OID == 16384
    assert json.loads(payload) == {"_id": "x", "n": 2}


def test_the_transit_parameter_escapes_sets_and_instants() -> None:
    doc = {
        "_id": "bh-1",
        "title": "~starts with a tilde",
        "labels": frozenset({"b", "a"}),
        "due_at": Instant.parse("2026-10-30T19:25:37.123456Z"),
        "nested": [{"created_at": Instant.parse("2026-10-22T00:06:00Z")}],
    }
    adapters = AdaptersMap(psycopg.adapters)
    configure_adapters(adapters)
    _, payload = dump(adapters, TransitParam(doc))
    assert json.loads(payload) == {
        "_id": "bh-1",
        "title": "~~starts with a tilde",
        "labels": {"~#set": ["a", "b"]},
        "due_at": {"~#time/instant": "2026-10-30T19:25:37.123456Z"},
        "nested": [{"created_at": {"~#time/instant": "2026-10-22T00:06:00Z"}}],
    }


def test_the_loader_decodes_oid_16384_text_to_typed_values() -> None:
    loader = TransitLoader(TRANSIT_OID)
    data = (
        b'["^ ","deps",[["^ ","at",["~#time/zoned-date-time","2026-10-22T00:06Z[UTC]"]]],'
        b'"s",["~#set",["a"]]]'
    )
    assert loader.load(memoryview(data)) == {
        "deps": [{"at": datetime(2026, 10, 22, 0, 6, tzinfo=UTC)}],
        "s": frozenset({"a"}),
    }
    adapters = AdaptersMap(psycopg.adapters)
    configure_adapters(adapters)
    assert adapters.get_loader(TRANSIT_OID, Format.TEXT) is TransitLoader
    assert TransitDumper.oid == TRANSIT_OID


def test_configure_connection_sets_transit_output_outside_a_transaction() -> None:
    connection = FakeConnection()
    assert configure_connection(connection) is connection
    assert connection.statements == [(READ_FORMAT_SQL, None, True)]
    assert connection.autocommit is False  # restored
    assert READ_FORMAT_SQL == "SET fallback_output_format='transit'"
    quiet = FakeConnection()
    configure_connection(quiet, transit_reads=False)
    assert quiet.statements == []


def test_connect_defaults_to_a_loopback_node_and_applies_the_configuration() -> None:
    seen: dict[str, Any] = {}

    def connector(conninfo: str, **kwargs: Any) -> FakeConnection:
        seen.clear()
        seen.update(conninfo=conninfo, **kwargs)
        return FakeConnection()

    connection = connect(connector=connector)
    assert seen == {"conninfo": "", "autocommit": False, **DEFAULT_PARAMS}
    assert DEFAULT_PARAMS["host"] == "127.0.0.1"
    assert connection.statements[0][0] == READ_FORMAT_SQL

    connect("host=db port=1", autocommit=True, connector=connector, password="pw")
    assert seen == {"conninfo": "host=db port=1", "autocommit": True, "password": "pw"}
    connect(connector=connector, port=15432, dbname="other")
    assert seen["port"] == 15432 and seen["dbname"] == "other" and seen["host"] == "127.0.0.1"


def test_insert_statement_uses_one_records_placeholder_per_document() -> None:
    assert insert_statement("beads", 3) == "INSERT INTO beads RECORDS %s, %s, %s"
    with pytest.raises(ValueError):
        insert_statement("beads", 0)
    for table in ("beads; DROP", "Beads", "", "a b", "1x"):
        with pytest.raises(ValueError, match="safe table"):
            insert_statement(table, 1)


def test_insert_records_batches_and_leaves_transactions_to_the_caller() -> None:
    connection = FakeConnection()
    documents = [{"_id": f"bh-{index}"} for index in range(7)]
    assert insert_records(connection, "beads", documents, batch_size=3) == 7
    sizes = [len(params) for _, params, _ in connection.statements]
    assert sizes == [3, 3, 1]
    assert all(
        isinstance(p, TransitParam) for _, params, _ in connection.statements for p in params
    )
    assert [sql.count("%s") for sql, _, _ in connection.statements] == sizes
    assert insert_records(FakeConnection(), "beads", [], batch_size=3) == 0
    with pytest.raises(ValueError):
        insert_records(connection, "beads", documents, batch_size=0)


def test_psycopg_is_a_declared_dependency_not_imported_by_beadhive() -> None:
    assert beadlog_xtdb.distribution_name() == "beadlog-xtdb"
