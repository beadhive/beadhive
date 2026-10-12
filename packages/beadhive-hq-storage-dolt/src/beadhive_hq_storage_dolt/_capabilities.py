"""The Dolt-only capability interfaces (hq-storage-port-adr section 4) and their Dolt adapters.

A generic consumer never needs these; a Dolt-specific one reaches them through
``require_capability(host, DoltHistory)`` (and gets ``CapabilityUnsupported`` on any adapter
that lacks one). The shapes are final as settled by ``bh-3kl1k``; their consumers are root's
``fence_audit``, ``fence_orphan``, ``fence_cutover``, ``hive_forward`` and the placement
``conformance``. ``DoltFramePrivateRefs`` stays provisional and is not declared here (ADR
section 4).

* :class:`DoltHistory` -- read-only: the commit graph and the fence tables at a revision.
* :class:`DoltMerge` -- branch movement: the conflict preview, the orphan merge, the working-set
  commit, the one sanctioned rewind, and the fast-forward-only pushes of the branch and of an
  orphan branch.
* :class:`DoltAccounts` -- a server's SQL accounts, grants, sessions, globals, fence identity
  and triggers, plus the HQ schema provisioning.

Every implementation runs over a :class:`SqlRunner` (``query`` / ``execute`` / ``session``), so
the same code serves a hive's fence engine (:func:`engine_runner`), a hive server login (a
:class:`SqlRunner` over root's ``Sql``), an operator's DB-API cursor (:func:`cursor_runner`) and
the HQ connection factory (:func:`connection_runner`). :class:`DoltSqlCapabilities` is the
``CapabilityHost`` over one runner.

**Errors pass through.** A capability raises ``InvalidRequest`` for an argument it refuses
before any I/O, and otherwise lets the runner's exception through unchanged: root's engines
already raise the port's ``StorageUnavailable`` (``SqlFailed``, ``DataUnreachable``), and its
consumers branch on those exact types and messages.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from beadhive_hq_storage import (
    IntegrityViolation,
    InvalidRequest,
    Revision,
    StorageUnavailable,
    WriterRow,
)

from . import _schema as schema
from ._hq import ConnectionFactory, Role
from ._values import quote

__all__ = [
    "AccountRow",
    "CommitInfo",
    "DoltAccounts",
    "DoltHistory",
    "DoltMerge",
    "DoltSqlCapabilities",
    "FenceTable",
    "HistoryRow",
    "MarkRow",
    "MergeRefused",
    "Revocation",
    "ServerIdentity",
    "ServerSession",
    "SqlDoltAccounts",
    "SqlDoltHistory",
    "SqlDoltMerge",
    "SqlRunner",
    "TriggerInfo",
    "WorkingSetResidue",
    "connection_runner",
    "cursor_runner",
    "engine_runner",
    "fence_capabilities",
    "parse_trigger",
]

C = TypeVar("C")

_HASH = re.compile(r"[0-9a-v]{32}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,254}")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}")
_HOST = re.compile(r"[A-Za-z0-9_.%:/-]{1,255}")
#: Any existing account name or host: printable, no quote-breaking control character.
_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,255}")
_NATIVE_HASH = re.compile(r"\*[0-9A-F]{40}")
_PRIVILEGE = re.compile(r"[A-Z][A-Z ]{0,40}")
_GRANTABLE = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRIGGER", "EXECUTE", "ALL", "ALL PRIVILEGES"}
)
#: ``CREATE [DEFINER=...] TRIGGER name {BEFORE|AFTER} {INSERT|UPDATE|DELETE} ON [db.]table FOR EACH
#: ROW [{FOLLOWS|PRECEDES} other] body`` -- the ``dolt_schemas`` fragment of one trigger.
_TRIGGER = re.compile(
    r"^\s*CREATE\s+(?:DEFINER\s*=\s*\S+\s+)?TRIGGER\s+`?(?P<name>\w+)`?\s+"
    r"(?:BEFORE|AFTER)\s+(?:INSERT|UPDATE|DELETE)\s+ON\s+(?:`?\w+`?\.)?`?(?P<table>\w+)`?\s+"
    r"FOR\s+EACH\s+ROW\s+(?:(?:FOLLOWS|PRECEDES)\s+`?\w+`?\s+)?(?P<body>.*?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_IN_CHUNK = 200
#: Push output that means "lost the race" (a non-fast-forward), not "could not reach the remote".
_NON_FF = ("non-fast-forward", "rejected", "fetch first", "behind its remote", "behind the remote")


class FenceTable(StrEnum):
    """The closed set of tables ``DoltHistory`` reads. Bead history is not in it, so the
    capability cannot become a bead-data read (ADR section 4)."""

    BH_WRITER = "bh_writer"
    BH_WRITE_MARK = "bh_write_mark"
    BH_EPOCH_LIVE = "bh_epoch_live"


# --- values -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitInfo:
    """One commit: its hash, ordered parents (first parent first) and full message."""

    commit: Revision
    parents: tuple[Revision, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class HistoryRow:
    commit: Revision
    row: Mapping[str, Any]


@dataclass(frozen=True)
class MarkRow:
    """One ``bh_write_mark`` row."""

    id: str
    epoch: int
    tbl: str = ""


@dataclass(frozen=True)
class WorkingSetResidue:
    """What a merge left in the working set: any constraint-violation or conflict row, and how
    many marks are not at the expected epoch."""

    constraint_violations: bool
    conflicts: bool
    off_epoch_marks: int


@dataclass(frozen=True, order=True)
class AccountRow:
    """One ``mysql.user`` row. ``ssl_type`` is empty when the account does not ``REQUIRE SSL``."""

    user: str
    host: str
    ssl_type: str = ""


@dataclass(frozen=True)
class ServerSession:
    """One ``information_schema.processlist`` session."""

    id: int
    user: str
    host: str


@dataclass(frozen=True)
class ServerIdentity:
    """What a hive server says it is: its ``bh_local_ident`` frame and its ``bh_writer`` row
    (``None`` for either that is absent)."""

    ident: str | None
    writer: WriterRow | None


@dataclass(frozen=True)
class Revocation:
    """Privileges to revoke on one grant target. ``database == "*"`` is server-wide (``*.*``);
    ``table == "*"`` is database-wide. ``grant_option`` revokes ``GRANT OPTION`` first."""

    database: str
    table: str
    privileges: frozenset[str] = field(default_factory=frozenset)
    grant_option: bool = False


@dataclass(frozen=True)
class TriggerInfo:
    """One trigger from ``dolt_schemas``: ``table`` and ``statement`` (the action body) are parsed
    from ``fragment`` (the full ``CREATE TRIGGER``). An unparseable fragment keeps ``table``
    empty and ``statement`` the whole fragment, so a conformance check still reads its text."""

    name: str
    table: str
    statement: str
    fragment: str


class MergeRefused(InvalidRequest):
    """A merge left conflicts or constraint violations; it was aborted, nothing committed."""


# --- the protocols ----------------------------------------------------------------------------


@runtime_checkable
class DoltHistory(Protocol):
    """Read-only commit-graph walks and fence-table reads at a revision (``fence_audit``'s I3
    late-write check, the orphan divert and merge, the cutover record, the epoch floor).
    ``rev`` is a ref or a hash; ``None`` reads the working set."""

    def head(self, ref: str = "HEAD") -> Revision: ...

    def log(
        self, ref: str, *, subject_prefix: str | None = None, limit: int | None = None
    ) -> Sequence[CommitInfo]: ...

    def commit(self, commit: Revision) -> CommitInfo | None: ...

    def parents(self, ref: str) -> Mapping[Revision, tuple[Revision, ...]]: ...

    def messages(self, ref: str, commits: Collection[str]) -> Mapping[Revision, str]: ...

    def ahead(self, ref: str, of: str) -> int: ...

    def merge_base(self, left: str, right: str) -> Revision | None: ...

    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...

    def remote_branches(self, remote: str) -> Mapping[str, Revision]: ...

    def fence_tables_at(self, rev: str) -> frozenset[FenceTable]: ...

    def writer_at(self, rev: str | None) -> WriterRow | None: ...

    def live_epoch_at(self, rev: str | None) -> int: ...

    def stale_marks_at(
        self, rev: str, *, live: int, limit: int
    ) -> tuple[int, tuple[MarkRow, ...]]: ...

    def mark_ids_at(self, rev: str) -> frozenset[str]: ...

    def writer_epochs(self, at: Revision) -> Mapping[Revision, int]: ...

    def history_max_epoch(self) -> int: ...

    def table_history(self, table: FenceTable, *, at: Revision) -> Sequence[HistoryRow]: ...


@runtime_checkable
class DoltMerge(Protocol):
    """Branch movement on one node: the conflict preview, merges, the working-set commit, the
    one sanctioned rewind, and the fast-forward-only pushes (``fence_orphan``,
    ``fence_cutover``, the server-mode reset)."""

    def preview_conflicts(self, base: str, other: str) -> tuple[str, ...]: ...

    def merge(self, rev: str, message: str) -> Revision: ...

    def merge_restamping(self, rev: str, *, epoch: int, message: str) -> None: ...

    def residue(self, *, epoch: int) -> WorkingSetResidue: ...

    def dirty_tables(self) -> tuple[str, ...]: ...

    def commit_working_set(self, message: str) -> bool: ...

    def abort(self) -> None: ...

    def reset_hard(self, rev: str) -> None: ...

    def publish(self) -> bool: ...

    def publish_branch(self, branch: str) -> None: ...


@runtime_checkable
class DoltAccounts(Protocol):
    """A Dolt server's SQL accounts and what they reach (``hive_forward``, the HQ placement
    ``conformance``, HQ provisioning): users, grants, sessions, globals, the server's fence
    identity and its triggers, plus the HQ schema."""

    def provision_hq_schema(self, *, generation: str, author: str) -> Revision: ...

    def users(self) -> tuple[AccountRow, ...]: ...

    def create_user(
        self,
        user: str,
        host: str,
        *,
        password: str | None = None,
        native_hash: str | None = None,
        require_ssl: bool = False,
        replace: bool = False,
    ) -> None: ...

    def set_password(self, user: str, host: str, *, native_hash: str) -> None: ...

    def drop_user(self, user: str, host: str, *, if_exists: bool = False) -> None: ...

    def grants(self, user: str, host: str) -> tuple[str, ...]: ...

    def grant(
        self,
        user: str,
        host: str,
        tables: Mapping[str, Collection[str]],
        *,
        database: str | None = None,
    ) -> tuple[str, ...]: ...

    def revoke(
        self, user: str, host: str, revocations: Sequence[Revocation]
    ) -> tuple[str, ...]: ...

    def base_tables(self) -> tuple[str, ...]: ...

    def sessions(self) -> tuple[ServerSession, ...]: ...

    def kill(self, session: int) -> None: ...

    def global_variable(self, name: str) -> str | None: ...

    def server_identity(self) -> ServerIdentity: ...

    def triggers(self, database: str | None = None) -> Mapping[str, TriggerInfo]: ...


# --- runners ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlRunner:
    """``query(sql) -> rows as dicts`` and ``execute(statements)`` (in order). ``session`` runs
    a batch in ONE SQL session (session variables persist across it); without it ``execute``
    does, which is one session for every runner here except root's server engine."""

    query: Callable[[str], list[dict]]
    execute: Callable[[Sequence[str]], None]
    session: Callable[[Sequence[str]], None] | None = None

    def run_session(self, statements: Sequence[str]) -> None:
        (self.session or self.execute)(statements)


