"""The forward write path, option A (bh-g7dlo, P-M10; ADR §3, condition 16, Decision 6).

``docs/design/hive-writer-partitioning-adr.md`` §3: execution frames and agents that claim,
create or close point bd at the current primary's hive ``dolt sql-server``. Claims are then
granted in one place, so bd's claim, heartbeat and reclaim work as designed (``bh-sieai`` E8).
Option A narrows and detects; it does not prevent. ``docs/FORWARD-WRITE-PATH.md`` is the
operator guide. This module is the Typer-free core; ``hive_forward_cli`` renders it.

**The primary's side** (the frame whose hive server takes forwarders):

* :func:`provision` — one host-pinned account per forwarding frame,
  ``'<principal>'@'<frame address>'``, never a shared or root login (:func:`account_problems`),
  created ``REQUIRE SSL`` by default. Its grants are **table-scoped, never database-wide**
  (M13, ``bh-uhx2r`` E2–E4; :func:`grant_plan`): DML on bd's tables and ``dolt_ignore``;
  ``SELECT`` on the fence tables (``bh_writer``, ``bh_epoch_live``, ``bh_local_ident``);
  ``SELECT, INSERT`` on ``bh_write_mark`` (the guard trigger's inline insert is
  invoker-checked, ``bh-wtsrc`` E3); no database-level grant, so no ``DOLT_COMMIT`` right — bd's
  post-write commit defers to the primary's next commit. The table list is re-derived from
  ``SHOW FULL TABLES`` on every run, so re-provisioning regrants when bd adds a table. The
  password crosses only as its ``mysql_native_password`` hash, never as plaintext in an argv.
* :func:`conformance` — every non-operator account on the server is a forwarder and must hold
  exactly that shape or less. Database-wide, server-wide, ``ALL``, grant option, a write on a
  fence table, an unpinned host, a shared principal or a missing ``REQUIRE SSL`` are findings.
  :func:`provision` refuses (:class:`GrantShapeViolation`) when its own result fails it, and
  ``bh doctor`` on a forwarding primary reports it.
* :func:`check_globals` and :func:`root_problems` — what the operator watchdog
  (``scripts/dolt_globals_watchdog.py``) checks, read through the same channel, for ``bh doctor``.
  :data:`WATCHED_GLOBALS` equals the watchdog's ``DEFAULT_WATCHED`` (a unit test pins it).
* :func:`quiesce` — before a demoted primary's divert reset (``DOLT_RESET --hard``), every
  forwarder session on its hive server is killed, so an in-flight forwarded transaction is
  refused (the client sees a lost connection) instead of acknowledged and then silently dropped
  by the reset (``bh-uhx2r`` E5). :class:`beadhive.fence_data.BdServerEngine` calls
  :func:`quiesce_before_reset` from ``reset_to_remote``.

**The forwarder's side** (an executor frame that is not the hive's primary):

* :func:`resolve_target` picks the current primary from placement and this frame's configured
  endpoints; :func:`preflight` reads, over the forwarder's own TLS login, the target's
  ``bh_local_ident`` and ``bh_writer`` and refuses (:class:`ForwardRefused`) unless both name the
  placed frame at the placed epoch. A demoted primary fails closed.
* :func:`point` records a git-private marker (:func:`marker_path`) naming the target and the
  placement it was pointed for; :func:`bd_env` turns it into the environment bh gives every bd
  it runs in that checkout (server mode on the target, TLS with the endpoint's CA, the
  forwarder's principal and password). bd's tracked ``metadata.json`` is never edited.
  :func:`ensure_current` re-points when the cached placement moved; :func:`refuse` records a
  refusal, and bh then refuses to run bd for the hive until a re-point passes. A raw ``bd`` on
  the forwarder writes its own replica, whose write guard refuses: it fails closed too.

Forwarding is opt-in per frame (``host.forward.enabled``); serving forwarders is opt-in per
frame (``host.forward.serve.enabled``). Every default here is configurable there.

**Residual trust** (:data:`RESIDUAL_TRUST`, carried into the 0.23.0 release note by M11):
table-scoped grants cannot stop a forwarder issuing ``SET GLOBAL`` / ``SET PERSIST`` or editing
any bead row of the hive. Detection is the globals watchdog plus ``fence_audit``'s history check.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .fence_schema import FENCE_TABLES, LOCAL_IDENT_TABLE, quote

__all__ = [
    "DEFAULT_OPERATORS",
    "DML",
    "FENCE_READ_TABLES",
    "IGNORE_TABLE",
    "MARKER",
    "MARK_TABLE",
    "PASSWORD_ENV",
    "QUIESCE_ENV",
    "RESIDUAL_TRUST",
    "SYSTEM_ACCOUNTS",
    "WATCHED_GLOBALS",
    "Account",
    "DbapiSql",
    "ForwardError",
    "ForwardRefused",
    "ForwardTarget",
    "GlobalResult",
    "GrantShapeViolation",
    "Marker",
    "ProvisionReport",
    "Session",
    "Sql",
    "account_problems",
    "bd_env",
    "check_forwarder_grants",
    "check_globals",
    "conformance",
    "ensure_current",
    "forwarder_sessions",
    "marker_path",
    "grant_plan",
    "native_password_hash",
    "point",
    "preflight",
    "primary_report",
    "provision",
    "quiesce",
    "quiesce_before_reset",
    "read_marker",
    "refuse",
    "resolve_target",
    "revoke",
    "root_problems",
    "serve_settings",
    "stop",
    "watched_globals",
]

#: Condition 16's watched list as amended by M13 (``bh-uhx2r`` E1, ``bh-wtsrc`` E4). Equal to
#: ``scripts/dolt_globals_watchdog.py``'s ``DEFAULT_WATCHED`` and
#: ``deploy/dolt/watched-globals.json`` (``tests/test_dolt_globals_watchdog.py`` pins all three).
#: ``dolt_allow_commit_conflicts`` is session-only on Dolt 2.3.5 and is not watched.
WATCHED_GLOBALS: dict[str, str] = {
    "dolt_force_transaction_commit": "0",
    "dolt_transaction_commit": "0",
    "read_only": "0",
    "max_connections": "100",
}

#: Dolt's own built-in accounts. Never forwarders, never killed, never provisioned.
SYSTEM_ACCOUNTS = frozenset({"root", "event_scheduler", "__dolt_local_user__"})
#: The default operator logins (configurable: ``host.forward.serve.operators``). ``watchdog`` is
#: the user the deploy templates run the globals watchdog as.
DEFAULT_OPERATORS: tuple[str, ...] = ("root", "watchdog")

#: Fence tables a forwarder may only read. ``bh_local_ident`` is the node's identity: a
#: forwarder that could write it becomes an undetectable second writer (``bh-uhx2r`` E3).
FENCE_READ_TABLES: tuple[str, ...] = ("bh_writer", "bh_epoch_live", LOCAL_IDENT_TABLE)
#: The mark table takes the guard trigger's inline INSERT, which runs as the invoker.
MARK_TABLE = "bh_write_mark"
#: bd seeds ``dolt_ignore`` when it opens a database; without DML on it ``bd create`` fails.
IGNORE_TABLE = "dolt_ignore"
DML = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
_READ = frozenset({"SELECT"})
_MARK = frozenset({"SELECT", "INSERT"})
_USAGE = frozenset({"USAGE"})

#: The marker's file name when the checkout is not in git (otherwise :func:`marker_path`).
MARKER = "bh-forward.json"
MARKER_VERSION = 1
#: A forwarder's password when no fnox reference is configured (tests, operator shells).
PASSWORD_ENV = "BH_FORWARD_PASSWORD"
#: ``off`` skips the forwarder-session kill before a divert reset (default on).
QUIESCE_ENV = "BH_FORWARD_QUIESCE"

RESIDUAL_TRUST = (
    "Forwarding frames hold a Dolt login on the primary's hive server. Their grants are "
    "table-scoped (DML on bd's tables and dolt_ignore, SELECT on the fence tables, SELECT and "
    "INSERT on bh_write_mark, no DOLT_COMMIT right), which closes the stale-write and "
    "identity-spoof paths the M13 spike measured. Table-scoped grants cannot stop a forwarder "
    "issuing SET GLOBAL or SET PERSIST, or editing any bead row of the hive. A forwarder is "
    "trusted not to; detection is the globals watchdog plus fence_audit's history check."
)

_PRINCIPAL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}")
_ACCOUNT = re.compile(r"^[`'\"]?(?P<user>[^`'\"@]+)[`'\"]?@[`'\"]?(?P<host>[^`'\"@]+)[`'\"]?$")
_TABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


# =============================================================================================
# Errors
# =============================================================================================


class ForwardError(RuntimeError):
    """The forward path could not be provisioned, checked or pointed."""


class ForwardRefused(ForwardError):
    """Forwarding is refused: fail closed (a demoted primary, no placement, a bad account)."""


class GrantShapeViolation(ForwardError):
    """An account's grants exceed the table-scoped forwarder shape."""

    def __init__(self, message: str, problems: Sequence[str] = ()):
        super().__init__(message)
        self.problems = list(problems)


