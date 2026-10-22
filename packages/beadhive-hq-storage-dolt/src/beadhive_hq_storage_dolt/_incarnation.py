"""Per-incarnation session and evidence tables in the HQ runtime database (bh-xnn5p).

Root's ``hq_sql_session`` (bh-owqdg, hive-writer-partitioning ADR section 5) gives each frame
incarnation two operator-provisioned, single-row tables, ``frame_<principal>_<epoch>_session``
and ``frame_<principal>_<epoch>_evidence``, whose ``renewed_at`` / ``measured_at`` a ``BEFORE
UPDATE`` trigger stamps with the server clock. This module owns their SQL: the operator DDL, the
renewal and evidence ``UPDATE``\\ s, and the one-statement eligibility read. Root keeps the
identifier validation, the policy values and every threshold.

It also maps the port's ``Liveness`` onto those tables for :class:`DoltHqStorage`
(``runtime_tables=True``): a ``SESSION`` / ``EVIDENCE`` key is ``"<principal>/<epoch>"``, an age
is ``TIMESTAMPDIFF(MICROSECOND, <stamp>, UTC_TIMESTAMP(6))`` in the same read, and a renewal is
the provisioned row's ``UPDATE`` (never an ``INSERT``: frames hold ``SELECT, UPDATE`` only).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from beadhive_hq_storage import InvalidRequest, LivenessKind

__all__ = [
    "ELIGIBILITY_SQL",
    "EVIDENCE_COLUMNS",
    "EVIDENCE_SQL",
    "IGNORE_PATTERN",
    "LIVENESS_POLICY_TABLE",
    "POLICY_SQL",
    "RENEW_SQL",
    "STAMP_COLUMN",
    "UNMEASURED",
    "account_statements",
    "evidence_ddl",
    "incarnation_key",
    "incarnation_table",
    "liveness_schema_statements",
    "parse_incarnation_key",
    "session_ddl",
    "stamp_trigger",
]

IGNORE_PATTERN: Final = "frame_*"
"""The ``dolt_ignore`` rule committed before any incarnation table exists (root's)."""
LIVENESS_POLICY_TABLE: Final = "hq_liveness_policy"
"""Root ``hq_sql_runtime_schema.LIVENESS_POLICY_TABLE``: the operator's committed TTL row."""
UNMEASURED: Final = "unmeasured"
STAMP_COLUMN: Final = {"session": "renewed_at", "evidence": "measured_at"}
EVIDENCE_COLUMNS: Final = (
    "epoch",
    "release_id",
    "release_digest",
    "profile",
    "status",
    "report_digest",
)
"""The evidence row's frame-written columns, in :data:`EVIDENCE_SQL` order."""

_PRINCIPAL = re.compile(r"[a-z][a-z0-9_]{0,30}")
_TABLE = re.compile(r"frame_([a-z][a-z0-9_]{0,30})_([0-9]{1,10})_(session|evidence)")
_KEY = re.compile(r"([a-z][a-z0-9_]{0,30})/([0-9]{1,10})")
_ROUTE = {LivenessKind.SESSION: "session", LivenessKind.EVIDENCE: "evidence"}

RENEW_SQL: Final = "UPDATE {session} SET epoch = epoch WHERE id = 1"
"""One renewal: the operator trigger stamps ``renewed_at``; the frame supplies nothing."""
EVIDENCE_SQL: Final = (
    "UPDATE {evidence} SET epoch = %s, release_id = %s, release_digest = %s, profile = %s, "
    "status = %s, report_digest = %s WHERE id = 1"
)
POLICY_SQL: Final = f"SELECT session_ttl_s, evidence_ttl_s FROM {LIVENESS_POLICY_TABLE}"

#: Eligibility in ONE statement: grant route (committed, ``AS OF`` the authority head) ⋈ session
#: ⋈ evidence ⋈ policy (committed) ⟕ placement. Ages are by the HQ server's own clock and are
#: returned with the committed TTLs; the freshness THRESHOLDS are root's (bh-xnn5p), which
#: compares ``age_us < ttl * 1000000`` exactly as this statement once did. The equality
#: predicates stay here, under the columns' own collation. Never ``UTC_TIMESTAMP(6) - INTERVAL n
#: SECOND``: Dolt 2.3.5 drops its fractional seconds (bh-7crof; upstream draft dolt-5).
ELIGIBILITY_SQL: Final = (
    "SELECT r.epoch, s.epoch, s.renewed_at, e.epoch, e.release_id, e.release_digest, "
    "e.profile, e.status, e.report_digest, e.measured_at, "
    "TIMESTAMPDIFF(MICROSECOND, s.renewed_at, UTC_TIMESTAMP(6)) AS session_age_us, "
    "TIMESTAMPDIFF(MICROSECOND, e.measured_at, UTC_TIMESTAMP(6)) AS evidence_age_us, "
    "p.session_ttl_s, p.evidence_ttl_s, "
    "COALESCE(s.epoch = r.epoch, FALSE) AS session_epoch_current, "
    "COALESCE(e.epoch = r.epoch AND e.status = 'conformant' AND e.profile = %s, FALSE) "
    "AS evidence_conformant, "
    "COALESCE(e.epoch = r.epoch AND e.release_digest = %s, FALSE) AS release_matches, "
    "COALESCE(l.prefix IS NOT NULL AND JSON_UNQUOTE(JSON_EXTRACT("
    "CONVERT(l.lease_json USING utf8mb4), '$.lease.host_id')) = r.holder_identity, FALSE) "
    "AS current_hive_lease_holder, "
    "SHA2(CONCAT_WS('|', e.epoch, e.release_id, e.release_digest, e.profile, e.status, "
    "e.report_digest, COALESCE(CAST(e.measured_at AS CHAR), '')), 256) AS evidence_digest "
    "FROM hq_principal_registry AS OF %s AS r "
    "JOIN {session} AS s ON s.id = 1 "
    "JOIN {evidence} AS e ON e.id = 1 "
    f"JOIN {LIVENESS_POLICY_TABLE} AS OF %s AS p ON p.singleton_id = 1 "
    "LEFT JOIN hq_live_hive_leases AS l ON l.prefix = %s "
    "WHERE r.principal = %s AND r.epoch = %s"
)


def incarnation_table(principal: str, epoch: int, route: str) -> str:
    """``frame_<principal>_<epoch>_<route>`` (root ``hq_sql_runtime_schema.session_table`` /
    ``evidence_table``), refusing anything that would not be a safe identifier."""
    if (
        route not in STAMP_COLUMN
        or not isinstance(principal, str)
        or not _PRINCIPAL.fullmatch(principal)
        or type(epoch) is not int
        or not 0 <= epoch <= 9_999_999_999
    ):
        raise InvalidRequest("invalid provisioned runtime principal or incarnation")
    return f"frame_{principal}_{epoch}_{route}"


def incarnation_key(principal: str, epoch: int) -> str:
    """The ``Liveness`` key of one incarnation: ``"<principal>/<epoch>"``."""
    incarnation_table(principal, epoch, "session")
    return f"{principal}/{epoch}"


def parse_incarnation_key(key: object) -> tuple[str, int]:
    """``(principal, epoch)`` from an :func:`incarnation_key`; ``InvalidRequest`` otherwise."""
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    if match is None:
        raise InvalidRequest("an incarnation liveness key is '<principal>/<epoch>'")
    principal, epoch = match.group(1), int(match.group(2))
    incarnation_table(principal, epoch, "session")
    return principal, epoch


def liveness_table(kind: LivenessKind, key: str) -> str:
    principal, epoch = parse_incarnation_key(key)
    return incarnation_table(principal, epoch, _ROUTE[LivenessKind(kind)])


def liveness_key(kind: LivenessKind, table: str) -> str | None:
    """The key a listed table belongs to, or ``None`` for any other table."""
    match = _TABLE.fullmatch(table)
    if match is None or match.group(3) != _ROUTE[LivenessKind(kind)]:
        return None
    return f"{match.group(1)}/{int(match.group(2))}"


def stamp_trigger(table: str, column: str) -> str:
    return (
        f"CREATE TRIGGER {table}_stamp BEFORE UPDATE ON {table} "
        f"FOR EACH ROW SET NEW.{column} = UTC_TIMESTAMP(6)"
    )


def session_ddl(table: str, epoch: int) -> tuple[str, ...]:
    """The session table, its stamping trigger and its one seeded, never-renewed row (no INSERT
    trigger: provisioning alone never makes a frame look live)."""
    return (
        f"CREATE TABLE {table} (id TINYINT PRIMARY KEY CHECK (id = 1), "
        "epoch BIGINT UNSIGNED NOT NULL, renewed_at DATETIME(6) NULL)",
        stamp_trigger(table, STAMP_COLUMN["session"]),
        f"INSERT INTO {table} (id, epoch, renewed_at) VALUES (1, {int(epoch)}, NULL)",
    )


def evidence_ddl(table: str, epoch: int) -> tuple[str, ...]:
    """The evidence table, its stamping trigger and its one seeded (unmeasured) row."""
    return (
        f"CREATE TABLE {table} (id TINYINT PRIMARY KEY CHECK (id = 1), "
        "epoch BIGINT UNSIGNED NOT NULL, release_id VARCHAR(128) NOT NULL, "
        "release_digest VARCHAR(80) NOT NULL, profile VARCHAR(64) NOT NULL, "
        "status VARCHAR(32) NOT NULL, report_digest VARCHAR(80) NOT NULL, "
        "measured_at DATETIME(6) NULL)",
        stamp_trigger(table, STAMP_COLUMN["evidence"]),
        f"INSERT INTO {table} VALUES (1, {int(epoch)}, '', '', '', '{UNMEASURED}', '', NULL)",
    )


def liveness_schema_statements(
    policy_schema: Sequence[str], session_ttl_s: int, evidence_ttl_s: int
) -> tuple[str, ...]:
    """Once per runtime database, before any incarnation table: the ``frame_*`` ignore rule and
    the committed policy row (root's ``policy_schema`` DDL), in one operator commit."""
    return (
        f"INSERT INTO dolt_ignore VALUES ('{IGNORE_PATTERN}', TRUE)",
        *policy_schema,
        f"INSERT INTO {LIVENESS_POLICY_TABLE} VALUES "
        f"(1, {int(session_ttl_s)}, {int(evidence_ttl_s)})",
        f"CALL DOLT_ADD('dolt_ignore','{LIVENESS_POLICY_TABLE}')",
        "CALL DOLT_COMMIT('-m','HQ session liveness policy and frame_* ignore rule',"
        "'--author','HQ operator <hq-operator@localhost>')",
    )


def account_statements(
    account: str,
    database: str,
    *,
    session: str,
    evidence: str,
    inbox: str | None = None,
    catalog: bool = False,
) -> tuple[str, ...]:
    """The host-pinned, TLS-required account (``account`` is root's validated
    ``'<principal>'@'<address>'``) and its grants. ``CREATE USER`` binds the password as its one
    ``%s`` parameter."""
    statements = [
        f"CREATE USER {account} IDENTIFIED BY %s REQUIRE SSL",
        f"GRANT SELECT, UPDATE ON `{database}`.`{session}` TO {account}",
        f"GRANT SELECT, UPDATE ON `{database}`.`{evidence}` TO {account}",
        f"GRANT SELECT ON `{database}`.`{LIVENESS_POLICY_TABLE}` TO {account}",
    ]
    if inbox is not None:
        statements.append(f"GRANT SELECT, INSERT ON `{database}`.`{inbox}` TO {account}")
    if catalog:
        # The signed release catalog (bh-vfrem.4): read-only, and only once it is provisioned.
        statements.append(f"GRANT SELECT ON `{database}`.`hq_release_catalog` TO {account}")
    return tuple(statements)