def engine_runner(engine: Any) -> SqlRunner:
    """A runner over a fence statement engine (root's ``DoltCliEngine`` / ``BdServerEngine`` or
    :class:`~beadhive_hq_storage_dolt.SqlConnectionFenceEngine`)."""
    session = getattr(engine, "execute_session", None)
    return SqlRunner(
        query=lambda sql: engine.query(sql),
        execute=lambda statements: engine.execute(statements),
        session=None
        if session is None
        else (lambda statements: engine.execute_session(statements)),
    )


def _run_on_cursor(cursor: Any, statements: Sequence[str], rows: bool) -> list[dict]:
    out: list[dict] = []
    for statement in statements:
        cursor.execute(statement)
        description = getattr(cursor, "description", None)
        if rows and description:
            names = [d[0] for d in description]
            out = [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]
    return out


def connection_runner(connect: ConnectionFactory, role: Role = Role.OPERATOR) -> SqlRunner:
    """A runner over the HQ connection factory: each call owns one connection and commits."""

    def run(statements: Sequence[str], rows: bool) -> list[dict]:
        connection = connect(role)
        try:
            out = _run_on_cursor(connection.cursor(), statements, rows)
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


def cursor_runner(cursor: Any) -> SqlRunner:
    """A runner over a caller's open DB-API cursor. It never commits, rolls back or closes: the
    transaction and the connection stay the caller's."""

    def execute(statements: Sequence[str]) -> None:
        _run_on_cursor(cursor, statements, False)

    return SqlRunner(query=lambda sql: _run_on_cursor(cursor, [sql], True), execute=execute)


