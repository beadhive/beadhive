"""The Dolt-only capability interfaces (hq-storage-port-adr section 4) and their Dolt adapters.

A generic consumer never needs these; a Dolt-specific one reaches them through
``CapabilityHost.capability(DoltHistory)`` (and gets ``None``, hence ``CapabilityUnsupported``,
on any other adapter). The shapes here are the first cut; their final shapes for cutover, orphan,
audit and ``hive_forward`` are ``bh-3kl1k``'s. ``DoltFramePrivateRefs`` stays provisional and is
not declared here (ADR section 4).

Every implementation runs over a **query runner** -- ``Callable[[str], list[dict]]`` plus a
statement runner -- so the same code serves a hive fence's engine (:func:`engine_runner`) and the
HQ connection factory (:func:`connection_runner`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from beadhive_hq_storage import InvalidRequest, Revision, StorageUnavailable

from . import _schema as schema
from ._hq import ConnectionFactory, Role
from ._values import aware, quote

__all__ = [
    "CommitInfo",
    "DoltAccounts",
    "DoltHistory",
    "DoltMerge",
    "FenceTable",
    "HistoryRow",
    "MergeRefused",
    "SqlDoltAccounts",
    "SqlDoltHistory",
    "SqlDoltMerge",
    "SqlRunner",
    "connection_runner",
    "engine_runner",
]

_HASH = re.compile(r"[0-9a-v]{32}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,254}")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_PRIVILEGES = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRIGGER", "EXECUTE", "ALL", "ALL PRIVILEGES"}
)


class FenceTable(StrEnum):
    """The closed set of tables ``DoltHistory.table_history`` reads. Bead history is not in it,
    so the capability cannot become a bead-data read (ADR section 4)."""

    BH_WRITER = "bh_writer"
    BH_WRITE_MARK = "bh_write_mark"
    BH_EPOCH_LIVE = "bh_epoch_live"


@dataclass(frozen=True)
class CommitInfo:
    commit: Revision
    parents: tuple[Revision, ...]
    message: str
    date: datetime


@dataclass(frozen=True)
class HistoryRow:
    commit: Revision
    row: Mapping[str, Any]


class MergeRefused(InvalidRequest):
    """A merge left conflicts or constraint violations; it was aborted, nothing committed."""


# --- the protocols ----------------------------------------------------------------------------


@runtime_checkable
class DoltHistory(Protocol):
    """Commit-graph walks and fence-table history (``fence_audit``'s I3 late-write check)."""

    def head(self, ref: str = "HEAD") -> Revision: ...

    def log(self, ref: str, *, limit: int | None = None) -> Sequence[CommitInfo]: ...

    def merge_base(self, left: str, right: str) -> Revision | None: ...

    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...

    def table_history(self, table: FenceTable, *, at: Revision) -> Sequence[HistoryRow]: ...


@runtime_checkable
class DoltMerge(Protocol):
    """Merge with a conflict preview, abort, and the one sanctioned rewind (``fence_orphan``,
    ``fence_cutover``, the server-mode reset)."""

    def preview_conflicts(self, base: str, other: str) -> tuple[str, ...]: ...

    def merge(self, rev: str, message: str) -> Revision: ...

    def abort(self) -> None: ...

    def reset_hard(self, rev: str) -> None: ...


@runtime_checkable
class DoltAccounts(Protocol):
    """MySQL users and grants, schema provisioning, and trigger conformance (``hive_forward``,
    ``hq_sql_session_provision``, ``check_grants``, ``check_triggers``)."""

    def provision_hq_schema(self, *, generation: str, author: str) -> Revision: ...

    def create_user(self, user: str, host: str, password: str) -> None: ...

    def drop_user(self, user: str, host: str) -> None: ...

    def grant(self, privileges: Sequence[str], table: str, user: str, host: str) -> None: ...

    def grants(self, user: str, host: str) -> tuple[str, ...]: ...

    def triggers(self) -> Mapping[str, str]: ...


# --- runners ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlRunner:
    """``query(sql) -> rows as dicts`` and ``execute(statements)`` (one session, in order)."""

    query: Callable[[str], list[dict]]
    execute: Callable[[Sequence[str]], None]


def engine_runner(engine: Any) -> SqlRunner:
    """A runner over a fence statement engine (root's ``DoltCliEngine`` / ``BdServerEngine`` or
    :class:`~beadhive_hq_storage_dolt.SqlConnectionFenceEngine`)."""
    return SqlRunner(query=engine.query, execute=engine.execute)


def connection_runner(connect: ConnectionFactory, role: Role = Role.OPERATOR) -> SqlRunner:
    """A runner over the HQ connection factory: each call owns one connection and commits."""

    def run(statements: Sequence[str], rows: bool) -> list[dict]:
        connection = connect(role)
        try:
            cursor = connection.cursor()
            out: list[dict] = []
            for statement in statements:
                cursor.execute(statement)
                description = getattr(cursor, "description", None)
                if rows and description:
                    names = [d[0] for d in description]
                    out = [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]
            connection.commit()
            return out
        except Exception:
            try:
                connection.rollback()
            finally:
                raise
        finally:
            connection.close()

    return SqlRunner(
        query=lambda sql: run([sql], True), execute=lambda statements: run(statements, False)
    )


def _ref(value: str, what: str = "revision") -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value) or ".." in value:
        raise InvalidRequest(f"{what} {value!r} is not a plain Dolt ref or hash")
    return value