# =============================================================================================
# The SQL port
# =============================================================================================


class Sql(Protocol):
    """One server, one login. :class:`beadhive.fence_data.BdServerEngine` (``bd sql``) and
    :class:`DbapiSql` both implement it."""

    def query(self, sql: str) -> list[dict]: ...

    def execute(self, statements: Sequence[str]) -> None: ...


class DbapiSql:
    """:class:`Sql` over a DB-API connection (PyMySQL). Autocommit per statement."""

    def __init__(self, connection):
        self.connection = connection

    def query(self, sql: str) -> list[dict]:
        with self.connection.cursor() as cur:
            cur.execute(sql)
            names = [d[0] for d in (cur.description or ())]
            rows = [dict(zip(names, row, strict=False)) for row in cur.fetchall()]
        self._commit()
        return rows

    def execute(self, statements: Sequence[str]) -> None:
        with self.connection.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        self._commit()

    def _commit(self) -> None:
        with contextlib.suppress(Exception):
            self.connection.commit()


def _first(row: Mapping) -> Any:
    return next(iter(row.values())) if row else None


def _ci(row: Mapping, name: str) -> Any:
    """A column by case-insensitive name (``bd sql --json`` and PyMySQL disagree on case)."""
    for key, value in row.items():
        if str(key).lower() == name:
            return value
    return None


# =============================================================================================
# Accounts
# =============================================================================================


@dataclass(frozen=True, order=True)
class Account:
    """``'<principal>'@'<frame address>'``."""

    user: str
    host: str

    @classmethod
    def parse(cls, text: str) -> Account:
        match = _ACCOUNT.match(text.strip())
        if match is None:
            raise ValueError(f"not an account (want '<principal>'@'<frame address>'): {text!r}")
        return cls(match.group("user"), match.group("host"))

    @property
    def sql(self) -> str:
        return f"{quote(self.user)}@{quote(self.host)}"

    def __str__(self) -> str:
        return f"'{self.user}'@'{self.host}'"


