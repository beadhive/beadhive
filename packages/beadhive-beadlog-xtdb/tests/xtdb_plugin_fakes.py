"""Fakes for the plugin's tests: a connection, a connector and a loader stand-in."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

SECRET = "s3cr3t-" + "Xtdb" * 4


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


@dataclass
class FakeConnection:
    """Answers the information_schema table query with ``tables``; records other statements."""

    closed: bool = False
    autocommit: bool = True
    tables: tuple[str, ...] = ("beads", "bead_labels", "bead_edges", "bead_comments")
    statements: list[str] = field(default_factory=list)

    def execute(self, sql: str, params: Any = None) -> _Rows:
        self.statements.append(sql)
        if "information_schema.tables" in sql:
            return _Rows([(name,) for name in self.tables])
        return _Rows([("row",)])

    def close(self) -> None:
        self.closed = True


@dataclass
class Connector:
    """Records every ``connect(dsn, password)``; raises ``fail`` when set."""

    fail: BaseException | None = None
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    opened: list[FakeConnection] = field(default_factory=list)

    def __call__(self, dsn: str, password: str | None) -> FakeConnection:
        self.calls.append((dsn, password))
        if self.fail is not None:
            raise self.fail
        connection = FakeConnection()
        self.opened.append(connection)
        return connection


class FakeInnerLoader:
    """The ``XtdbStoreLoader`` surface the guard wraps."""

    name = "xtdb"
    version = "0.1.0"
    capabilities = frozenset({"valid-time"})

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.loaded: list[tuple[Sequence[Any], bool]] = []

    def export(self):
        if self.fail is not None:
            raise self.fail
        return iter(())

    def load(self, events, *, dry_run):
        if self.fail is not None:
            raise self.fail
        self.loaded.append((tuple(events), dry_run))
        return {"mode": "dry-run" if dry_run else "apply"}