def _ident(value: str, what: str) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise InvalidRequest(f"{what} {value!r} is not a plain SQL identifier")
    return value


def _first(rows: list[dict]) -> Any:
    return next(iter(rows[0].values()), None) if rows else None


def _parents(value: object) -> tuple[Revision, ...]:
    return tuple(Revision(p.strip()) for p in str(value or "").split(",") if p.strip())


# --- adapters ---------------------------------------------------------------------------------


class SqlDoltHistory:
    """:class:`DoltHistory` over a :class:`SqlRunner`."""

    def __init__(self, runner: SqlRunner) -> None:
        self._run = runner

    def _query(self, sql: str) -> list[dict]:
        try:
            return self._run.query(sql)
        except InvalidRequest:
            raise
        except Exception:  # noqa: BLE001
            raise StorageUnavailable("Dolt history read unavailable") from None

    def head(self, ref: str = "HEAD") -> Revision:
        found = _first(self._query(f"SELECT DOLT_HASHOF({quote(_ref(ref, 'ref'))}) AS h"))
        if not found:
            raise InvalidRequest(f"ref {ref!r} does not resolve")
        return Revision(str(found))

    def log(self, ref: str, *, limit: int | None = None) -> Sequence[CommitInfo]:
        sql = (
            "SELECT commit_hash AS c, parents AS p, message AS m, date AS d "
            f"FROM dolt_log('--parents', {quote(_ref(ref, 'ref'))})"
        )
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise InvalidRequest("limit must be a positive integer")
            sql += f" LIMIT {limit}"
        return [
            CommitInfo(Revision(str(r["c"])), _parents(r.get("p")), str(r["m"]), aware(r["d"]))
            for r in self._query(sql)
        ]

    def merge_base(self, left: str, right: str) -> Revision | None:
        found = _first(
            self._query(f"SELECT dolt_merge_base({quote(_ref(left))}, {quote(_ref(right))}) AS b")
        )
        return None if not found else Revision(str(found))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.merge_base(ancestor, descendant) == self.head(ancestor)

    def table_history(self, table: FenceTable, *, at: Revision) -> Sequence[HistoryRow]:
        if not isinstance(table, FenceTable):
            raise InvalidRequest("table_history reads fence tables only (a FenceTable member)")
        if not isinstance(at, str) or not _HASH.fullmatch(at):
            raise InvalidRequest("table_history needs an exact commit hash")
        database = _ident(str(_first(self._query("SELECT DATABASE() AS d"))), "database")
        rows = self._query(f"SELECT * FROM `{database}/{at}`.dolt_history_{table.value}")
        out = []
        for row in rows:
            commit = Revision(str(row.get("commit_hash")))
            body = {
                k: v for k, v in row.items() if k not in ("commit_hash", "committer", "commit_date")
            }
            out.append(HistoryRow(commit, body))
        return out