def _operators(operators: Iterable[str]) -> frozenset[str]:
    return SYSTEM_ACCOUNTS | frozenset(str(o) for o in operators)


def account_problems(account: Account, operators: Iterable[str] = DEFAULT_OPERATORS) -> list[str]:
    """Why ``account`` cannot be a forwarder login (empty when it can).

    A forwarder account is per frame and host-pinned: its principal is not an operator, system
    or root login (a shared login), and its host is one address, not a pattern."""
    problems = []
    if not _PRINCIPAL.fullmatch(account.user):
        problems.append(f"principal {account.user!r} is not a plain login name")
    if account.user in _operators(operators):
        problems.append(
            f"principal {account.user!r} is a shared, root or operator login; each forwarding "
            "frame gets its own"
        )
    if not account.host or "%" in account.host or "_" in account.host or "/" in account.host:
        problems.append(f"host {account.host!r} is not pinned to one frame address")
    elif not _HOST.fullmatch(account.host):
        problems.append(f"host {account.host!r} is not a plain address")
    return problems


def native_password_hash(password: str) -> str:
    """MySQL ``mysql_native_password`` hash: ``*`` + upper-hex ``SHA1(SHA1(password))``."""
    if not password:
        raise ValueError("a forwarder account needs a non-empty password")
    inner = hashlib.sha1(password.encode()).digest()  # noqa: S324 — the protocol's own hash
    return "*" + hashlib.sha1(inner).hexdigest().upper()  # noqa: S324


# =============================================================================================
# The grant shape (M13: table-scoped, never database-wide)
# =============================================================================================


def table_shape(table: str) -> frozenset[str]:
    """The most a forwarder may hold on ``table``."""
    if table == MARK_TABLE:
        return _MARK
    if table in FENCE_READ_TABLES or table in FENCE_TABLES or table.startswith("bh_"):
        return _READ
    return DML


def grant_plan(tables: Iterable[str]) -> dict[str, frozenset[str]]:
    """``table -> privileges`` for every base table plus ``dolt_ignore``."""
    plan: dict[str, frozenset[str]] = {}
    for table in [*tables, IGNORE_TABLE]:
        if not _TABLE.fullmatch(table):
            raise ForwardError(f"refusing to grant on odd table name {table!r}")
        plan[table] = table_shape(table)
    return plan


def _parse_grant(line: str):
    from .hq_sql_placement import parse_grant  # lazy: one SHOW GRANTS parser for HQ and hives

    return parse_grant(line)


def check_forwarder_grants(lines: Iterable[str], *, database: str) -> list[str]:
    """Violations of the forwarder shape in one account's ``SHOW GRANTS`` lines."""
    problems = []
    for line in lines:
        grant = _parse_grant(line)
        if grant is None:
            problems.append(f"unrecognised grant: {line.strip()[:120]}")
            continue
        scope = f"{grant.database}.{grant.table}"
        if grant.grant_option:
            problems.append(f"grant option on {scope}")
        privileges = grant.privileges
        if privileges <= _USAGE:
            continue
        if privileges & {"ALL", "ALL PRIVILEGES"}:
            problems.append(f"holds ALL on {scope} (never)")
            continue
        if grant.database == "*":
            problems.append(f"holds {sorted(privileges)} server-wide on {scope}")
            continue
        if grant.database != database:
            problems.append(f"holds {sorted(privileges)} on another database ({scope})")
            continue
        if grant.table == "*":
            problems.append(
                f"holds {sorted(privileges)} database-wide on {scope} (grants are per table)"
            )
            continue
        excess = privileges - table_shape(grant.table)
        if excess:
            problems.append(
                f"holds {sorted(excess)} on {scope} beyond {sorted(table_shape(grant.table))}"
            )
    return problems


def _grants(sql: Sql, account: Account) -> list[str]:
    return [str(_first(r)) for r in sql.query(f"SHOW GRANTS FOR {account.sql}")]


def _accounts(sql: Sql) -> list[tuple[Account, str]]:
    rows = sql.query("SELECT user, host, ssl_type FROM mysql.user")
    return [
        (Account(str(_ci(r, "user")), str(_ci(r, "host"))), str(_ci(r, "ssl_type") or ""))
        for r in rows
    ]


def conformance(
    sql: Sql,
    *,
    database: str,
    operators: Iterable[str] = DEFAULT_OPERATORS,
    require_tls: bool = True,
    only: Iterable[Account] | None = None,
) -> list[str]:
    """Every finding against the forwarder shape on one hive server (empty = conformant).

    Every account that is not an operator or Dolt system login is a forwarder; ``only`` narrows
    the check to the given accounts (provisioning's own verify). Needs a login that can read
    ``mysql.user`` and ``SHOW GRANTS`` for others (the primary's bd login)."""
    ops = _operators(operators)
    wanted = None if only is None else set(only)
    accounts = [(a, ssl) for a, ssl in _accounts(sql) if a.user not in ops]
    hosts: dict[str, set[str]] = {}
    for account, _ssl in accounts:
        hosts.setdefault(account.user, set()).add(account.host)
    problems = []
    for account, ssl_type in sorted(accounts):
        if wanted is not None and account not in wanted:
            continue
        found = account_problems(account, ops)
        if len(hosts[account.user]) > 1:
            found.append(
                f"principal {account.user!r} is shared by {len(hosts[account.user])} hosts "
                f"({', '.join(sorted(hosts[account.user]))})"
            )
        if require_tls and not ssl_type:
            found.append("does not REQUIRE SSL")
        found += check_forwarder_grants(_grants(sql, account), database=database)
        problems += [f"{account}: {p}" for p in found]
    return problems


