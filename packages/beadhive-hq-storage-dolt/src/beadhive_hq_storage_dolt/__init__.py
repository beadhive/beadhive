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
    SERIALIZATION_FAILURE,
    TRANSPORT_CODES,
    DeadlinePredicate,
    error_code,
    unknown_reason,
)
from ._fence import DoltHiveFence, FenceStatementEngine, SqlConnectionFenceEngine
from ._hq import (
    PLACEMENT_DOMAIN,
    ConnectionFactory,
    DbApiConnection,
    DbApiCursor,
    DoltHqStorage,
    DoltPlacementStore,
    Role,
    placement_witness,
)
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
    "FenceTable",
    "HistoryRow",
    "MergeRefused",
    "SqlDoltAccounts",
    "SqlDoltHistory",
    "SqlDoltMerge",
    "SqlRunner",
    "connection_runner",
    "engine_runner",
    # error-code classification
    "COMMIT_ERROR",
    "CONFLICT_CODES",
    "DEADLINE_CODES",
    "SERIALIZATION_FAILURE",
    "TRANSPORT_CODES",
    "error_code",
    "unknown_reason",
    # schema and wire format
    "APPEND_TABLE",
    "COMMITTED_TABLES",
    "DOCUMENTS_TABLE",
    "FENCE_TABLES",
    "LIVENESS_TABLE",
    "PLACEMENT_DOMAIN",
    "PLACEMENT_TABLE",
    "PUBLICATIONS_TABLE",
    "RECORDS_TABLE",
    "bump_statements",
    "committed_schema",
    "live_schema",
    "placement_witness",
    "reclaim_statements",
]