class SqlDoltMerge:
    """:class:`DoltMerge` over a :class:`SqlRunner` (statements run in one session)."""

    def __init__(self, runner: SqlRunner) -> None:
        self._run = runner

    def preview_conflicts(self, base: str, other: str) -> tuple[str, ...]:
        rows = self._run.query(
            "SELECT `table` AS t, num_data_conflicts AS d, num_schema_conflicts AS s FROM "
            f"DOLT_PREVIEW_MERGE_CONFLICTS_SUMMARY({quote(_ref(base))}, {quote(_ref(other))})"
        )
        return tuple(
            sorted(str(r["t"]) for r in rows if int(r.get("d") or 0) or int(r.get("s") or 0))
        )

    def merge(self, rev: str, message: str) -> Revision:
        if self.preview_conflicts("HEAD", rev):
            raise MergeRefused(f"merging {rev} would conflict; nothing merged")
        try:
            self._run.execute(
                [f"CALL DOLT_MERGE('--no-ff', '-m', {quote(message)}, {quote(_ref(rev))})"]
            )
        except Exception:  # noqa: BLE001
            self.abort()
            raise MergeRefused(f"merging {rev} was refused; nothing committed") from None
        return Revision(str(_first(self._run.query("SELECT DOLT_HASHOF('HEAD') AS h"))))

    def abort(self) -> None:
        merging = self._run.query("SELECT is_merging AS m FROM dolt_merge_status")
        if merging and str(_first(merging)).lower() in ("1", "true"):
            self._run.execute(["CALL DOLT_MERGE('--abort')"])

    def reset_hard(self, rev: str) -> None:
        self.abort()
        self._run.execute([f"CALL DOLT_RESET('--hard', {quote(_ref(rev))})"])


class SqlDoltAccounts:
    """:class:`DoltAccounts` over a :class:`SqlRunner` holding operator privileges."""

    def __init__(self, runner: SqlRunner) -> None:
        self._run = runner

    def provision_hq_schema(self, *, generation: str, author: str) -> Revision:
        """Create the HQ storage schema on a fresh database: the committed publication tables
        and ignore rule as the provisioning commit, then the live tables and their triggers."""
        self._run.execute(schema.committed_schema(generation, author))
        self._run.execute(schema.live_schema())
        return Revision(str(_first(self._run.query("SELECT DOLT_HASHOF('HEAD') AS h"))))

    @staticmethod
    def _account(user: str, host: str) -> str:
        return f"{quote(_ident(user, 'user'))}@{quote(_host(host))}"

    def create_user(self, user: str, host: str, password: str) -> None:
        if not isinstance(password, str) or not password:
            raise InvalidRequest("a SQL account needs a password")
        self._run.execute(
            [f"CREATE USER {self._account(user, host)} IDENTIFIED BY {quote(password)}"]
        )

    def drop_user(self, user: str, host: str) -> None:
        self._run.execute([f"DROP USER IF EXISTS {self._account(user, host)}"])

    def grant(self, privileges: Sequence[str], table: str, user: str, host: str) -> None:
        wanted = [p.strip().upper() for p in privileges]
        if not wanted or any(p not in _PRIVILEGES for p in wanted):
            raise InvalidRequest(f"unsupported privileges {privileges!r}")
        database = _ident(str(_first(self._run.query("SELECT DATABASE() AS d"))), "database")
        target = "*" if table == "*" else _ident(table, "table")
        self._run.execute(
            [f"GRANT {', '.join(wanted)} ON {database}.{target} TO {self._account(user, host)}"]
        )

    def grants(self, user: str, host: str) -> tuple[str, ...]:
        rows = self._run.query(f"SHOW GRANTS FOR {self._account(user, host)}")
        return tuple(str(_first([row])) for row in rows)

    def triggers(self) -> Mapping[str, str]:
        """``{trigger name: CREATE statement}`` from ``dolt_schemas`` (working set), never
        ``information_schema.triggers``, which fails on a trigger whose table is ignored."""
        rows = self._run.query(
            "SELECT name AS n, fragment AS f FROM dolt_schemas WHERE type = 'trigger'"
        )
        return {str(r["n"]): str(r["f"]) for r in rows}


def default_hq_capability(kind: type, connect: ConnectionFactory) -> object | None:
    """The HQ store's built-in capability for ``kind``, or ``None``."""
    if kind is DoltHistory:
        return SqlDoltHistory(connection_runner(connect, Role.READER))
    if kind is DoltMerge:
        return SqlDoltMerge(connection_runner(connect, Role.OPERATOR))
    if kind is DoltAccounts:
        return SqlDoltAccounts(connection_runner(connect, Role.OPERATOR))
    return None


def default_fence_capability(kind: type, engine: Any) -> object | None:
    """A hive fence's built-in capability for ``kind``, or ``None``."""
    if kind is DoltHistory:
        return SqlDoltHistory(engine_runner(engine))
    if kind is DoltMerge:
        return SqlDoltMerge(engine_runner(engine))
    return None


def _host(host: str) -> str:
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.%:-]{1,255}", host):
        raise InvalidRequest(f"host {host!r} is not a plain account host")
    return host