@dataclass(frozen=True)
class ProvisionReport:
    account: Account
    created: bool
    tables: tuple[str, ...]
    granted: tuple[str, ...]
    revoked: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "account": str(self.account),
            "created": self.created,
            "tables": list(self.tables),
            "granted": list(self.granted),
            "revoked": list(self.revoked),
        }


def _base_tables(sql: Sql) -> list[str]:
    rows = sql.query("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
    return sorted(str(_first(r)) for r in rows)


def _held(sql: Sql, account: Account) -> list:
    return [g for g in (_parse_grant(line) for line in _grants(sql, account)) if g is not None]


def provision(
    sql: Sql,
    *,
    database: str,
    account: Account,
    password: str | None = None,
    operators: Iterable[str] = DEFAULT_OPERATORS,
    require_tls: bool = True,
) -> ProvisionReport:
    """Create or regrant one forwarder account in the table-scoped shape, then verify it.

    Idempotent and gap-free: it revokes only what exceeds the shape (including any
    database-wide or server-wide grant) and grants only what is missing, from the current
    ``SHOW FULL TABLES``. A new account needs ``password``; an existing one keeps its password
    unless one is given. Raises :class:`ForwardRefused` for a shared, root, operator or
    unpinned account and :class:`GrantShapeViolation` if the result is still not conformant."""
    if not _TABLE.fullmatch(database):
        raise ForwardError(f"not a plain database name: {database!r}")
    problems = account_problems(account, operators)
    if problems:
        raise ForwardRefused(f"{account}: " + "; ".join(problems))
    existing = [a for a, _ in _accounts(sql) if a.user == account.user]
    others = sorted(a.host for a in existing if a.host != account.host)
    if others:
        raise ForwardRefused(
            f"{account}: principal {account.user!r} already exists for {', '.join(others)} — a "
            "forwarder login is per frame, never shared; pick another principal"
        )
    created = account not in existing
    statements: list[str] = []
    tls = " REQUIRE SSL" if require_tls else ""
    if created:
        if not password:
            raise ForwardError(f"{account}: a new forwarder account needs a password")
        statements.append(
            f"CREATE USER {account.sql} IDENTIFIED WITH mysql_native_password AS "
            f"{quote(native_password_hash(password))}{tls}"
        )
    else:
        if password:
            statements.append(
                f"ALTER USER {account.sql} IDENTIFIED WITH mysql_native_password AS "
                f"{quote(native_password_hash(password))}"
            )
        if require_tls:
            statements.append(f"ALTER USER {account.sql} REQUIRE SSL")
    if statements:
        sql.execute(statements)

    tables = _base_tables(sql)
    plan = grant_plan(tables)
    revoked, granted = [], []
    held: dict[str, set[str]] = {}
    for grant in _held(sql, account):
        privileges = set(grant.privileges) - {"USAGE"}
        if not privileges and not grant.grant_option:
            continue
        if grant.database == "*":
            target = "*.*"
        elif grant.table == "*":
            target = f"`{grant.database}`.*"
        else:
            target = f"`{grant.database}`.`{grant.table}`"
        in_plan = grant.database == database and grant.table != "*" and grant.table in plan
        if privileges & {"ALL", "ALL PRIVILEGES"}:
            keep, drop = set(), {"ALL PRIVILEGES"}
        elif in_plan:
            keep, drop = privileges & plan[grant.table], privileges - plan[grant.table]
        else:
            keep, drop = set(), privileges
        if in_plan:
            held[grant.table] = held.get(grant.table, set()) | keep
        if grant.grant_option:
            revoked.append(f"REVOKE GRANT OPTION ON {target} FROM {account.sql}")
        if drop:
            revoked.append(f"REVOKE {', '.join(sorted(drop))} ON {target} FROM {account.sql}")
    if revoked:
        sql.execute(revoked)
    for table, privileges in plan.items():
        missing = set(privileges) - held.get(table, set())
        if missing:
            granted.append(
                f"GRANT {', '.join(sorted(missing))} ON `{database}`.`{table}` TO {account.sql}"
            )
    if granted:
        sql.execute(granted)
    leftover = conformance(
        sql, database=database, operators=operators, require_tls=require_tls, only=[account]
    )
    if leftover:
        raise GrantShapeViolation(
            f"{account}: grants still exceed the forwarder shape after provisioning", leftover
        )
    return ProvisionReport(
        account=account,
        created=created,
        tables=tuple(plan),
        granted=tuple(granted),
        revoked=tuple(revoked),
    )


def revoke(sql: Sql, *, account: Account, operators: Iterable[str] = DEFAULT_OPERATORS) -> bool:
    """Drop one forwarder account (its sessions first). ``False`` when it did not exist."""
    if account.user in _operators(operators):
        raise ForwardRefused(f"{account}: refusing to drop an operator or system login")
    if account not in {a for a, _ in _accounts(sql)}:
        return False
    quiesce(sql, operators=operators, only=[account])
    sql.execute([f"DROP USER {account.sql}"])
    return True


# =============================================================================================
# Sessions (M13 E5: quiesce forwarders before a divert reset)
# =============================================================================================


@dataclass(frozen=True)
class Session:
    id: int
    user: str
    host: str

    def __str__(self) -> str:
        return f"{self.id} '{self.user}'@'{self.host}'"


def forwarder_sessions(
    sql: Sql,
    *,
    operators: Iterable[str] = DEFAULT_OPERATORS,
    only: Iterable[Account] | None = None,
) -> list[Session]:
    """Live sessions on the server held by any non-operator login (or by ``only``'s users)."""
    ops = _operators(operators)
    users = None if only is None else {a.user for a in only}
    out = []
    for row in sql.query("SELECT id, user, host FROM information_schema.processlist"):
        user = str(_ci(row, "user") or "")
        if not user or user in ops or (users is not None and user not in users):
            continue
        out.append(Session(int(_ci(row, "id")), user, str(_ci(row, "host") or "")))
    return out


def quiesce(
    sql: Sql,
    *,
    operators: Iterable[str] = DEFAULT_OPERATORS,
    only: Iterable[Account] | None = None,
) -> list[Session]:
    """``KILL`` every forwarder session; an in-flight transaction is rolled back and its
    ``COMMIT`` fails with a lost connection. A session that is already gone is not an error."""
    killed = []
    for session in forwarder_sessions(sql, operators=operators, only=only):
        with contextlib.suppress(Exception):
            sql.execute([f"KILL {int(session.id)}"])
        killed.append(session)
    return killed


def serve_settings(cfg: Mapping | None = None) -> dict:
    """``host.forward.serve`` validated (defaults when unset). ``cfg`` is a loaded bh config;
    ``None`` loads it."""
    from .modules.config.contracts import HostForwardServeConfig

    if cfg is None:
        from . import config  # lazy: only the reset path and doctor need the loaded config

        cfg = config.load()
    raw = ((cfg or {}).get("host") or {}).get("forward") or {}
    return HostForwardServeConfig.model_validate(dict(raw.get("serve") or {})).model_dump()


def quiesce_before_reset(
    sql: Sql,
    *,
    login: str = "",
    settings: Mapping | None = None,
    logger=None,
) -> list[Session]:
    """Kill every forwarder session before ``DOLT_RESET --hard`` on a server-mode hive.

    On by default; ``host.forward.serve.quiesce_before_reset: false`` or ``BH_FORWARD_QUIESCE=off``
    turns it off. ``login`` (bd's own login on the server) is never a forwarder. Never raises:
    a quiesce that cannot run must not block the reset, which is the fence's own safety step;
    the failure is logged and the E5 window stays as measured."""
    if os.environ.get(QUIESCE_ENV, "").strip().lower() in {"0", "off", "false", "no"}:
        return []
    try:
        opts = dict(settings) if settings is not None else serve_settings()
        if not opts.get("quiesce_before_reset", True):
            return []
        operators = [*opts.get("operators", DEFAULT_OPERATORS), *([login] if login else [])]
        killed = quiesce(sql, operators=operators)
    except Exception as exc:  # noqa: BLE001 — never block the fence's reset on the quiesce
        if logger is not None:
            logger.warning("forward_quiesce_failed", error=str(exc)[:300])
        return []
    if killed and logger is not None:
        logger.info(
            "forward_quiesce", killed=[str(s) for s in killed], reason="divert reset follows"
        )
    return killed


# =============================================================================================
# Globals and the read-only root (what bh doctor reports on a forwarding primary)
# =============================================================================================


@dataclass(frozen=True)
class GlobalResult:
    name: str
    expected: str
    actual: str | None
    status: str  # ok | drift | unverifiable
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual,
            "status": self.status,
            "detail": self.detail,
        }


