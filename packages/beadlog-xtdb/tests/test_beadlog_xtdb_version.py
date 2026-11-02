"""The version gate (``>=2.2.0,<2.3``) and the dangling-edge report, against fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from beadlog_xtdb import (
    DANGLING_EDGE_SQL,
    DanglingEdge,
    UnsupportedXtdbVersion,
    check_version,
    find_dangling_edges,
    parse_version,
    require_supported_version,
)


@pytest.mark.parametrize(
    "text", ["PostgreSQL 16 (XTDB 2.2.0)", "PostgreSQL 16 (XTDB 2.2.7)", "XTDB 2.2.99"]
)
def test_the_supported_range_is_accepted(text: str) -> None:
    version = check_version(text)
    assert version.release[:2] == (2, 2)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("PostgreSQL 16 (XTDB 2.1.1)", "older than the supported floor 2.2.0"),
        ("PostgreSQL 16 (XTDB 2.0.0)", "Upgrade the node"),
        ("PostgreSQL 16 (XTDB 2.2.0-SNAPSHOT)", "older than the supported floor"),
        ("PostgreSQL 16 (XTDB 2.3.0)", "newer than the validated range >=2.2.0,<2.3"),
        ("PostgreSQL 16 (XTDB 2.3.0-rc1)", "conformance kit"),
        ("PostgreSQL 16 (XTDB 3.0.0)", "newer than"),
    ],
)
def test_other_versions_are_refused_with_an_actionable_message(text: str, needle: str) -> None:
    with pytest.raises(UnsupportedXtdbVersion, match=needle):
        check_version(text)


@pytest.mark.parametrize("text", ["PostgreSQL 16.2 (Debian 16.2-1)", "", "XTDB", "garbage"])
def test_a_server_that_is_not_xtdb_is_refused(text: str) -> None:
    with pytest.raises(UnsupportedXtdbVersion, match="not an XTDB node"):
        parse_version(text)


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        self.calls.append((sql, params))
        return _Cursor(self.rows)


def test_require_supported_version_asks_the_server() -> None:
    connection = _Connection([("PostgreSQL 16 (XTDB 2.2.0)",)])
    assert str(require_supported_version(connection)) == "2.2.0"
    assert connection.calls == [("SELECT version()", None)]
    with pytest.raises(UnsupportedXtdbVersion):
        require_supported_version(_Connection([("PostgreSQL 16 (XTDB 2.4.0)",)]))
    with pytest.raises(UnsupportedXtdbVersion):
        require_supported_version(_Connection([]))


def test_dangling_edges_are_reported_with_external_targets_split_out() -> None:
    rows = [
        ("bh-1", "blocks", "external:infra:gateway"),
        ("bh-2", "related", "discovered-from:bh-9.1"),
        ("bh-3", "blocks", "bh-gone"),
    ]
    connection = _Connection(rows)
    report = find_dangling_edges(connection)
    assert connection.calls == [(DANGLING_EDGE_SQL, None)]
    assert len(report) == 3
    assert report.external == (DanglingEdge("bh-1", "blocks", "external:infra:gateway"),)
    assert {edge.to_id for edge in report.unresolved} == {"discovered-from:bh-9.1", "bh-gone"}
    assert "NOT EXISTS" in DANGLING_EDGE_SQL and "b._id = e.to_id" in DANGLING_EDGE_SQL


def test_dangling_edges_as_of_a_valid_time_bind_the_instant_twice() -> None:
    connection = _Connection([])
    moment = datetime(2026, 10, 1, tzinfo=UTC)
    assert len(find_dangling_edges(connection, valid_as_of=moment)) == 0
    sql, params = connection.calls[0]
    assert sql.count("FOR VALID_TIME AS OF %s") == 2 and params == [moment, moment]
    with pytest.raises(ValueError, match="aware"):
        find_dangling_edges(connection, valid_as_of=datetime(2026, 10, 1))
