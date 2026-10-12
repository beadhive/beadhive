"""Operator provisioning and the condition-15 check for session/evidence rows (bh-owqdg).

Server-local operator work, never run in a frame: :func:`provision_liveness_schema` once per HQ
runtime database (the committed ``frame_*`` ignore rule and liveness policy), then
:func:`provision_incarnation` per frame incarnation (tables, stamping triggers, the host-pinned
``REQUIRE SSL`` account and its grants), each followed by :func:`check_provisioning`, which
refuses a non-pinned or TLS-optional account and any hive database co-hosted on the HQ server
(ADR §5, conditions 6 and 15). The DDL itself lives in :mod:`beadhive.hq_sql_session`.

Separate from :mod:`beadhive.hq_sql_session` so the frame-side reader never imports the
placement conformance checks (no import cycle through ``hq_sql_runtime``). Every statement is the
HQ storage port's Dolt adapter's (``HqRuntimeStatements`` on the operator's own cursor,
bh-xnn5p; its final ``DoltAccounts`` shape is bh-3kl1k's); this module holds no SQL.
"""

from __future__ import annotations

import re

from .hq_sql_placement import check_grants, check_triggers
from .hq_sql_runtime_schema import (
    LIVENESS_POLICY_TABLE,
    evidence_table,
    inbox_table,
    session_table,
)
from .hq_sql_session import (
    _STAMP_COLUMN,
    _SYSTEM_SCHEMAS,
    HIVE_DATABASE_MARKERS,
    IGNORE_PATTERN,
    LivenessPolicy,
    SessionError,
    _existing,
    _policy,
    _statements,
    account_statements,
    evidence_ddl,
    liveness_schema_statements,
    pinned_address,
    session_ddl,
)

__all__ = [
    "check_account",
    "check_provisioning",
    "cohosted_hive_databases",
    "provision_incarnation",
    "provision_liveness_schema",
    "resume_incarnation",
    "validate_incarnation",
]


def _ignore_committed(cursor) -> bool:
    sql = _statements(cursor)
    rows = sql.ignore_rules(IGNORE_PATTERN)
    if not rows or not all(bool(row[1]) for row in rows):
        return False
    return not sql.ignore_dirty()


def provision_liveness_schema(cursor, policy: LivenessPolicy | None = None) -> bool:
    """Idempotent: run :func:`liveness_schema_statements` unless already committed. Returns
    whether anything was written."""
    if _ignore_committed(cursor) and _existing(cursor, (LIVENESS_POLICY_TABLE,)):
        return False
    sql = _statements(cursor)
    for statement in liveness_schema_statements(policy):
        sql.execute(statement)
    return True


def provision_incarnation(
    cursor,
    principal: str,
    epoch: int,
    *,
    frame_address: str,
    password: str,
    database: str,
    inbox: bool = False,
) -> list[str]:
    """Fresh-only operator provisioning of one incarnation's tables, triggers and account.

    Refuses unless the ignore rule is committed first (bh-v0k3i), and refuses when either table
    already exists. Commits ``dolt_schemas`` (the trigger definitions) so the runtime working
    tree stays clean for the next authority publication. Then runs :func:`check_provisioning`
    and raises on any violation; returns the (empty) problem list on success."""
    tables = (session_table(principal, epoch), evidence_table(principal, epoch))
    if not _ignore_committed(cursor) or not _existing(cursor, (LIVENESS_POLICY_TABLE,)):
        raise SessionError(
            f"the {IGNORE_PATTERN!r} ignore rule and {LIVENESS_POLICY_TABLE} must be committed "
            "before any incarnation table exists (provision_liveness_schema)"
        )
    if _existing(cursor, tables):
        raise SessionError("incarnation session/evidence tables already exist (fresh-only)")
    if not isinstance(password, str) or not password:
        raise SessionError("frame account password required")
    sql = _statements(cursor)
    for statement in (*session_ddl(principal, epoch), *evidence_ddl(principal, epoch)):
        sql.execute(statement)
    for statement in account_statements(
        principal, epoch, frame_address=frame_address, database=database, inbox=inbox
    ):
        sql.execute(statement, (password,) if "%s" in statement else None)
    sql.commit_schemas(f"HQ session/evidence triggers for {principal} incarnation {epoch}")
    problems = check_provisioning(cursor, principal, epoch, database=database, inbox=inbox)
    if problems:
        raise SessionError("provisioning check failed: " + "; ".join(problems))
    return problems