_TRUE = {"1", "on", "true", "yes"}
_FALSE = {"0", "off", "false", "no"}


def _normalize(value: object) -> str:
    text = str(value).strip().lower()
    return "1" if text in _TRUE else "0" if text in _FALSE else text


def watched_globals(
    expect: Mapping[str, str] | None = None, unwatch: Iterable[str] = ()
) -> dict[str, str]:
    """:data:`WATCHED_GLOBALS` plus overrides, minus ``unwatch`` (``host.forward.serve``)."""
    out = dict(WATCHED_GLOBALS)
    out.update({str(k): str(v) for k, v in (expect or {}).items()})
    for name in unwatch:
        out.pop(name, None)
    return out


def check_globals(sql: Sql, watched: Mapping[str, str] | None = None) -> list[GlobalResult]:
    """Read each watched ``@@GLOBAL`` and compare. Read-only: it never sets a value."""
    results = []
    for name, expected in (watched if watched is not None else WATCHED_GLOBALS).items():
        if not name.replace("_", "").isalnum():
            results.append(GlobalResult(name, expected, None, "unverifiable", "odd name"))
            continue
        try:
            rows = sql.query(f"SELECT @@GLOBAL.{name} AS v")
            actual = str(_first(rows[0])) if rows else None
        except Exception as exc:  # noqa: BLE001 — an unreadable global is a finding, not a crash
            results.append(GlobalResult(name, expected, None, "unverifiable", str(exc)[:200]))
            continue
        if actual is None:
            results.append(GlobalResult(name, expected, None, "unverifiable", "no value"))
            continue
        status = "ok" if _normalize(actual) == _normalize(expected) else "drift"
        results.append(GlobalResult(name, expected, actual, status))
    return results


def root_problems(root: Path | str) -> list[str]:
    """The read-only ``DOLT_ROOT_PATH`` check, for the user running it (as the watchdog's
    ``root-check``): ``.dolt`` and ``config_global.json`` must exist and be unwritable."""
    dot = Path(root) / ".dolt"
    cfg = dot / "config_global.json"
    if not dot.is_dir():
        return [f"{dot} does not exist"]
    problems = []
    if os.access(dot, os.W_OK):
        problems.append(f"{dot} is writable (SET PERSIST can create its temp file)")
    if not cfg.exists():
        problems.append(f"{cfg} is absent (provision it read-only before first start)")
    elif os.access(cfg, os.W_OK):
        problems.append(f"{cfg} is writable")
    return problems


