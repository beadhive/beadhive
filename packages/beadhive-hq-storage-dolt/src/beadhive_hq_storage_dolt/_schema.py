"""Every SQL string the adapter's tables need: the HQ schema and the fence-bump statements.

**HQ** (one Dolt database). Committed, versioned tables carry the config publications; the
``hq_live_*`` tables are ``dolt_ignore``'d (root's ``hq_sql_runtime_schema`` rule), so a CAS, an
append or a renewal never makes a commit. The ignore rule is committed BEFORE any live table
exists (bh-v0k3i ordering). ``hq_live_hive_leases`` is root's placement table verbatim, so the
trusted receiver and 0.22.x readers keep reading the same row.

The stamping triggers (``SET NEW.<col> = UTC_TIMESTAMP(6)``, the only safe trigger body -- Dolt
checks trigger DML against the invoker, bh-wtsrc E3) live in ``dolt_schemas`` and are never
staged: a committed trigger whose table is ignored breaks ``information_schema.triggers`` on
Dolt 2.3.5 (it resolves the table at ``HEAD``, where an ignored table does not exist).

**Fence** statements are the bump root's ``writer_adopt`` step 2 commits and the guarded
``ReclaimSet`` revert that rides in it (once root ``failover_reclaim.reclaim_statements``),
rendered for an engine that takes no parameters.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC
from typing import Final

from beadhive_hq_storage import (
    CLAIM_FRAME_LABEL_PREFIX,
    RECOVERY_LABEL_PREFIX,
    RESUMABLE_LABEL,
    ReclaimSet,
)

from ._values import quote

__all__ = [
    "ADOPT_COMMIT_PREFIX",
    "APPEND_TABLE",
    "CLAIM_FRAME_DIMENSION",
    "COMMENT_STAMP_FORMAT",
    "COMMITTED_TABLES",
    "DOCUMENTS_TABLE",
    "FENCE_TABLES",
    "HQ_IGNORE_PATTERN",
    "LIVENESS_TABLE",
    "PLACEMENT_TABLE",
    "PUBLICATIONS_TABLE",
    "PUBLICATION_META_TABLE",
    "RECORDS_TABLE",
    "bump_statements",
    "committed_schema",
    "live_schema",
    "reclaim_statements",
]

PLACEMENT_TABLE: Final = "hq_live_hive_leases"
"""Root ``hq_sql_placement.PLACEMENT_TABLE``: one row per hive, UPDATE-only for the director."""
RECORDS_TABLE: Final = "hq_live_records"
"""Every other ``RecordKind`` (failover policy, authority) as one versioned row per key."""
APPEND_TABLE: Final = "hq_live_append"
LIVENESS_TABLE: Final = "hq_live_liveness"
DOCUMENTS_TABLE: Final = "hq_storage_documents"
PUBLICATIONS_TABLE: Final = "hq_storage_publications"
PUBLICATION_META_TABLE: Final = "hq_storage_publication_meta"
COMMITTED_TABLES: Final = (DOCUMENTS_TABLE, PUBLICATIONS_TABLE, PUBLICATION_META_TABLE)
"""The explicit staging allowlist of every publication (never ``DOLT_ADD('-A')``)."""
HQ_IGNORE_PATTERN: Final = "hq_live_*"
"""Root ``hq_sql_runtime_schema``'s ignore rule for live HQ tables."""

FENCE_TABLES: Final = ("bh_writer", "bh_epoch_live", "bh_write_mark")
ADOPT_COMMIT_PREFIX: Final = "bh: adopt "
"""Root ``writer_adopt.ADOPT_COMMIT_PREFIX``: a bump commit's subject is ``<prefix><frame>@<n>``."""
CLAIM_FRAME_DIMENSION: Final = CLAIM_FRAME_LABEL_PREFIX.rstrip(":")
"""Root ``work_backup.CLAIM_FRAME_DIMENSION``: the label a reclaim clears with the claim."""
COMMENT_STAMP_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
"""How a reclaim note's ``created_at`` is stored (whole seconds, UTC; bd's comment column)."""