def resume_incarnation(
    cursor, principal: str, epoch: int, *, frame_address: str, password: str, database: str
) -> list[str]:
    """Converge the reviewed session-only incarnation without rewriting existing evidence.

    A retry may complete a missing table, seed, trigger or account. Existing objects
    must match the supported schema and exact timestamp triggers; account passwords
    and evidence already published by a frame are never reset.
    """
    statements = account_statements(
        principal, epoch, frame_address=frame_address, database=database
    )  # Validate identifiers/address before the first write.
    if not isinstance(password, str) or not password:
        raise SessionError("frame account password required")
    if not _ignore_committed(cursor) or not _existing(cursor, (LIVENESS_POLICY_TABLE,)):
        raise SessionError("committed frame_* ignore rule and liveness policy required")
    if _existing(cursor, (inbox_table(principal, epoch),)):
        raise SessionError("reviewed session-only incarnation already has an inbox")
    definitions = (session_ddl(principal, epoch), evidence_ddl(principal, epoch))
    expected_triggers = {f"{ddl[0].split()[2]}_stamp": ddl[1] for ddl in definitions}
    sql = _statements(cursor)
    _pending_schema(sql, expected_triggers)
    accounts = sql.accounts(principal)
    if accounts and (
        len(accounts) != 1
        or accounts[0][0] != frame_address
        or check_account(accounts[0][0], accounts[0][1], secure_transport=False)
    ):
        raise SessionError("existing incarnation account differs from pinned TLS account")
    for ddl in definitions:
        table = ddl[0].split()[2]
        if not _existing(sql, (table,)):
            sql.execute(ddl[0])
        _check_resume_table(sql, table)
        rows = sql.table_rows(table, "id,epoch")
        if not rows:
            sql.execute(ddl[2])
        elif [tuple(row) for row in rows] != [(1, epoch)]:
            raise SessionError("existing incarnation row differs from reviewed epoch")
        existing = sql.trigger_fragment(f"{table}_stamp")
        if existing:
            if len(existing) != 1 or existing[0][0] != ddl[1]:
                raise SessionError("existing incarnation trigger differs from supported stamp")
        else:
            sql.execute(ddl[1])
    if not accounts:
        sql.execute(statements[0], (password,))
    for statement in statements[1:]:
        sql.execute(statement)
    problems = check_provisioning(sql, principal, epoch, database=database)
    if problems:
        raise SessionError("provisioning check failed: " + "; ".join(problems))
    if _pending_schema(sql, expected_triggers, complete=True):
        sql.commit_schemas(f"HQ session/evidence triggers for {principal} incarnation {epoch}")
    return problems


def validate_incarnation(cursor, principal: str, epoch: int, *, database: str) -> None:
    """Refuse drift in a supported session route without repairing any object."""
    sql = _statements(cursor)
    for ddl in (session_ddl(principal, epoch), evidence_ddl(principal, epoch)):
        table = ddl[0].split()[2]
        _check_resume_table(sql, table)
        rows = sql.table_rows(table, "id,epoch")
        if [tuple(row) for row in rows] != [(1, epoch)]:
            raise SessionError("existing incarnation row differs from reviewed epoch")
        triggers = sql.trigger_fragment(f"{table}_stamp")
        if [tuple(row) for row in triggers] != [(ddl[1],)]:
            raise SessionError("existing incarnation trigger differs from supported stamp")
    problems = check_provisioning(sql, principal, epoch, database=database)
    if problems:
        raise SessionError("provisioning check failed: " + "; ".join(problems))


def _pending_schema(cursor, expected_triggers, *, complete=False):
    sql = _statements(cursor)
    dirty = sql.status_tables()
    if not dirty:
        return False
    if {row[0] for row in dirty} != {"dolt_schemas"}:
        raise SessionError("unrelated committed working tree changes during provisioning")
    head = sql.head()
    before = {tuple(row[:2]): tuple(row[2:]) for row in sql.schemas(at=head)}
    current = {tuple(row[:2]): tuple(row[2:]) for row in sql.schemas()}
    added = {key: row for key, row in current.items() if key not in before}
    expected = {("trigger", name): fragment for name, fragment in expected_triggers.items()}
    if (
        any(current.get(key) != row for key, row in before.items())
        or not added
        or not set(added) <= set(expected)
        or any(row[0] != expected[key] for key, row in added.items())
        or complete
        and set(added) != set(expected)
    ):
        raise SessionError("unverified schema changes during provisioning")
    return True


def _check_resume_table(cursor, table: str) -> None:
    expected = [
        ("id", "tinyint", "NO", "PRI"),
        ("epoch", "bigint unsigned", "NO", ""),
    ]
    if table.endswith("_session"):
        expected.append(("renewed_at", "datetime(6)", "YES", ""))
    else:
        expected += [
            (name, kind, "NO", "")
            for name, kind in (
                ("release_id", "varchar(128)"),
                ("release_digest", "varchar(80)"),
                ("profile", "varchar(64)"),
                ("status", "varchar(32)"),
                ("report_digest", "varchar(80)"),
            )
        ]
        expected.append(("measured_at", "datetime(6)", "YES", ""))
    sql = _statements(cursor)
    rows = sql.describe(table)
    if [tuple(row[:4]) for row in rows] != expected or any(
        row[4] is not None or row[5] for row in rows
    ):
        raise SessionError("existing incarnation table differs from supported schema")
    checks = sql.check_clauses(table)
    if len(checks) != 1 or re.sub(r"[`()\s]", "", checks[0][0].lower()) != "id=1":
        raise SessionError("existing incarnation table lacks singleton constraint")
    indexes = sql.indexes(table)
    if len(indexes) != 1 or indexes[0][1:5] != (0, "PRIMARY", 1, "id"):
        raise SessionError("existing incarnation table has unsupported indexes")


