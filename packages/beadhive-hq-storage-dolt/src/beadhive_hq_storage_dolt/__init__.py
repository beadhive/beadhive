"""Dolt adapter for the HQ storage port (``docs/design/hq-storage-port-adr.md``, bh-br1cd).

A **library package**: it imports ``beadhive_hq_storage`` only through its ``__all__``, never
imports ``beadhive``, and never imports PyMySQL. Root injects a DB-API connection factory
(:data:`ConnectionFactory`, over ``hq_sql_transport.connect``) for HQ and a statement engine
(:class:`FenceStatementEngine`, its ``DoltCliEngine`` / ``BdServerEngine``) for hive fences. Every
SQL string, Dolt procedure call and error-code classification for the port lives here.

The reusable ``dolt`` contract adapter, its harness and an owned ``dolt sql-server`` for tests
are in :mod:`beadhive_hq_storage_dolt.contract`.
"""

from __future__ import annotations

from ._capabilities import (
    CommitInfo,
    DoltAccounts,
    DoltHistory,
    DoltMerge,
    FenceTable,
    HistoryRow,
    MergeRefused,
    SqlDoltAccounts,
    SqlDoltHistory,
    SqlDoltMerge,
    SqlRunner,
    connection_runner,
    engine_runner,
)
from ._classify import (
    COMMIT_ERROR,
    CONFLICT_CODES,
    DEADLINE_CODES,
    DUPLICATE_KEY,
    NO_SUCH_TABLE,
    SERIALIZATION_FAILURE,
    TRANSPORT_CODES,
    DeadlinePredicate,
    error_code,
    unknown_reason,
)
from ._config import (
    CONFIG_TABLES,
    DoltConfigHistory,
    config_change_set,
    config_document_kind,
    config_documents_digest,
)
from ._failover import POLICY_TABLE as FAILOVER_POLICY_TABLE
from ._failover import failover_policy_grant, policy_revision, policy_statements
from ._fence import DoltHiveFence, FenceStatementEngine, SqlConnectionFenceEngine
from ._hq import (
    ConnectionFactory,
    DbApiConnection,
    DbApiCursor,
    DoltHqStorage,
    DoltPlacementStore,
    Role,
)
from ._incarnation import (
    ELIGIBILITY_SQL,
    EVIDENCE_COLUMNS,
    EVIDENCE_SQL,
    IGNORE_PATTERN,
    LIVENESS_POLICY_TABLE,
    POLICY_SQL,
    RENEW_SQL,
    STAMP_COLUMN,
    UNMEASURED,
    account_statements,
    evidence_ddl,
    incarnation_key,
    incarnation_table,
    liveness_schema_statements,
    parse_incarnation_key,
    session_ddl,
    stamp_trigger,
)
from ._placement import (
    CAUSE_DOMAIN,
    PLACEMENT_CAUSES,
    PLACEMENT_DOMAIN,
    cause_token,
    operation_revision,
    placement_cause,
    placement_conformance_rows,
    placement_seed_statement,
    placement_witness,
)
from ._runtime import DoltRuntimeReads, HqRuntimeStatements, SqlDoltRuntimeReads
from ._schema import (
    APPEND_TABLE,
    COMMITTED_TABLES,
    DOCUMENTS_TABLE,
    FENCE_TABLES,
    LIVENESS_TABLE,
    PLACEMENT_TABLE,
    PUBLICATIONS_TABLE,
    RECORDS_TABLE,
    bump_statements,
    committed_schema,
    live_schema,
    reclaim_statements,
)

__all__ = [
    # the generic port adapters
    "DoltConfigHistory",
    "DoltHiveFence",
    "DoltHqStorage",
    "DoltPlacementStore",
    # injection seams
    "ConnectionFactory",
    "DbApiConnection",
    "DbApiCursor",
    "DeadlinePredicate",
    "FenceStatementEngine",
    "Role",
    "SqlConnectionFenceEngine",
    # Dolt-only capabilities
    "CommitInfo",
    "DoltAccounts",
    "DoltHistory",
    "DoltMerge",
    "DoltRuntimeReads",
    "FenceTable",
    "HistoryRow",
    "MergeRefused",
    "SqlDoltAccounts",
    "SqlDoltHistory",
    "SqlDoltMerge",
    "SqlDoltRuntimeReads",
    "SqlRunner",
    "connection_runner",
    "engine_runner",
    # the HQ runtime database's Dolt-only statements (bh-xnn5p)
    "HqRuntimeStatements",
    # error-code classification
    "COMMIT_ERROR",
    "CONFLICT_CODES",
    "DEADLINE_CODES",
    "DUPLICATE_KEY",
    "NO_SUCH_TABLE",
    "SERIALIZATION_FAILURE",
    "TRANSPORT_CODES",
    "error_code",
    "unknown_reason",
    # schema and wire format
    "APPEND_TABLE",
    "CAUSE_DOMAIN",
    "COMMITTED_TABLES",
    "CONFIG_TABLES",
    "DOCUMENTS_TABLE",
    "ELIGIBILITY_SQL",
    "EVIDENCE_COLUMNS",
    "EVIDENCE_SQL",
    "FAILOVER_POLICY_TABLE",
    "FENCE_TABLES",
    "IGNORE_PATTERN",
    "LIVENESS_POLICY_TABLE",
    "LIVENESS_TABLE",
    "PLACEMENT_CAUSES",
    "PLACEMENT_DOMAIN",
    "PLACEMENT_TABLE",
    "POLICY_SQL",
    "PUBLICATIONS_TABLE",
    "RECORDS_TABLE",
    "RENEW_SQL",
    "STAMP_COLUMN",
    "UNMEASURED",
    "account_statements",
    "bump_statements",
    "cause_token",
    "committed_schema",
    "config_change_set",
    "config_document_kind",
    "config_documents_digest",
    "evidence_ddl",
    "failover_policy_grant",
    "incarnation_key",
    "incarnation_table",
    "live_schema",
    "liveness_schema_statements",
    "operation_revision",
    "parse_incarnation_key",
    "placement_cause",
    "placement_conformance_rows",
    "placement_seed_statement",
    "placement_witness",
    "policy_revision",
    "policy_statements",
    "reclaim_statements",
    "session_ddl",
    "stamp_trigger",
]