_BIN = "CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"


def committed_schema(generation: str, author: str) -> list[str]:
    """Operator DDL for a fresh HQ database: the versioned publication tables and the live
    ignore rule, committed together (the provisioning commit every store starts with)."""
    return [
        f"CREATE TABLE {DOCUMENTS_TABLE} (path VARCHAR(255) {_BIN} PRIMARY KEY, "
        "content LONGBLOB NOT NULL)",
        f"CREATE TABLE {PUBLICATIONS_TABLE} (publication_id CHAR(36) PRIMARY KEY, "
        "sequence BIGINT UNSIGNED NOT NULL, parent_revision VARCHAR(64) NOT NULL, "
        "author VARCHAR(255) NOT NULL, document_count INT UNSIGNED NOT NULL, "
        f"UNIQUE KEY {PUBLICATIONS_TABLE}_sequence (sequence))",
        f"CREATE TABLE {PUBLICATION_META_TABLE} (singleton_id TINYINT PRIMARY KEY "
        "CHECK (singleton_id = 1), sequence BIGINT UNSIGNED NOT NULL, "
        "generation VARCHAR(128) NOT NULL)",
        f"INSERT INTO {PUBLICATION_META_TABLE} VALUES (1, 0, {quote(generation)})",
        f"REPLACE INTO dolt_ignore VALUES ({quote(HQ_IGNORE_PATTERN)}, 1)",
        "CALL DOLT_ADD(" + ", ".join(quote(t) for t in (*COMMITTED_TABLES, "dolt_ignore")) + ")",
        "CALL DOLT_COMMIT('-m', 'HQ storage schema and live ignore policy', '--author', "
        f"{quote(author)})",
    ]


def _stamp_triggers(table: str, column: str) -> list[str]:
    return [
        f"CREATE TRIGGER {table}_{event[:3]} BEFORE {event} ON {table} FOR EACH ROW "
        f"SET NEW.{column} = UTC_TIMESTAMP(6)"
        for event in ("INSERT", "UPDATE")
    ]


def live_schema() -> list[str]:
    """The ``dolt_ignore``'d live tables and their server-clock stamping triggers. Run after
    :func:`committed_schema` (the ignore rule must already be committed)."""
    return [
        # Root's placement row, column for column (receiver and 0.22.x reader compatible).
        f"CREATE TABLE {PLACEMENT_TABLE} (prefix VARCHAR(512) {_BIN} PRIMARY KEY, "
        "revision CHAR(64) NOT NULL, lease_json LONGBLOB NOT NULL, "
        "request_id CHAR(36) NOT NULL, request_sha256 CHAR(64) NOT NULL)",
        f"CREATE TABLE {RECORDS_TABLE} (kind VARCHAR(32) NOT NULL, "
        f"record_key VARCHAR(255) {_BIN} NOT NULL, revision CHAR(64) NOT NULL, "
        "body LONGBLOB NOT NULL, operation_id CHAR(36) NOT NULL, "
        "recorded_at DATETIME(6) NOT NULL, PRIMARY KEY (kind, record_key))",
        *_stamp_triggers(RECORDS_TABLE, "recorded_at"),
        f"CREATE TABLE {APPEND_TABLE} (seq BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
        f"stream VARCHAR(32) NOT NULL, entry_key VARCHAR(255) {_BIN} NOT NULL, "
        "revision CHAR(64) NOT NULL, body LONGBLOB NOT NULL, operation_id CHAR(36) NOT NULL, "
        f"recorded_at DATETIME(6) NOT NULL, UNIQUE KEY {APPEND_TABLE}_key (stream, entry_key), "
        f"UNIQUE KEY {APPEND_TABLE}_revision (revision))",
        *_stamp_triggers(APPEND_TABLE, "recorded_at"),
        f"CREATE TABLE {LIVENESS_TABLE} (kind VARCHAR(32) NOT NULL, "
        f"live_key VARCHAR(255) {_BIN} NOT NULL, body LONGBLOB NULL, "
        "renewed_at DATETIME(6) NOT NULL, PRIMARY KEY (kind, live_key))",
        *_stamp_triggers(LIVENESS_TABLE, "renewed_at"),
    ]