def primary_report(sql: Sql, *, database: str, settings: Mapping | None = None) -> dict:
    """What ``bh doctor`` and ``bh hive forward check`` report on a forwarding primary:
    ``{database, globals, root, conformance, findings}``. Never raises."""
    opts = dict(settings) if settings is not None else serve_settings()
    findings: list[str] = []
    watched = watched_globals(opts.get("watched"), opts.get("unwatched") or ())
    globals_ = check_globals(sql, watched)
    for g in globals_:
        if g.status != "ok":
            shown = "<unreadable>" if g.actual is None else g.actual
            findings.append(
                f"global {g.name} {g.status}: expected {g.expected}, actual {shown}"
                + (f" ({g.detail})" if g.detail else "")
            )
    root_path = str(opts.get("root_path") or "")
    root = {"path": root_path or None, "problems": []}
    if root_path:
        root["problems"] = root_problems(root_path)
        findings += [f"DOLT_ROOT_PATH not read-only: {p}" for p in root["problems"]]
    else:
        findings.append(
            "DOLT_ROOT_PATH not checked: set host.forward.serve.root_path to the server's root"
        )
    try:
        conf = conformance(
            sql,
            database=database,
            operators=opts.get("operators", DEFAULT_OPERATORS),
            require_tls=bool(opts.get("require_tls", True)),
        )
    except Exception as exc:  # noqa: BLE001 — doctor degrades, it never crashes
        conf = [f"grant conformance unreadable: {str(exc)[:200]}"]
    findings += [f"forwarder grants: {p}" for p in conf]
    return {
        "database": database,
        "globals": [g.as_dict() for g in globals_],
        "root": root,
        "conformance": conf,
        "findings": findings,
    }


# =============================================================================================
# The forwarder's side: target, preflight, pointing bd
# =============================================================================================


@dataclass(frozen=True)
class ForwardTarget:
    """Where a forwarder's bd should write: the placed primary and its hive server."""

    frame: str
    epoch: int
    endpoint: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"frame": self.frame, "epoch": self.epoch, "endpoint": dict(self.endpoint)}


def resolve_target(
    placement: tuple[str, int] | None,
    endpoints: Mapping[str, Mapping[str, Any]],
    *,
    self_frame: str,
) -> ForwardTarget | None:
    """The forward target for the placed primary. ``None`` when this frame IS the primary (it
    writes ``main`` directly). Raises :class:`ForwardRefused` with no placement or no endpoint."""
    if placement is None:
        raise ForwardRefused("no placement is cached for this hive: nobody to forward to")
    frame, epoch = str(placement[0]), int(placement[1])
    if frame == self_frame:
        return None
    endpoint = endpoints.get(frame)
    if not endpoint or not endpoint.get("host"):
        raise ForwardRefused(
            f"placement names {frame}@{epoch} but host.forward.endpoints has no hive server for "
            f"{frame!r}"
        )
    return ForwardTarget(frame=frame, epoch=epoch, endpoint=dict(endpoint))


def preflight(sql: Sql, target: ForwardTarget) -> tuple[str, int]:
    """Refuse unless the target server's own identity and ``bh_writer`` name the placed frame
    at the placed epoch. Reads only (the forwarder's grants allow exactly these reads).
    Returns ``(frame, epoch)``."""
    try:
        ident = sql.query(f"SELECT frame FROM {LOCAL_IDENT_TABLE} WHERE id = 1")
        writer = sql.query("SELECT frame, epoch FROM bh_writer WHERE id = 1")
    except Exception as exc:  # noqa: BLE001 — unreadable means not verifiably the writer
        raise ForwardRefused(
            f"cannot verify {target.frame}'s hive server is the writer: {str(exc)[:200]}"
        ) from exc
    if not writer:
        raise ForwardRefused(f"{target.frame}'s hive data carries no bh_writer: not cut over")
    w_frame, w_epoch = str(_ci(writer[0], "frame")), int(_ci(writer[0], "epoch"))
    i_frame = str(_ci(ident[0], "frame")) if ident else ""
    if i_frame != target.frame:
        raise ForwardRefused(
            f"the server at {target.endpoint.get('host')}:{target.endpoint.get('port')} "
            f"identifies as {i_frame or '<no bh_local_ident>'}, not the placed primary "
            f"{target.frame}"
        )
    if w_frame != target.frame:
        raise ForwardRefused(
            f"{target.frame} is not the writer (bh_writer names {w_frame}@{w_epoch}): a demoted "
            "primary — refusing to forward"
        )
    if w_epoch < target.epoch:
        raise ForwardRefused(
            f"{target.frame}'s data is at epoch {w_epoch}, placement at {target.epoch}: adopt "
            "incomplete — refusing to forward until it lands"
        )
    if w_epoch > target.epoch:
        raise ForwardRefused(
            f"{target.frame}'s data is at epoch {w_epoch}, ahead of the cached placement "
            f"{target.epoch}: refresh HQ and re-point"
        )
    return w_frame, w_epoch


