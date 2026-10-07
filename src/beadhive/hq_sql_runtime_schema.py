"""Fresh-only operator DDL for a runtime database separate from config custody.

Callers must execute these statements with an isolated server-local operator.  The
application never creates tables or grants while attaching a host.  Commit the
ignore rule *before* creating live tables, so observations and receipts cannot
become part of a later versioned authority commit.
"""

from __future__ import annotations

import re


class RuntimeSchemaError(ValueError):
    """A runtime table or identity is not an operator-provisioned identifier."""


COMMITTED_SCHEMA = (
    "CREATE TABLE hq_authority ("
    "singleton_id TINYINT PRIMARY KEY, schema_version INT NOT NULL, "
    "backend_identity VARCHAR(128) NOT NULL, generation VARCHAR(128) NOT NULL, "
    "revision BIGINT UNSIGNED NOT NULL, config_backend VARCHAR(128) NOT NULL, "
    "config_generation VARCHAR(128) NOT NULL, config_head VARCHAR(128) NOT NULL, "
    "state_json LONGBLOB NOT NULL, state_sha256 CHAR(64) NOT NULL, "
    "hive_policies_json LONGBLOB NOT NULL, hive_policies_sha256 CHAR(64) NOT NULL, "
    "operator_signature BLOB NOT NULL)",
    "CREATE TABLE hq_principal_registry ("
    "principal VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY, "
    "frame_id VARCHAR(128) NOT NULL, holder_identity VARCHAR(128) NOT NULL, "
    "instance_ref VARCHAR(128) NOT NULL, epoch BIGINT UNSIGNED NOT NULL, "
    "inbox_table VARCHAR(64) NOT NULL UNIQUE, signer_fingerprint VARCHAR(128) NOT NULL, "
    "UNIQUE KEY incarnation (frame_id, holder_identity, instance_ref, epoch))",
    "INSERT INTO dolt_ignore VALUES ('hq_live_*', TRUE)",
    "CALL DOLT_ADD('hq_authority','hq_principal_registry','dolt_ignore')",
    "CALL DOLT_COMMIT('-m','HQ runtime schema and ignore policy',"
    "'--author','HQ operator <hq-operator@localhost>')",
)

LIVE_SCHEMA = (
    # This table is the only frame-writable surface, one table per authenticated
    # principal/incarnation.  An UPSERT can rewrite sender evidence, never protected
    # registry, receipt, accepted floor, global lease, or accepted result.
    "CREATE TABLE {inbox} (request_id CHAR(36) PRIMARY KEY, kind VARCHAR(32) NOT NULL, "
    "payload LONGBLOB NOT NULL, payload_sha256 CHAR(64) NOT NULL, "
    "created_at DOUBLE NOT NULL)",
)

PROTECTED_LIVE_SCHEMA = (
    "CREATE TABLE hq_live_receipts ("
    "frame_id VARCHAR(128) NOT NULL, holder_identity VARCHAR(128) NOT NULL, "
    "epoch BIGINT UNSIGNED NOT NULL, sequence BIGINT UNSIGNED NOT NULL, "
    "digest VARCHAR(72) NOT NULL, first_seen DOUBLE NOT NULL, "
    "lease_json LONGBLOB NOT NULL, envelope_json LONGBLOB NOT NULL, "
    "signer_fingerprint VARCHAR(128) NOT NULL, "
    "PRIMARY KEY (frame_id,holder_identity,epoch,sequence), "
    "UNIQUE KEY digest_identity (frame_id,holder_identity,epoch,digest))",
    "CREATE TABLE hq_live_floors ("
    "frame_id VARCHAR(128) NOT NULL, holder_identity VARCHAR(128) NOT NULL, "
    "epoch BIGINT UNSIGNED NOT NULL, sequence BIGINT UNSIGNED NOT NULL, "
    "digest VARCHAR(72) NOT NULL, first_seen DOUBLE, "
    "PRIMARY KEY (frame_id,holder_identity,epoch))",
    "CREATE TABLE hq_live_hive_leases ("
    "prefix VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY, "
    "revision CHAR(64) NOT NULL, lease_json LONGBLOB NOT NULL, "
    "request_id CHAR(36) NOT NULL, request_sha256 CHAR(64) NOT NULL)",
    "CREATE TABLE hq_live_public_observations ("
    "frame_id VARCHAR(128) NOT NULL, holder_identity VARCHAR(128) NOT NULL, "
    "epoch BIGINT UNSIGNED NOT NULL, sequence BIGINT UNSIGNED NOT NULL, "
    "digest VARCHAR(72) NOT NULL, accepted_until DOUBLE NOT NULL, "
    "lease_json LONGBLOB NOT NULL, envelope_json LONGBLOB NOT NULL, "
    "signer_fingerprint VARCHAR(128) NOT NULL, "
    "PRIMARY KEY (frame_id,holder_identity,epoch))",
    "CREATE TABLE hq_live_registrations ("
    "frame_id VARCHAR(128) NOT NULL, holder_identity VARCHAR(128) NOT NULL, "
    "instance_ref VARCHAR(128) NOT NULL, epoch BIGINT UNSIGNED NOT NULL, "
    "request_id CHAR(36) NOT NULL, request_sha256 CHAR(64) NOT NULL, "
    "manifest_json LONGBLOB NOT NULL, accepted_at DOUBLE NOT NULL, "
    "signer_fingerprint VARCHAR(128) NOT NULL, "
    "PRIMARY KEY (frame_id,holder_identity,instance_ref,epoch))",
    "CREATE TABLE hq_live_results ("
    "request_id CHAR(36) PRIMARY KEY, request_sha256 CHAR(64) NOT NULL, "
    "principal VARCHAR(64) NOT NULL, frame_id VARCHAR(128) NOT NULL, "
    "holder_identity VARCHAR(128) NOT NULL, instance_ref VARCHAR(128) NOT NULL, "
    "epoch BIGINT UNSIGNED NOT NULL, audience VARCHAR(128) NOT NULL, "
    "signer_fingerprint VARCHAR(128) NOT NULL, expected_revision VARCHAR(128) NOT NULL, "
    "result_revision VARCHAR(128), status VARCHAR(32) NOT NULL, accepted_at DOUBLE NOT NULL)",
)


