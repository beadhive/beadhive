"""``CommittedHistory`` over root's committed HQ config tables (bh-x7xtc; hq-storage-port-adr
section 2, "Committed history").

The generic :class:`~._hq.DoltHqStorage` publishes into the adapter-owned
``hq_storage_documents`` table. HQ configuration already has a committed, receiver- and
0.22.x-compatible layout in its own database, read by the runtime authority, the frames and
the operator surfaces, so :class:`DoltConfigHistory` maps the port's publication onto those
three tables instead, column for column as root's ``hq_sql_config`` always wrote them:

* ``hq_config_meta`` -- the singleton head: ``schema_version``, ``publication_sequence``,
  ``publication_id``, ``documents_sha256`` and ``document_count`` move by a sequence CAS;
  ``backend_identity``, ``generation`` and ``initial_attempt_nonce`` never change here;
* ``hq_config_documents`` -- replaced wholesale: one row per document, its ``ordinal`` the
  change set's order, its ``kind`` from :func:`config_document_kind`, its ``content_sha256``;
* ``hq_config_publications`` -- one appended witness row: the ``OperationId`` is the
  publication UUID, ``expected_parent_revision`` is the ORIGINAL expected head.

**The change set.** ``ChangeSet.tables`` must be exactly :data:`CONFIG_TABLES`, which is also
the explicit ``DOLT_ADD`` list (never ``-A``). The documents are ``hq_config_meta/schema_version``
(ASCII decimal) followed by ``hq_config_documents/<path>`` raw UTF-8 bytes in order; nothing
else. The digest (:func:`config_documents_digest`) and kinds restate root originals
(``ordered_documents_digest`` and ``hq_sql_config._kind``);
``tests/contracts/test_hq_storage_composition.py`` proves they agree.

**Lookup.** ``lookup_publication`` confirms a witness row ``AS OF`` the current head whose
``expected_parent_revision`` is the expected head and whose parent the head descends from
(``HAS_ANCESTOR``). It needs no grant on ``dolt_log``, ``dolt_commit_ancestors`` or
``dolt_history_*`` (the config reader holds none), so ``Confirmed.revision`` is the head the
witness is visible at, not the publication's own commit, and ``recorded_at`` is the server
clock at lookup -- an upper bound on the write, as for placement. ``Applied.revision`` from
``publish`` is still the exact committed hash.

Initial seeding into the empty schema (``hq_sql_config.initialize_snapshot``) is a separate
``bh-4shu3`` contract and is not on the port.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Final

from beadhive_hq_storage import (
    Absent,
    Applied,
    ChangeSet,
    Confirmed,
    Conflict,
    ConsistencyToken,
    IntegrityViolation,
    InvalidRequest,
    LookupOutcome,
    NotConfirmed,
    NotProvisioned,
    OperationId,
    Outcome,
    Revision,
    StorageError,
    StorageUnavailable,
    SyncReport,
    Versioned,
)

from ._classify import DeadlinePredicate
from ._hq import (
    ConnectionFactory,
    DbApiConnection,
    DbApiCursor,
    DoltHqStorage,
    Role,
    _author,
    _commit_transaction,
    _ConflictMarker,
    _rollback,
    _text,
)
from ._values import aware, check_operation

__all__ = [
    "CONFIG_DOCUMENTS_TABLE",
    "CONFIG_META_TABLE",
    "CONFIG_PUBLICATIONS_TABLE",
    "CONFIG_SCHEMA_VERSION",
    "CONFIG_TABLES",
    "DoltConfigHistory",
    "config_change_set",
    "config_document_kind",
    "config_documents_digest",
]

CONFIG_META_TABLE: Final = "hq_config_meta"
CONFIG_DOCUMENTS_TABLE: Final = "hq_config_documents"
CONFIG_PUBLICATIONS_TABLE: Final = "hq_config_publications"
CONFIG_TABLES: Final = (CONFIG_META_TABLE, CONFIG_DOCUMENTS_TABLE, CONFIG_PUBLICATIONS_TABLE)
"""Root ``hq_sql_config.TABLES``: the staging allowlist and the only base tables allowed."""
CONFIG_SCHEMA_VERSION: Final = f"{CONFIG_META_TABLE}/schema_version"
"""The change-set document carrying the publication's storage ``schema_version``."""
_DOCUMENT_PREFIX: Final = f"{CONFIG_DOCUMENTS_TABLE}/"
_MAX_PATH: Final = 512  # hq_config_documents.path VARCHAR(512)
_BEADYARD_PATH: Final = "beadyard.json"  # root beadyard_identity.DOCUMENT_PATH