@dataclass(frozen=True)
class Marker:
    """A forwarded checkout's state, kept git-private (:func:`marker_path`), never in a
    tracked file. Holds no secret: the credential is a fnox reference."""

    prefix: str
    self_frame: str
    state: str  # forwarding | refused
    frame: str = ""
    epoch: int = 0
    endpoint: Mapping[str, Any] = field(default_factory=dict)
    endpoints: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    hq_dir: str = ""
    reason: str = ""
    at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "version": MARKER_VERSION,
            "prefix": self.prefix,
            "self_frame": self.self_frame,
            "state": self.state,
            "frame": self.frame,
            "epoch": self.epoch,
            "endpoint": dict(self.endpoint),
            "endpoints": {k: dict(v) for k, v in self.endpoints.items()},
            "hq_dir": self.hq_dir,
            "reason": self.reason,
            "at": self.at,
        }


def _git_common_dir(start: Path) -> Path | None:
    """The git common dir of the checkout holding ``start`` (a worktree resolves to its main
    repository's), found from the filesystem alone."""
    for directory in (start, *start.parents):
        dot = directory / ".git"
        if dot.is_dir():
            return dot
        if dot.is_file():
            text = dot.read_text(errors="replace").strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text.removeprefix("gitdir:").strip())
            if not gitdir.is_absolute():
                gitdir = (directory / gitdir).resolve()
            common = gitdir / "commondir"
            if common.is_file():
                target = Path(common.read_text().strip())
                return target if target.is_absolute() else (gitdir / target).resolve()
            return gitdir
    return None


def marker_path(hive_dir: Path | str) -> Path:
    """``<git common dir>/bh/forward.json``: one marker for a hive checkout and all of its
    worktrees, never tracked. A directory outside git keeps it in ``.beads/`` instead."""
    common = _git_common_dir(Path(hive_dir).resolve())
    if common is None:
        return Path(hive_dir) / ".beads" / MARKER
    return common / "bh" / "forward.json"