# --- argument checks (refused before any I/O) --------------------------------------------------


def _ref(value: object, what: str = "revision") -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value) or ".." in value:
        raise InvalidRequest(f"{what} {value!r} is not a plain Dolt ref or hash")
    return value


def _hash(value: object, what: str = "commit") -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise InvalidRequest(f"{what} {value!r} is not an exact Dolt commit hash")
    return value


def _ident(value: object, what: str) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise InvalidRequest(f"{what} {value!r} is not a plain SQL identifier")
    return value


def _count(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidRequest(f"{what} must be a non-negative integer")
    return value


def _first(rows: list[dict]) -> Any:
    return next(iter(rows[0].values()), None) if rows and rows[0] else None


def _ci(row: Mapping, name: str) -> Any:
    """A column by case-insensitive name (``bd sql --json`` and PyMySQL disagree on case)."""
    for key, value in row.items():
        if str(key).lower() == name:
            return value
    return None


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true"}


def _parents(value: object) -> tuple[Revision, ...]:
    return tuple(Revision(p.strip()) for p in str(value or "").split(",") if p.strip())


def _as_of(rev: str | None) -> str:
    return "" if rev is None else f" AS OF {quote(_ref(rev))}"


# --- DoltHistory ------------------------------------------------------------------------------


class SqlDoltHistory:
    """:class:`DoltHistory` over a :class:`SqlRunner`."""

    def __init__(self, runner: SqlRunner) -> None:
        self._run = runner

    def _query(self, sql: str) -> list[dict]:
        return self._run.query(sql)

    def head(self, ref: str = "HEAD") -> Revision:
        """The exact commit ``ref`` names. ``StorageUnavailable`` when the answer is not a
        commit hash (an unresolvable ref)."""
        text = str(_first(self._query(f"SELECT hashof({quote(_ref(ref, 'ref'))}) AS h")) or "")
        if not _HASH.fullmatch(text):
            raise StorageUnavailable(f"hashof({ref}) is not a commit hash: {text!r}")
        return Revision(text)

    def log(
        self, ref: str, *, subject_prefix: str | None = None, limit: int | None = None
    ) -> Sequence[CommitInfo]:
        """Commits reachable from ``ref``, newest first; with ``subject_prefix`` only those
        whose message starts with it."""
        sql = (
            "SELECT commit_hash AS c, parents AS p, message AS m "
            f"FROM dolt_log('--parents', {quote(_ref(ref, 'ref'))})"
        )
        if subject_prefix is not None:
            if not isinstance(subject_prefix, str) or not subject_prefix:
                raise InvalidRequest("subject_prefix must be a non-empty string")
            escaped = subject_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += f" WHERE message LIKE {quote(escaped + '%')}"
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise InvalidRequest("limit must be a positive integer")
            sql += f" LIMIT {limit}"
        return [
            CommitInfo(Revision(str(r["c"])), _parents(r.get("p")), str(r.get("m") or ""))
            for r in self._query(sql)
        ]

    def commit(self, commit: Revision) -> CommitInfo | None:
        """One exact commit's parents and message (``None`` when it is not in its own log)."""
        h = quote(_hash(commit))
        rows = self._query(
            f"SELECT parents AS p, message AS m FROM dolt_log('--parents', {h}) "
            f"WHERE commit_hash = {h}"
        )
        if not rows:
            return None
        return CommitInfo(Revision(commit), _parents(rows[0].get("p")), str(rows[0].get("m") or ""))

    def parents(self, ref: str) -> Mapping[Revision, tuple[Revision, ...]]:
        """``{commit: ordered parents}`` for every commit reachable from ``ref``, in one read."""
        rows = self._query(
            f"SELECT commit_hash AS c, parents AS p FROM dolt_log('--parents', {quote(_ref(ref))})"
        )
        return {Revision(str(r["c"])): _parents(r.get("p")) for r in rows}

    def messages(self, ref: str, commits: Collection[str]) -> Mapping[Revision, str]:
        """Messages of the given exact commits reachable from ``ref`` (chunked ``IN`` reads)."""
        wanted = sorted({_hash(c) for c in commits})
        out: dict[Revision, str] = {}
        for i in range(0, len(wanted), _IN_CHUNK):
            listed = ", ".join(quote(c) for c in wanted[i : i + _IN_CHUNK])
            for row in self._query(
                f"SELECT commit_hash, message FROM dolt_log({quote(_ref(ref))}) "
                f"WHERE commit_hash IN ({listed})"
            ):
                out[Revision(str(row["commit_hash"]))] = str(row.get("message") or "")
        return out

    def ahead(self, ref: str, of: str) -> int:
        """How many commits ``ref`` reaches that ``of`` does not."""
        rows = self._query(
            f"SELECT COUNT(*) AS n FROM dolt_log({quote(_ref(ref))}, '--not', {quote(_ref(of))})"
        )
        return int(_first(rows) or 0)

    def merge_base(self, left: str, right: str) -> Revision | None:
        found = _first(
            self._query(f"SELECT dolt_merge_base({quote(_ref(left))}, {quote(_ref(right))}) AS b")
        )
        return None if not found else Revision(str(found))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether ``ancestor`` is contained in ``descendant`` (one read)."""
        a = quote(_ref(ancestor))
        rows = self._query(
            f"SELECT dolt_merge_base({a}, {quote(_ref(descendant))}) = hashof({a}) AS is_ancestor"
        )
        return _truthy(_first(rows))

    def remote_branches(self, remote: str) -> Mapping[str, Revision]:
        """``{branch: hash}`` of ``remote``'s tracking branches (as of the last fetch)."""
        prefix = f"remotes/{_ref(remote, 'remote')}/"
        out: dict[str, Revision] = {}
        for row in self._query("SELECT name, hash FROM dolt_remote_branches"):
            name = str(row.get("name") or "")
            if name.startswith(prefix):
                out[name[len(prefix) :]] = Revision(str(row.get("hash") or ""))
        return out

    def fence_tables_at(self, rev: str) -> frozenset[FenceTable]:
        names = {str(_first([row])) for row in self._query(f"SHOW TABLES{_as_of(rev)}")}
        return frozenset(t for t in FenceTable if t.value in names)

    def writer_at(self, rev: str | None) -> WriterRow | None:
        """``bh_writer`` at ``rev`` (``None``: the working set); ``None`` when that revision
        carries no ``bh_writer`` table or row."""
        try:
            rows = self._query(f"SELECT frame, epoch FROM bh_writer{_as_of(rev)} WHERE id = 1")
        except Exception as exc:
            if "not found" in str(exc).lower():
                return None
            raise
        if not rows:
            return None
        return WriterRow(str(_ci(rows[0], "frame")), int(_ci(rows[0], "epoch")))

    def live_epoch_at(self, rev: str | None) -> int:
        rows = self._query(f"SELECT epoch FROM bh_epoch_live{_as_of(rev)} WHERE id = 1")
        return int(_first(rows) or 0)

    def stale_marks_at(self, rev: str, *, live: int, limit: int) -> tuple[int, tuple[MarkRow, ...]]:
        """How many marks at ``rev`` are not at ``live``, and the first ``limit`` of them (a
        bound value, never a subquery against ``bh_epoch_live``)."""
        at, epoch = _as_of(rev), _count(live, "live epoch")
        count = int(
            _first(
                self._query(f"SELECT COUNT(*) AS n FROM bh_write_mark{at} WHERE epoch <> {epoch}")
            )
            or 0
        )
        rows = self._query(
            f"SELECT id, epoch, tbl FROM bh_write_mark{at} WHERE epoch <> {epoch} "
            f"ORDER BY epoch, id LIMIT {_count(limit, 'limit')}"
        )
        return count, tuple(
            MarkRow(str(r["id"]), int(r["epoch"]), str(r.get("tbl") or "")) for r in rows
        )

    def mark_ids_at(self, rev: str) -> frozenset[str]:
        return frozenset(
            str(r["id"]) for r in self._query(f"SELECT id FROM bh_write_mark{_as_of(rev)}")
        )

    def writer_epochs(self, at: Revision) -> Mapping[Revision, int]:
        """``{commit: bh_writer.epoch}`` for every commit in ``dolt_history_bh_writer`` as of
        the exact commit ``at``. Never ``dolt_diff_bh_writer``: it omits commits on merged
        histories (measured on Dolt 2.3.5)."""
        revision = self._revision_db(at)
        return {
            Revision(str(row["c"])): int(row["e"])
            for row in self._query(
                f"SELECT commit_hash AS c, epoch AS e FROM {revision}.dolt_history_bh_writer "
                "WHERE id = 1"
            )
        }

    def history_max_epoch(self) -> int:
        """The highest ``bh_writer.epoch`` the current branch's history ever carried (0 if
        none). One row per commit from ``dolt_history_bh_writer``, never ``dolt_diff``."""
        try:
            rows = self._query(
                "SELECT COALESCE(MAX(epoch), 0) AS m FROM dolt_history_bh_writer WHERE id = 1"
            )
        except Exception as exc:
            text = str(exc).lower()
            if "not found" in text and "bh_writer" in text:
                return 0
            raise
        return int(_first(rows) or 0)

    def table_history(self, table: FenceTable, *, at: Revision) -> Sequence[HistoryRow]:
        if not isinstance(table, FenceTable):
            raise InvalidRequest("table_history reads fence tables only (a FenceTable member)")
        revision = self._revision_db(at)
        rows = self._query(f"SELECT * FROM {revision}.dolt_history_{table.value}")
        out = []
        for row in rows:
            commit = Revision(str(row.get("commit_hash")))
            body = {
                k: v for k, v in row.items() if k not in ("commit_hash", "committer", "commit_date")
            }
            out.append(HistoryRow(commit, body))
        return out

    def _revision_db(self, at: object) -> str:
        if not isinstance(at, str) or not _HASH.fullmatch(at):
            raise InvalidRequest("a history read needs an exact commit hash")
        database = str(_first(self._query("SELECT database() AS d")) or "")
        if not database or "`" in database:
            raise IntegrityViolation(f"unusable database name {database!r}")
        return f"`{database}/{at}`"


# --- DoltMerge --------------------------------------------------------------------------------


class SqlDoltMerge:
    """:class:`DoltMerge` over a :class:`SqlRunner`.

    ``engine`` is root's fence engine when there is one: its own ``commit``, ``push``,
    ``push_branch`` and ``reset_hard`` win, because they own the embedded CLI calls, bd's server
    commit and the server-mode quiesce before a reset. Without it every operation is SQL on
    ``remote`` / ``branch``."""

    def __init__(
        self,
        runner: SqlRunner,
        *,
        remote: str = "origin",
        branch: str = "main",
        engine: Any = None,
    ) -> None:
        self._run = runner
        self._remote = _ref(remote, "remote")
        self._branch = _ref(branch, "branch")
        self._engine = engine

    def _delegate(self, name: str) -> Callable[..., Any] | None:
        return None if self._engine is None else getattr(self._engine, name, None)

    def preview_conflicts(self, base: str, other: str) -> tuple[str, ...]:
        """Tables a merge of ``other`` into ``base`` would leave a data or schema conflict on,
        sorted (nothing is merged)."""
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

    def merge_restamping(self, rev: str, *, epoch: int, message: str) -> None:
        """The writer's orphan merge, in ONE session: ``DOLT_MERGE --no-ff --no-commit`` of
        ``rev`` with ``dolt_force_transaction_commit`` set for that session only, every mark
        not at ``epoch`` re-stamped to it, the mark table's violation rows cleared, and the
        merge committed as ``message``.

        A batch may not stop at a failing statement (bd's server batch), so every statement
        after the merge is guarded in SQL on the merge actually being open: a failed
        ``DOLT_MERGE`` re-stamps nothing, deletes nothing and commits nothing (a NULL message
        aborts ``DOLT_COMMIT``). The caller verifies the outcome (:meth:`residue`)."""
        live = _count(epoch, "epoch")
        if not isinstance(message, str) or not message or ";" in message:
            raise InvalidRequest("a merge message must be non-empty and carry no ';'")
        self._run.run_session(
            [
                "SET @@dolt_force_transaction_commit = 1",
                f"CALL DOLT_MERGE('--no-ff', '--no-commit', {quote(_ref(rev))})",
                "SET @bh_merging = (SELECT COUNT(*) FROM dolt_merge_status WHERE is_merging)",
                f"UPDATE bh_write_mark SET epoch = {live} "
                f"WHERE epoch <> {live} AND @bh_merging > 0",
                "DELETE FROM dolt_constraint_violations_bh_write_mark WHERE @bh_merging > 0",
                f"CALL DOLT_COMMIT('-Am', IF(@bh_merging > 0, {quote(message)}, NULL))",
            ]
        )

    def residue(self, *, epoch: int) -> WorkingSetResidue:
        live = _count(epoch, "epoch")
        violations = bool(self._run.query("SELECT `table` FROM dolt_constraint_violations"))
        conflicts = bool(self._run.query("SELECT `table` FROM dolt_conflicts"))
        stale = self._run.query(f"SELECT COUNT(*) AS n FROM bh_write_mark WHERE epoch <> {live}")
        return WorkingSetResidue(violations, conflicts, int(_first(stale) or 0))

    def dirty_tables(self) -> tuple[str, ...]:
        """Tables ``dolt_status`` lists (staged or not), sorted and distinct."""
        rows = self._run.query("SELECT table_name FROM dolt_status")
        return tuple(sorted({str(r.get("table_name", "")) for r in rows}))

    def commit_working_set(self, message: str) -> bool:
        """Commit the whole working set (``-A``); ``False`` when there was nothing to commit."""
        commit = self._delegate("commit")
        if commit is not None:
            return bool(commit(message))
        if not self._run.query("SELECT table_name FROM dolt_status"):
            return False
        self._run.execute([f"CALL DOLT_COMMIT('-Am', {quote(message)})"])
        return True

    def abort(self) -> None:
        merging = self._run.query("SELECT is_merging AS m FROM dolt_merge_status")
        if merging and _truthy(_first(merging)):
            self._run.execute(["CALL DOLT_MERGE('--abort')"])

    def reset_hard(self, rev: str) -> None:
        """Abort any open merge, then hard-reset the branch to ``rev`` -- the one sanctioned
        rewind. Root's server engine kills forwarder sessions first."""
        reset = self._delegate("reset_hard")
        if reset is not None:
            reset(_ref(rev))
            return
        self.abort()
        self._run.execute([f"CALL DOLT_RESET('--hard', {quote(_ref(rev))})"])

    def publish(self) -> bool:
        """Push the branch to the remote, fast-forward only. ``False`` on a non-fast-forward
        rejection; any other failure raises."""
        push = self._delegate("push")
        if push is not None:
            return bool(push())
        try:
            self._run.execute([f"CALL DOLT_PUSH({quote(self._remote)}, {quote(self._branch)})"])
        except Exception as exc:
            if any(marker in str(exc).lower() for marker in _NON_FF):
                return False
            raise
        return True

    def publish_branch(self, branch: str) -> None:
        """Push the local branch to the remote branch ``branch`` (the orphan divert)."""
        target = _ref(branch, "branch")
        push = self._delegate("push_branch")
        if push is not None:
            push(target)
            return
        refspec = quote(f"{self._branch}:{target}")
        self._run.execute([f"CALL DOLT_PUSH({quote(self._remote)}, {refspec})"])


# --- DoltAccounts -----------------------------------------------------------------------------


def _user(value: object, strict: bool) -> str:
    if not isinstance(value, str) or not (_USER if strict else _NAME).fullmatch(value):
        raise InvalidRequest(f"user {value!r} is not a plain login name")
    return value


def _host(value: object, strict: bool) -> str:
    if not isinstance(value, str) or not (_HOST if strict else _NAME).fullmatch(value):
        raise InvalidRequest(f"host {value!r} is not a plain account host")
    return value


def _account(user: str, host: str, *, strict: bool = False) -> str:
    """``'user'@'host'``, quoted. ``strict`` (an account this capability creates) also refuses
    anything but a plain name and host; a read or drop takes any existing account's name."""
    return f"{quote(_user(user, strict))}@{quote(_host(host, strict))}"


def _native(value: object) -> str:
    if not isinstance(value, str) or not _NATIVE_HASH.fullmatch(value):
        raise InvalidRequest("a mysql_native_password hash is '*' and 40 upper-hex digits")
    return value


def _privileges(values: Collection[str], allowed: frozenset[str] | None) -> list[str]:
    wanted = sorted({str(p).strip().upper() for p in values})
    bad = [p for p in wanted if not _PRIVILEGE.fullmatch(p) or (allowed and p not in allowed)]
    if not wanted or bad:
        raise InvalidRequest(f"unsupported privileges {sorted(values)!r}")
    return wanted


def _target(database: str, table: str) -> str:
    if database == "*":
        return "*.*"
    db = _ident(database, "database")
    return f"`{db}`.*" if table == "*" else f"`{db}`.`{_ident(table, 'table')}`"


def parse_trigger(name: str, fragment: str) -> TriggerInfo:
    """A ``dolt_schemas`` trigger fragment as :class:`TriggerInfo`."""
    match = _TRIGGER.match(fragment or "")
    if match is None:
        return TriggerInfo(name, "", fragment or "", fragment or "")
    return TriggerInfo(name, match.group("table"), match.group("body"), fragment)


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

    def users(self) -> tuple[AccountRow, ...]:
        rows = self._run.query("SELECT user, host, ssl_type FROM mysql.user")
        return tuple(
            AccountRow(str(_ci(r, "user")), str(_ci(r, "host")), str(_ci(r, "ssl_type") or ""))
            for r in rows
        )

    def create_user(
        self,
        user: str,
        host: str,
        *,
        password: str | None = None,
        native_hash: str | None = None,
        require_ssl: bool = False,
        replace: bool = False,
    ) -> None:
        """Create ``user@host`` from exactly one of a plaintext ``password`` or its
        ``mysql_native_password`` hash (so plaintext never has to cross). ``replace`` drops the
        account first, in the same batch (Dolt 2.3.5 cannot add ``REQUIRE SSL`` in place)."""
        account = _account(user, host, strict=True)
        if (password is None) == (native_hash is None):
            raise InvalidRequest("a SQL account needs exactly one of a password or its hash")
        if password is not None:
            if not isinstance(password, str) or not password:
                raise InvalidRequest("a SQL account needs a password")
            identified = f"IDENTIFIED BY {quote(password)}"
        else:
            identified = f"IDENTIFIED WITH mysql_native_password AS {quote(_native(native_hash))}"
        statements = [f"DROP USER {account}"] if replace else []
        tls = " REQUIRE SSL" if require_ssl else ""
        statements.append(f"CREATE USER {account} {identified}{tls}")
        self._run.execute(statements)

    def set_password(self, user: str, host: str, *, native_hash: str) -> None:
        self._run.execute(
            [
                f"ALTER USER {_account(user, host)} IDENTIFIED WITH mysql_native_password AS "
                f"{quote(_native(native_hash))}"
            ]
        )

    def drop_user(self, user: str, host: str, *, if_exists: bool = False) -> None:
        exists = "IF EXISTS " if if_exists else ""
        self._run.execute([f"DROP USER {exists}{_account(user, host)}"])

    def grants(self, user: str, host: str) -> tuple[str, ...]:
        rows = self._run.query(f"SHOW GRANTS FOR {_account(user, host)}")
        return tuple(str(_first([row])) for row in rows)

    def grant(
        self,
        user: str,
        host: str,
        tables: Mapping[str, Collection[str]],
        *,
        database: str | None = None,
    ) -> tuple[str, ...]:
        """Grant table-scoped privileges, one statement per table in ``tables`` order, in one
        batch. ``database`` defaults to the session's. Returns the statements it ran."""
        account = _account(user, host)
        if database is None:
            database = str(_first(self._run.query("SELECT DATABASE() AS d")) or "")
        db = _ident(database, "database")
        statements = [
            f"GRANT {', '.join(_privileges(privileges, _GRANTABLE))} ON "
            f"`{db}`.`{_ident(table, 'table')}` TO {account}"
            for table, privileges in tables.items()
        ]
        if statements:
            self._run.execute(statements)
        return tuple(statements)

    def revoke(self, user: str, host: str, revocations: Sequence[Revocation]) -> tuple[str, ...]:
        """Revoke, in order and in one batch: per target its grant option first, then its
        privileges. Returns the statements it ran."""
        account = _account(user, host)
        statements: list[str] = []
        for item in revocations:
            target = _target(item.database, item.table)
            if item.grant_option:
                statements.append(f"REVOKE GRANT OPTION ON {target} FROM {account}")
            if item.privileges:
                statements.append(
                    f"REVOKE {', '.join(_privileges(item.privileges, None))} ON {target} "
                    f"FROM {account}"
                )
        if statements:
            self._run.execute(statements)
        return tuple(statements)

    def base_tables(self) -> tuple[str, ...]:
        """Base table names, sorted. The name column is ``Tables_in_<db>``; ``bd sql --json``
        does not keep column order, so it is found by name, never by position."""
        rows = self._run.query("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        names = []
        for row in rows:
            name = next(
                (v for k, v in row.items() if str(k).lower().startswith("tables_in_")), None
            )
            if name is None:
                raise IntegrityViolation(f"unrecognised SHOW FULL TABLES row: {dict(row)!r}"[:200])
            names.append(str(name))
        return tuple(sorted(names))

    def sessions(self) -> tuple[ServerSession, ...]:
        rows = self._run.query("SELECT id, user, host FROM information_schema.processlist")
        return tuple(
            ServerSession(int(_ci(r, "id")), str(_ci(r, "user") or ""), str(_ci(r, "host") or ""))
            for r in rows
        )

    def kill(self, session: int) -> None:
        self._run.execute([f"KILL {_count(session, 'session id')}"])

    def global_variable(self, name: str) -> str | None:
        """``@@GLOBAL.<name>`` as text (``None`` when the server returns no row). Read-only."""
        rows = self._run.query(f"SELECT @@GLOBAL.{_ident(name, 'global variable')} AS v")
        return str(_first(rows)) if rows else None

    def server_identity(self) -> ServerIdentity:
        """The server's ``bh_local_ident`` frame and ``bh_writer`` row: exactly the two reads a
        forwarder's table-scoped grants allow."""
        ident = self._run.query("SELECT frame FROM bh_local_ident WHERE id = 1")
        writer = self._run.query("SELECT frame, epoch FROM bh_writer WHERE id = 1")
        return ServerIdentity(
            ident=str(_ci(ident[0], "frame")) if ident else None,
            writer=(
                WriterRow(str(_ci(writer[0], "frame")), int(_ci(writer[0], "epoch")))
                if writer
                else None
            ),
        )

    def triggers(self, database: str | None = None) -> Mapping[str, TriggerInfo]:
        """``{name: TriggerInfo}`` from ``dolt_schemas`` (the working set of ``database``, or of
        the session's), never ``information_schema.triggers``: on Dolt 2.3.5 that fails for a
        committed trigger whose table is ``dolt_ignore``'d, because it resolves the table at
        ``HEAD``."""
        source = (
            "dolt_schemas" if database is None else f"`{_ident(database, 'database')}`.dolt_schemas"
        )
        rows = self._run.query(
            f"SELECT name AS n, fragment AS f FROM {source} WHERE type = 'trigger'"
        )
        return {str(r["n"]): parse_trigger(str(r["n"]), str(r.get("f") or "")) for r in rows}


# --- the host ---------------------------------------------------------------------------------


class DoltSqlCapabilities:
    """``CapabilityHost`` over one :class:`SqlRunner`: :class:`DoltHistory`, :class:`DoltMerge`
    and :class:`DoltAccounts`, each only when enabled (``None`` otherwise). ``engine`` (root's
    fence engine) backs ``DoltMerge``'s commit, pushes and reset."""

    def __init__(
        self,
        runner: SqlRunner,
        *,
        remote: str = "origin",
        branch: str = "main",
        engine: Any = None,
        history: bool = True,
        merge: bool = True,
        accounts: bool = True,
    ) -> None:
        self._runner = runner
        self._remote, self._branch, self._engine = remote, branch, engine
        self._enabled: dict[type, bool] = {
            DoltHistory: history,
            DoltMerge: merge,
            DoltAccounts: accounts,
        }
        self._built: dict[type, object] = {}

    def register_capability(self, kind: type[C], implementation: C) -> None:
        self._built[kind] = implementation

    def capability(self, kind: type[C]) -> C | None:
        if kind not in self._built:
            if not self._enabled.get(kind, False):
                return None
            self._built[kind] = self._build(kind)
        return self._built[kind]  # type: ignore[return-value]

    def _build(self, kind: type) -> object:
        if kind is DoltHistory:
            return SqlDoltHistory(self._runner)
        if kind is DoltMerge:
            return SqlDoltMerge(
                self._runner, remote=self._remote, branch=self._branch, engine=self._engine
            )
        return SqlDoltAccounts(self._runner)


def default_hq_capability(kind: type, connect: ConnectionFactory) -> object | None:
    """The HQ store's built-in capability for ``kind``, or ``None``."""
    if kind is DoltHistory:
        return SqlDoltHistory(connection_runner(connect, Role.READER))
    if kind is DoltMerge:
        return SqlDoltMerge(connection_runner(connect, Role.OPERATOR))
    if kind is DoltAccounts:
        return SqlDoltAccounts(connection_runner(connect, Role.OPERATOR))
    return None


def fence_capabilities(engine: Any, *, remote: str, branch: str) -> DoltSqlCapabilities:
    """A hive fence's built-in capabilities over its engine: ``DoltHistory`` and ``DoltMerge``.
    ``DoltAccounts`` belongs to a server login (root composes it from that login), never to a
    fence."""
    return DoltSqlCapabilities(
        engine_runner(engine), remote=remote, branch=branch, engine=engine, accounts=False
    )