def bump_statements(frame: str, epoch: int, revision: str) -> list[str]:
    """Root ``writer_adopt.bump_statements``: name the writer with a fresh revision, retire every
    older epoch's marks, move the singleton live epoch, and insert the ``adopt-<epoch>``
    sentinel so EVERY bump changes ``bh_write_mark`` (conditions 1 and 2)."""
    epoch = int(epoch)
    return [
        f"UPDATE bh_writer SET frame = {quote(frame)}, epoch = {epoch}, "
        f"revision = {quote(revision)} WHERE id = 1",
        f"DELETE FROM bh_write_mark WHERE epoch < {epoch}",
        f"UPDATE bh_epoch_live SET epoch = {epoch} WHERE id = 1",
        f"INSERT INTO bh_write_mark (id, epoch, tbl) VALUES "
        f"({quote(f'adopt-{epoch}')}, {epoch}, 'bh_writer')",
    ]


def reclaim_statements(reclaim: ReclaimSet) -> list[str]:
    """A ``ReclaimSet`` as root's ``failover_reclaim`` wrote it before the port (settled by
    bh-jhgum): exactly what ``bd unclaim --force`` leaves (``status = 'open'``, ``assignee =
    ''``, ``started_at = NULL``) plus the cleared ``claim-frame`` label; for a resumable bead,
    every ``recovery:*`` label replaced by ``recovery:resumable``; and one audit comment per
    note. Every write is guarded on ``status = 'in_progress'`` -- the label and comment writes
    run before the lifecycle UPDATE so their guard still sees the claim -- so a plan never
    touches a bead that moved on."""
    ids: Sequence[str] = sorted(reclaim.bead_ids)
    if not ids:
        return []

    def claimed(beads: Iterable[str]) -> str:
        listed = ", ".join(quote(bead) for bead in sorted(beads))
        return f"SELECT id FROM issues WHERE id IN ({listed}) AND status = 'in_progress'"

    out = [
        f"DELETE FROM labels WHERE issue_id IN ({claimed(ids)}) "
        f"AND label LIKE {quote(CLAIM_FRAME_LABEL_PREFIX + '%')}",
    ]
    if reclaim.resumable:
        out += [
            f"DELETE FROM labels WHERE issue_id IN ({claimed(reclaim.resumable)}) "
            f"AND label LIKE {quote(RECOVERY_LABEL_PREFIX + '%')}",
            f"INSERT INTO labels (issue_id, label) SELECT id, {quote(RESUMABLE_LABEL)} "
            f"FROM issues WHERE id IN ({', '.join(quote(b) for b in sorted(reclaim.resumable))}) "
            "AND status = 'in_progress'",
        ]
    for note in reclaim.notes:
        stamp = note.created_at.astimezone(UTC).strftime(COMMENT_STAMP_FORMAT)
        out.append(
            "INSERT INTO comments (id, issue_id, author, text, created_at) "
            f"SELECT {quote(note.comment_id)}, id, {quote(note.author)}, {quote(note.text)}, "
            f"{quote(stamp)} FROM issues WHERE id = {quote(note.bead_id)} "
            "AND status = 'in_progress'"
        )
    listed = ", ".join(quote(bead) for bead in ids)
    out.append(
        "UPDATE issues SET status = 'open', assignee = '', started_at = NULL "
        f"WHERE id IN ({listed}) AND status = 'in_progress'"
    )
    return out