def read_marker(hive_dir: Path | str) -> Marker | None:
    try:
        raw = json.loads(marker_path(hive_dir).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != MARKER_VERSION:
        return None
    return Marker(
        prefix=str(raw.get("prefix", "")),
        self_frame=str(raw.get("self_frame", "")),
        state=str(raw.get("state", "")),
        frame=str(raw.get("frame", "")),
        epoch=int(raw.get("epoch") or 0),
        endpoint=dict(raw.get("endpoint") or {}),
        endpoints={str(k): dict(v) for k, v in (raw.get("endpoints") or {}).items()},
        hq_dir=str(raw.get("hq_dir", "")),
        reason=str(raw.get("reason", "")),
        at=float(raw.get("at") or 0.0),
    )


def _write_marker(hive_dir: Path | str, marker: Marker) -> Marker:
    path = marker_path(hive_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(marker.as_dict(), indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return marker


def point(
    hive_dir: Path | str,
    target: ForwardTarget,
    *,
    prefix: str,
    self_frame: str,
    endpoints: Mapping[str, Mapping[str, Any]],
    hq_dir: str = "",
    now: Callable[[], float] = time.time,
) -> Marker:
    """Record that bh's bd for this checkout writes to ``target``. Call :func:`preflight`
    first; this only writes the marker. Nothing tracked changes: :func:`bd_env` carries the
    target to bd as environment."""
    return _write_marker(
        hive_dir,
        Marker(
            prefix=prefix,
            self_frame=self_frame,
            state="forwarding",
            frame=target.frame,
            epoch=target.epoch,
            endpoint=dict(target.endpoint),
            endpoints=endpoints,
            hq_dir=hq_dir,
            at=now(),
        ),
    )


def refuse(
    hive_dir: Path | str,
    reason: str,
    *,
    prefix: str,
    self_frame: str,
    endpoints: Mapping[str, Mapping[str, Any]],
    hq_dir: str = "",
    placement: tuple[str, int] | None = None,
    now: Callable[[], float] = time.time,
) -> Marker:
    """Fail closed: record the refusal. bh then refuses to run bd for the checkout
    (:func:`bd_env` raises) until a re-point passes :func:`preflight`. A raw ``bd`` on this
    frame writes its own replica, whose write guard refuses (it is not ``bh_writer``)."""
    return _write_marker(
        hive_dir,
        Marker(
            prefix=prefix,
            self_frame=self_frame,
            state="refused",
            frame=placement[0] if placement else "",
            epoch=int(placement[1]) if placement else 0,
            endpoints=endpoints,
            hq_dir=hq_dir,
            reason=reason,
            at=now(),
        ),
    )


def stop(hive_dir: Path | str) -> bool:
    """Stop forwarding the checkout. ``False`` when it was not forwarded."""
    path = marker_path(hive_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


# ---- credentials ---------------------------------------------------------------------------

_password_cache: dict[str, str] = {}


class EnvBroker:
    """A :class:`beadhive.hq_sql_transport.SecretBroker` reading :data:`PASSWORD_ENV`."""

    def get(self, reference: dict, *, deadline: float) -> str:
        value = os.environ.get(PASSWORD_ENV, "")
        if not value:
            raise ForwardError(f"no forwarder password: set a fnox credential or {PASSWORD_ENV}")
        return value


def _broker():
    if os.environ.get(PASSWORD_ENV):
        return EnvBroker()
    from .hq_sql_transport import FnoxBroker

    return FnoxBroker()


def password_for(endpoint: Mapping[str, Any], *, timeout: float = 15.0) -> str:
    """The forwarder's password for ``endpoint``: :data:`PASSWORD_ENV` when set, else its fnox
    reference. Cached per process by reference (never written anywhere)."""
    if os.environ.get(PASSWORD_ENV):
        return os.environ[PASSWORD_ENV]
    ref = dict(endpoint.get("credential") or {})
    key = json.dumps(ref, sort_keys=True)
    if key not in _password_cache:
        _password_cache[key] = _broker().get(ref, deadline=time.monotonic() + timeout)
    return _password_cache[key]


def connect(endpoint: Mapping[str, Any], *, timeout: float = 15.0):
    """A verified-TLS DB-API connection to ``endpoint`` with the forwarder's own login
    (:func:`beadhive.hq_sql_transport.connect`: never retries, never downgrades)."""
    from .hq_sql_transport import connect as sql_connect

    broker = EnvBroker() if os.environ.get(PASSWORD_ENV) else _broker()
    conn = sql_connect(dict(endpoint), broker, deadline=time.monotonic() + timeout)
    with contextlib.suppress(Exception):
        conn.autocommit(True)
    return conn


def bd_env(hive_dir: Path | str, base: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The environment for a bd that bh runs in a forwarded checkout, or ``None`` when the
    checkout is not forwarded. Raises :class:`ForwardRefused` while forwarding is refused.

    bd's own environment overrides its persisted ``metadata.json`` (which is tracked, so it is
    never edited): server mode on the target, TLS, the forwarder's principal and password, and
    no shared or auto-started local server. ``SSL_CERT_FILE`` pins the endpoint's CA."""
    marker = read_marker(hive_dir)
    if marker is None:
        return None
    if marker.state != "forwarding":
        raise ForwardRefused(
            f"{marker.prefix}: forwarding refused ({marker.reason or 'no reason recorded'}) — "
            f"re-point with `bh hive forward point {marker.prefix}`"
        )
    ep = marker.endpoint
    env = dict(os.environ if base is None else base)
    tls = ep.get("tls_mode", "required") == "required"
    env.update(
        {
            "BEADS_DOLT_SERVER_MODE": "1",
            "BEADS_DOLT_SHARED_SERVER": "0",
            "BEADS_DOLT_AUTO_START": "0",
            "BEADS_DOLT_SERVER_HOST": str(ep["host"]),
            "BEADS_DOLT_SERVER_PORT": str(int(ep.get("port") or 3307)),
            "BEADS_DOLT_SERVER_USER": str(ep.get("user") or ""),
            "BEADS_DOLT_SERVER_TLS": "1" if tls else "0",
            "BEADS_DOLT_PASSWORD": password_for(ep),
        }
    )
    if ep.get("database"):
        env["BEADS_DOLT_SERVER_DATABASE"] = str(ep["database"])
    if tls and ep.get("ca_file"):
        env["SSL_CERT_FILE"] = str(ep["ca_file"])
    return env


def _cached_placement(marker: Marker) -> tuple[str, int] | None:
    if not marker.hq_dir:
        return None
    from . import host_lease  # lazy: the HQ clone's cached lease (the guard's own read)

    lease = host_lease.read_cached(marker.prefix, cwd=Path(marker.hq_dir))
    return None if lease is None else (str(lease.host_id), int(lease.epoch))


def ensure_current(
    hive_dir: Path | str,
    *,
    placement: Callable[[Marker], tuple[str, int] | None] = _cached_placement,
    open_sql: Callable[[Mapping[str, Any]], Sql] | None = None,
) -> Marker | None:
    """Re-point a forwarded checkout when the cached placement moved since it was pointed.

    ``None`` when the checkout is not forwarded. A refused checkout is re-tried on every call
    (it fails closed until a placement names a writer that passes :func:`preflight`). When the
    placement names this frame, forwarding stops (this frame is the primary now)."""
    marker = read_marker(hive_dir)
    if marker is None:
        return None
    try:
        placed = placement(marker)
    except Exception:  # noqa: BLE001 — an unreadable cache keeps the last decision
        return marker
    if marker.state == "forwarding" and placed == (marker.frame, marker.epoch):
        return marker
    return repoint(
        hive_dir,
        placed,
        prefix=marker.prefix,
        self_frame=marker.self_frame,
        endpoints=marker.endpoints,
        hq_dir=marker.hq_dir,
        open_sql=open_sql,
    )


def repoint(
    hive_dir: Path | str,
    placed: tuple[str, int] | None,
    *,
    prefix: str,
    self_frame: str,
    endpoints: Mapping[str, Mapping[str, Any]],
    hq_dir: str = "",
    open_sql: Callable[[Mapping[str, Any]], Sql] | None = None,
) -> Marker | None:
    """Resolve, preflight and point — or refuse (fail closed). ``None`` (and forwarding
    stopped) when ``placed`` names this frame."""
    try:
        target = resolve_target(placed, endpoints, self_frame=self_frame)
        if target is None:
            stop(hive_dir)
            return None
        opener = open_sql or (lambda ep: DbapiSql(connect(ep)))
        sql = opener(target.endpoint)
        try:
            preflight(sql, target)
        finally:
            close = getattr(getattr(sql, "connection", None), "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    close()
    except ForwardRefused as exc:
        return refuse(
            hive_dir,
            str(exc),
            prefix=prefix,
            self_frame=self_frame,
            endpoints=endpoints,
            hq_dir=hq_dir,
            placement=placed,
        )
    except Exception as exc:  # noqa: BLE001 — cannot reach or verify the target: fail closed
        return refuse(
            hive_dir,
            f"cannot reach the placed primary's hive server: {str(exc)[:200]}",
            prefix=prefix,
            self_frame=self_frame,
            endpoints=endpoints,
            hq_dir=hq_dir,
            placement=placed,
        )
    return point(
        hive_dir,
        target,
        prefix=prefix,
        self_frame=self_frame,
        endpoints=endpoints,
        hq_dir=hq_dir,
    )