# =============================================================================================
# Provisioning check (condition 15)
# =============================================================================================


def check_account(host: str, ssl_type: str, *, secure_transport: bool) -> list[str]:
    """Violations for one ``mysql.user`` row of a frame principal."""
    problems = []
    if not pinned_address(host):
        problems.append(f"account host {host!r} is not pinned to one frame address")
    if not (ssl_type or "").strip() and not secure_transport:
        problems.append(f"account @{host!r} does not require TLS (REQUIRE SSL)")
    return problems


def check_provisioning(
    cursor, principal: str, epoch: int, *, database: str, inbox: bool = False
) -> list[str]:
    """Every condition-15 / §5 violation for one incarnation, run with an operator account.

    Refuses: a missing or uncommitted ignore rule; a missing or invalid policy row; a missing
    table or a table with anything but its one ``id = 1`` row; a missing stamping trigger or any
    trigger on these tables that runs DML; any account for the principal that is not host-pinned
    or not TLS-required; frame write rights beyond its own tables
    (:func:`beadhive.hq_sql_placement.check_grants`); and any hive database on this server."""

    session, evidence = session_table(principal, epoch), evidence_table(principal, epoch)
    sql = _statements(cursor)
    problems: list[str] = []
    if not _ignore_committed(sql):
        problems.append(f"ignore rule {IGNORE_PATTERN!r} is not committed")
    present = _existing(sql, (session, evidence, LIVENESS_POLICY_TABLE))
    if LIVENESS_POLICY_TABLE not in present:
        problems.append(f"{LIVENESS_POLICY_TABLE} missing")
    else:
        try:
            _policy(sql.liveness_policy())
        except SessionError as exc:
            problems.append(str(exc))
    for table in (session, evidence):
        if table not in present:
            problems.append(f"{table} missing")
            continue
        rows = sql.table_rows(table, "id")
        if [tuple(row) for row in rows] != [(1,)]:
            problems.append(f"{table} must hold exactly its one id = 1 row")
    triggers = sql.triggers(database)
    for table, kind in ((session, "session"), (evidence, "evidence")):
        stamp = _stamp_body(kind)
        mine = [row for row in triggers if row[1] == table]
        if table in present and not any(
            str(row[3]).upper() == "BEFORE"
            and str(row[4]).upper() == "UPDATE"
            and " ".join(str(row[2]).split()) == stamp
            for row in mine
        ):
            problems.append(f"{table} lacks its BEFORE UPDATE {_STAMP_COLUMN[kind]} stamp")
        for row in mine:
            if " ".join(str(row[2]).split()) != stamp:
                problems.append(f"trigger {row[0]} on {table} does more than stamp server time")
    problems += check_triggers(
        [tuple(row[:3]) for row in triggers], frame_writable=(session, evidence)
    )
    secure = sql.require_secure_transport()
    secure_transport = bool(secure and secure[0][0] in (1, "1", "ON", "on"))
    accounts = sql.accounts(principal, spaced=True)
    if not accounts:
        problems.append(f"no account for principal {principal!r}")
    writable = (session, evidence, *((inbox_table(principal, epoch),) if inbox else ()))
    for host, ssl_type in accounts:
        problems += [
            f"{principal}: {p}"
            for p in check_account(host, ssl_type, secure_transport=secure_transport)
        ]
        lines = [row[0] for row in sql.grants_for(principal, host)]
        problems += [
            f"{principal}@{host}: {p}"
            for p in check_grants(lines, role="frame", database=database, writable=writable)
        ]
    problems += [
        f"hive database {name!r} is co-hosted on the HQ server"
        for name in cohosted_hive_databases(sql)
    ]
    return problems


def cohosted_hive_databases(cursor) -> list[str]:
    """Databases on this server that carry a hive marker table (bd's ``issues`` or the fence)."""
    rows = _statements(cursor).schemas_with_tables(HIVE_DATABASE_MARKERS)
    return sorted({row[0] for row in rows} - _SYSTEM_SCHEMAS)


def _stamp_body(kind: str) -> str:
    """The one trigger body condition 6 allows on an incarnation table: the stamp statement the
    adapter's stamping trigger runs (``stamp_trigger``), whitespace-normalized."""
    from beadhive_hq_storage_dolt import stamp_trigger

    return stamp_trigger("t", _STAMP_COLUMN[kind]).split(" FOR EACH ROW ", 1)[1]
