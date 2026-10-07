"""Operator provisioning and the condition-15 check for session/evidence rows (bh-owqdg).

Server-local operator work, never run in a frame: :func:`provision_liveness_schema` once per HQ
runtime database (the committed ``frame_*`` ignore rule and liveness policy), then
:func:`provision_incarnation` per frame incarnation (tables, stamping triggers, the host-pinned
``REQUIRE SSL`` account and its grants), each followed by :func:`check_provisioning`, which
refuses a non-pinned or TLS-optional account and any hive database co-hosted on the HQ server
(ADR §5, conditions 6 and 15). The DDL itself lives in :mod:`beadhive.hq_sql_session`.

Separate from :mod:`beadhive.hq_sql_session` so the frame-side reader never imports the
placement conformance checks (no import cycle through ``hq_sql_runtime``).
"""

from __future__ import annotations

from .hq_sql_placement import check_grants, check_triggers
from .hq_sql_runtime_schema import (
    LIVENESS_POLICY_TABLE,
    evidence_table,
    inbox_table,
    session_table,
)
from .hq_sql_session import (
    _POLICY_SQL,
    _STAMP_COLUMN,
    _SYSTEM_SCHEMAS,
    HIVE_DATABASE_MARKERS,
    IGNORE_PATTERN,
    LivenessPolicy,
    SessionError,
    _existing,
    _fetch,
    _policy,
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
]


def _ignore_committed(cursor) -> bool:
    rows = _fetch(
        cursor, "SELECT pattern, ignored FROM dolt_ignore WHERE pattern = %s", (IGNORE_PATTERN,)
    )
    if not rows or not all(bool(row[1]) for row in rows):
        return False
    dirty = _fetch(cursor, "SELECT table_name FROM dolt_status WHERE table_name = 'dolt_ignore'")
    return not dirty


def provision_liveness_schema(cursor, policy: LivenessPolicy | None = None) -> bool:
    """Idempotent: run :func:`liveness_schema_statements` unless already committed. Returns
    whether anything was written."""
    if _ignore_committed(cursor) and _existing(cursor, (LIVENESS_POLICY_TABLE,)):
        return False
    for statement in liveness_schema_statements(policy):
        cursor.execute(statement)
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
    for statement in (*session_ddl(principal, epoch), *evidence_ddl(principal, epoch)):
        cursor.execute(statement)
    for statement in account_statements(
        principal, epoch, frame_address=frame_address, database=database, inbox=inbox
    ):
        cursor.execute(statement, (password,) if "%s" in statement else None)
    cursor.execute("CALL DOLT_ADD('dolt_schemas')")
    cursor.execute(
        "CALL DOLT_COMMIT('-m',%s,'--author','HQ operator <hq-operator@localhost>')",
        (f"HQ session/evidence triggers for {principal} incarnation {epoch}",),
    )
    problems = check_provisioning(cursor, principal, epoch, database=database, inbox=inbox)
    if problems:
        raise SessionError("provisioning check failed: " + "; ".join(problems))
    return problems


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
    problems: list[str] = []
    if not _ignore_committed(cursor):
        problems.append(f"ignore rule {IGNORE_PATTERN!r} is not committed")
    present = _existing(cursor, (session, evidence, LIVENESS_POLICY_TABLE))
    if LIVENESS_POLICY_TABLE not in present:
        problems.append(f"{LIVENESS_POLICY_TABLE} missing")
    else:
        try:
            _policy(_fetch(cursor, _POLICY_SQL))
        except SessionError as exc:
            problems.append(str(exc))
    for table in (session, evidence):
        if table not in present:
            problems.append(f"{table} missing")
            continue
        rows = _fetch(cursor, f"SELECT id FROM {table}")
        if [tuple(row) for row in rows] != [(1,)]:
            problems.append(f"{table} must hold exactly its one id = 1 row")
    triggers = _fetch(
        cursor,
        "SELECT trigger_name, event_object_table, action_statement, action_timing, "
        "event_manipulation FROM information_schema.triggers WHERE trigger_schema = %s",
        (database,),
    )
    for table, kind in ((session, "session"), (evidence, "evidence")):
        stamp = f"SET NEW.{_STAMP_COLUMN[kind]} = UTC_TIMESTAMP(6)"
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
    secure = _fetch(cursor, "SELECT @@require_secure_transport")
    secure_transport = bool(secure and secure[0][0] in (1, "1", "ON", "on"))
    accounts = _fetch(cursor, "SELECT host, ssl_type FROM mysql.user WHERE user = %s", (principal,))
    if not accounts:
        problems.append(f"no account for principal {principal!r}")
    writable = (session, evidence, *((inbox_table(principal, epoch),) if inbox else ()))
    for host, ssl_type in accounts:
        problems += [
            f"{principal}: {p}"
            for p in check_account(host, ssl_type, secure_transport=secure_transport)
        ]
        lines = [row[0] for row in _fetch(cursor, f"SHOW GRANTS FOR '{principal}'@'{host}'")]
        problems += [
            f"{principal}@{host}: {p}"
            for p in check_grants(lines, role="frame", database=database, writable=writable)
        ]
    problems += [
        f"hive database {name!r} is co-hosted on the HQ server"
        for name in cohosted_hive_databases(cursor)
    ]
    return problems


def cohosted_hive_databases(cursor) -> list[str]:
    """Databases on this server that carry a hive marker table (bd's ``issues`` or the fence)."""
    rows = _fetch(
        cursor,
        "SELECT DISTINCT table_schema FROM information_schema.tables WHERE table_name IN "
        f"({','.join(['%s'] * len(HIVE_DATABASE_MARKERS))})",
        HIVE_DATABASE_MARKERS,
    )
    return sorted({row[0] for row in rows} - _SYSTEM_SCHEMAS)