#: Operator-owned, committed liveness policy for session/evidence rows (bh-owqdg, ADR §5). Frames
#: hold SELECT only. It is created together with the ``frame_*`` ignore rule, so it exists
#: whenever any incarnation's session table does (see :mod:`beadhive.hq_sql_session`).
LIVENESS_POLICY_TABLE = "hq_liveness_policy"
LIVENESS_POLICY_SCHEMA = (
    f"CREATE TABLE {LIVENESS_POLICY_TABLE} ("
    "singleton_id TINYINT PRIMARY KEY CHECK (singleton_id = 1), "
    "session_ttl_s INT UNSIGNED NOT NULL, evidence_ttl_s INT UNSIGNED NOT NULL)",
)


#: Per-role, per-hive ``failover_after`` and the executor floor (bh-4biq8, ADR §4), beside the
#: placement row and read by no pre-0.23 reader (:mod:`beadhive.failover_policy`). The
#: ``hq_live_`` name keeps it under the ``dolt_ignore`` rule: a change never commits. Director
#: credential: SELECT, INSERT, UPDATE, DELETE here; frames: SELECT at most.
FAILOVER_POLICY_TABLE = "hq_live_failover_policy"
FAILOVER_POLICY_SCHEMA = (
    f"CREATE TABLE {FAILOVER_POLICY_TABLE} ("
    "scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL, "
    "setting VARCHAR(32) NOT NULL, seconds BIGINT NOT NULL, "
    "PRIMARY KEY (scope, setting))",
)


def _incarnation(principal: str, epoch: int) -> None:
    if (
        not isinstance(principal, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", principal)
        or type(epoch) is not int
        or epoch < 0
        or epoch > 9999999999
    ):
        raise RuntimeSchemaError("invalid provisioned runtime principal or incarnation")


def inbox_table(principal: str, epoch: int) -> str:
    """Derive a stable SQL identifier; never accept a sender-named table."""
    _incarnation(principal, epoch)
    return f"hq_live_inbox_{principal}_{epoch}"


def session_table(principal: str, epoch: int) -> str:
    """The incarnation's single-row, server-stamped liveness table (bh-owqdg, ADR §5)."""
    _incarnation(principal, epoch)
    return f"frame_{principal}_{epoch}_session"


def evidence_table(principal: str, epoch: int) -> str:
    """The incarnation's single-row conformance evidence table (bh-owqdg, ADR §5)."""
    _incarnation(principal, epoch)
    return f"frame_{principal}_{epoch}_evidence"


def routed_table_valid(table: str, principal: str, epoch: int) -> bool:
    """Whether a registry ``inbox_table`` value is one this incarnation may be routed to.

    A Φ3 (dual-write) or legacy incarnation is routed to its signed inbox; a session-only
    incarnation (no inbox provisioned) is routed to its session table, the ADR's "the
    registry's ``inbox_table`` column names the new tables". Never a sender-named table.
    """
    try:
        return table in (inbox_table(principal, epoch), session_table(principal, epoch))
    except RuntimeSchemaError:
        return False


def routed_to_inbox(table: str, principal: str, epoch: int) -> bool:
    """Whether the incarnation still has a signed inbox (legacy, or Φ3 dual-write)."""
    try:
        return table == inbox_table(principal, epoch)
    except RuntimeSchemaError:
        return False


def inbox_ddl(principal: str, epoch: int) -> str:
    """The caller checks exact registry uniqueness before executing fresh-only DDL."""
    return LIVE_SCHEMA[0].format(inbox=inbox_table(principal, epoch))