def config_document_kind(path: str) -> str:
    """Root ``hq_sql_config._kind``: the ``kind`` column of a config document path."""
    if path == _BEADYARD_PATH:
        return "beadyard"
    if path == "fleet.yaml":
        return "fleet"
    if path.startswith("workspace"):
        return "workspace"
    if path == "allowed_signers":
        return "trust"
    if path.startswith("hosts/"):
        return "host"
    if path.startswith("hives/"):
        return "hive"
    raise InvalidRequest("unsupported configuration document path")


def config_documents_digest(documents: Sequence[tuple[str, bytes]]) -> str:
    """Root ``ordered_documents_digest`` over ``(path, utf-8 content)`` pairs, in order."""
    digest = hashlib.sha256()
    for path, content in documents:
        raw_path = path.encode("utf-8")
        digest.update(len(raw_path).to_bytes(4, "big"))
        digest.update(raw_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def config_change_set(schema_version: int, documents: Sequence[tuple[str, bytes]]) -> ChangeSet:
    """The ``ChangeSet`` for one config publication: the schema version, then the documents."""
    return ChangeSet(
        documents=(
            (CONFIG_SCHEMA_VERSION, str(int(schema_version)).encode("ascii")),
            *((_DOCUMENT_PREFIX + path, content) for path, content in documents),
        ),
        tables=frozenset(CONFIG_TABLES),
    )


def _check_config_changes(changes: object) -> tuple[int, list[tuple[str, bytes]]]:
    """``(schema_version, [(path, content), ...])`` from a config change set, or refuse."""
    if not isinstance(changes, ChangeSet) or not changes.documents:
        raise InvalidRequest("a publication needs at least one document")
    if changes.tables != frozenset(CONFIG_TABLES):
        raise InvalidRequest("a config publication stages exactly the three hq_config tables")
    version: int | None = None
    documents: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for name, raw in changes.documents:
        if not isinstance(name, str) or not isinstance(raw, bytes):
            raise InvalidRequest("a config document is a '<table>/<name>' and raw bytes")
        if name == CONFIG_SCHEMA_VERSION:
            if version is not None or documents or not raw.isdigit():
                raise InvalidRequest("one leading ASCII schema_version document required")
            version = int(raw)
            continue
        path = name[len(_DOCUMENT_PREFIX) :] if name.startswith(_DOCUMENT_PREFIX) else ""
        if not path or len(path.encode("utf-8")) > _MAX_PATH or path in seen:
            raise InvalidRequest(f"document {name!r} is not one hq_config_documents/<path>")
        config_document_kind(path)
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidRequest(f"document {name!r} is not UTF-8") from None
        seen.add(path)
        documents.append((path, raw))
    if version is None or not documents:
        raise InvalidRequest("a config publication needs a schema_version and documents")
    return version, documents


class DoltConfigHistory:
    """``CommittedHistory`` over the HQ config database (``hq_config_*``).

    It reuses :class:`~._hq.DoltHqStorage`'s sessions, principal checks and phase
    classification; ``fetch`` and ``push`` are that store's. Reads and lookups use
    ``Role.READER``, ``publish`` / ``fetch`` / ``push`` use ``Role.PUBLISHER``."""

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        remote: str = "origin",
        branch: str = "main",
        deadline: DeadlinePredicate | None = None,
        principals: Mapping[Role, tuple[str, str]] | None = None,
    ) -> None:
        self._store = DoltHqStorage(
            connect, remote=remote, branch=branch, deadline=deadline, principals=principals
        )

    # -- reads -------------------------------------------------------------------------------

    def head(self) -> Revision:
        return self._store.head()

    def read_at(self, revision: Revision) -> Sequence[tuple[str, bytes]]:
        if not isinstance(revision, str) or not revision.isalnum():
            raise NotProvisioned("no commit at that revision")

        def rows(cursor: DbApiCursor) -> list[tuple[str, bytes]]:
            try:
                cursor.execute(
                    f"SELECT schema_version FROM {CONFIG_META_TABLE} AS OF %s "
                    "WHERE singleton_id = 1",
                    (revision,),
                )
                meta = cursor.fetchone()
                cursor.execute(
                    f"SELECT path, content FROM {CONFIG_DOCUMENTS_TABLE} AS OF %s ORDER BY ordinal",
                    (revision,),
                )
                found = cursor.fetchall()
            except Exception:  # noqa: BLE001 - an unknown revision is "not provisioned"
                raise NotProvisioned("no commit at that revision") from None
            if meta is None:
                return []
            return [
                (CONFIG_SCHEMA_VERSION, str(int(meta[0])).encode("ascii")),
                *((_DOCUMENT_PREFIX + _text(path), _raw(content)) for path, content in found),
            ]

        return self._store._read(Role.READER, rows)

    @contextmanager
    def _consistent(self) -> Iterator[ConsistencyToken]:
        with self._store._session(Role.READER) as (connection, cursor):
            try:
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                head = self._store._head(cursor)
                cursor.execute(
                    f"SELECT backend_identity, generation FROM {CONFIG_META_TABLE} "
                    "WHERE singleton_id = 1"
                )
                found = cursor.fetchone()
            except StorageError:
                _rollback(connection)
                raise
            except Exception:  # noqa: BLE001
                _rollback(connection)
                raise StorageUnavailable("HQ config consistent read unavailable") from None
            if found is None:
                _rollback(connection)
                raise NotProvisioned("the HQ config schema is not seeded")
            try:
                yield ConsistencyToken(_text(found[0]), _text(found[1]), head)
            finally:
                _rollback(connection)

    def consistent_read(self) -> Any:
        return self._consistent()

    # -- publish -----------------------------------------------------------------------------

    def publish(
        self,
        changes: ChangeSet,
        *,
        expected_head: Revision | Absent,
        operation: OperationId,
        author: str,
        message: str,
    ) -> Outcome:
        """One config publication: the original-parent head check, a clean working tree, the
        exact table allowlist, the sequence CAS on the meta singleton, the documents, the
        witness row, ``DOLT_ADD`` of exactly :data:`CONFIG_TABLES`, then ``DOLT_COMMIT`` (the
        commit point). Reusing a witnessed publication id is ``InvalidRequest``."""
        operation = check_operation(operation)
        version, documents = _check_config_changes(changes)
        commit_author = _author(author)
        subject = (message if isinstance(message, str) and message.strip() else "") or (
            f"HQ config publication {operation}"
        )
        digest = config_documents_digest(documents)
        result: list[Revision] = []

        def body_fn(cursor: DbApiCursor) -> tuple[Outcome, Callable[..., Any]]:
            head = self._store._head(cursor)
            if isinstance(expected_head, Absent) or expected_head != head:
                return Conflict(Versioned("head", head, {})), _commit_transaction
            # A trusted publisher has branch-wide staging power: never absorb unrelated edits.
            cursor.execute("SELECT * FROM dolt_status")
            if cursor.fetchall():
                raise IntegrityViolation("HQ config working tree is dirty")
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_type='BASE TABLE' "
                "AND table_name NOT LIKE 'dolt\\_%' ORDER BY table_name"
            )
            if {_text(row[0]) for row in cursor.fetchall()} != set(CONFIG_TABLES):
                raise NotProvisioned("HQ config schema table allowlist changed")
            cursor.execute(
                f"SELECT 1 FROM {CONFIG_PUBLICATIONS_TABLE} WHERE publication_id = %s",
                (operation,),
            )
            if cursor.fetchone() is not None:
                raise InvalidRequest("operation id already witnesses a publication; mint a new one")
            cursor.execute(
                f"SELECT publication_sequence, generation FROM {CONFIG_META_TABLE} "
                "WHERE singleton_id = 1"
            )
            row = cursor.fetchone()
            if row is None:
                raise NotProvisioned("the HQ config schema is not seeded")
            sequence, generation = int(row[0]), _text(row[1])
            cursor.execute(
                f"UPDATE {CONFIG_META_TABLE} SET schema_version=%s,"
                "publication_sequence=%s,publication_id=%s,"
                "documents_sha256=%s,document_count=%s "
                "WHERE singleton_id=1 AND publication_sequence=%s",
                (version, sequence + 1, operation, digest, len(documents), sequence),
            )
            if cursor.rowcount != 1:
                raise _ConflictMarker()
            cursor.execute(f"DELETE FROM {CONFIG_DOCUMENTS_TABLE}")
            for ordinal, (path, content) in enumerate(documents):
                cursor.execute(
                    f"INSERT INTO {CONFIG_DOCUMENTS_TABLE} "
                    "(path,ordinal,kind,content,content_sha256) VALUES (%s,%s,%s,%s,%s)",
                    (
                        path,
                        ordinal,
                        config_document_kind(path),
                        content,
                        hashlib.sha256(content).hexdigest(),
                    ),
                )
            cursor.execute(
                f"INSERT INTO {CONFIG_PUBLICATIONS_TABLE} VALUES (%s,%s,%s,%s,%s,%s)",
                (operation, sequence + 1, generation, str(expected_head), digest, len(documents)),
            )
            cursor.execute(
                "CALL DOLT_ADD(" + ",".join(f"'{table}'" for table in CONFIG_TABLES) + ")"
            )

            def commit(_connection: DbApiConnection, cur: DbApiCursor) -> None:
                # DOLT_COMMIT commits the SQL transaction too: this IS the commit point.
                cur.execute("CALL DOLT_COMMIT('-m',%s,'--author',%s)", (subject, commit_author))
                committed = cur.fetchone()
                if not committed or not isinstance(committed[0], str) or not committed[0]:
                    raise StorageError("DOLT_COMMIT returned no hash")
                result.append(Revision(committed[0]))

            return Applied(Revision(""), operation), commit

        def on_conflict() -> Outcome:
            try:
                return Conflict(Versioned("head", self.head(), {}))
            except StorageError:
                return Conflict(None)

        out = self._store._mutate(
            Role.PUBLISHER,
            body_fn,
            expected=expected_head,
            operation=operation,
            on_conflict=on_conflict,
        )
        if isinstance(out, Applied):
            return Applied(result[0], operation)
        return out

    def lookup_publication(
        self, operation: OperationId, *, expected_head: Revision | Absent
    ) -> LookupOutcome:
        operation = check_operation(operation)
        if isinstance(expected_head, Absent):
            return NotConfirmed(operation)  # every config publication has a parent commit

        def find(cursor: DbApiCursor) -> tuple | None:
            head = self._store._head(cursor)
            cursor.execute(
                f"SELECT expected_parent_revision FROM {CONFIG_PUBLICATIONS_TABLE} AS OF %s "
                "WHERE publication_id = %s",
                (head, operation),
            )
            rows = cursor.fetchall()
            if len(rows) != 1 or _text(rows[0][0]) != str(expected_head):
                return None
            cursor.execute(
                "SELECT HAS_ANCESTOR(%s,%s), UTC_TIMESTAMP(6)", (head, str(expected_head))
            )
            found = cursor.fetchone()
            if found is None or found[0] != 1:
                return None
            return head, found[1]

        found = self._store._read(Role.READER, find)
        if found is None:
            return NotConfirmed(operation)
        return Confirmed(operation, Revision(found[0]), aware(found[1]))

    # -- sync --------------------------------------------------------------------------------

    def fetch(self) -> SyncReport:
        return self._store.fetch()

    def push(self) -> Outcome:
        return self._store.push()

    # -- CapabilityHost ----------------------------------------------------------------------

    def capability(self, kind: type) -> None:
        """No capability: the config history offers the generic protocol only."""
        return None


def _raw(value: object) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)  # type: ignore[arg-type]
