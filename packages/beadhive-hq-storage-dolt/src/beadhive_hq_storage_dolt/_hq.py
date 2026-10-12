"""The Dolt HQ adapter: ``VersionedRecords``, ``AppendLog``, ``Liveness``, ``CommittedHistory``
and ``CapabilityHost`` over one Dolt database, reached through a root-injected DB-API factory.

Transport stays in root (hq-storage-port-adr section 7): ``connect(role)`` returns a fresh
format-paramstyle DB-API connection (PyMySQL under root's bounded-deadline, no-reconnect,
verified-TLS ``hq_sql_transport.connect``) with autocommit off. Every operation owns exactly one
connection and closes it; nothing is retried. Staleness is the server's: ``now()`` reads
``UTC_TIMESTAMP(6)``, triggers stamp every renewal and append, and ``ages`` / ``prune`` compute
``TIMESTAMPDIFF(MICROSECOND, <stamp>, UTC_TIMESTAMP(6))`` inside one statement (never
``UTC_TIMESTAMP(6) - INTERVAL n SECOND``: Dolt 2.3.5 drops its fractional seconds).

Every mutation reaches exactly one **commit point** (``COMMIT``, ``CALL DOLT_COMMIT`` or ``CALL
DOLT_PUSH``), even when its outcome is already a ``Conflict``, so an acknowledgment lost there is
always ``Unknown`` with the ORIGINAL expectation, exactly like the port's fake. See
:mod:`._classify` for how an error is mapped by where it happened.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, TypeVar

from beadhive_hq_storage import (
    ABSENT,
    UPDATE_ONLY_KINDS,
    Absent,
    Applied,
    Body,
    ChangeSet,
    Confirmed,
    Conflict,
    ConsistencyToken,
    IntegrityViolation,
    InvalidRequest,
    LivenessKind,
    LookupOutcome,
    NotConfirmed,
    NotProvisioned,
    OperationId,
    Outcome,
    PlacementView,
    PruneReport,
    RecordKind,
    Revision,
    StorageError,
    StorageUnavailable,
    StreamKind,
    SyncReport,
    Unknown,
    Versioned,
)

from . import _schema as schema
from ._classify import (
    DUPLICATE_KEY,
    DeadlinePredicate,
    error_code,
    is_conflict,
    is_deadline,
    unknown_reason,
)
from ._values import (
    DEFAULT_MAX_BODY_BYTES,
    aware,
    canonical,
    check_key,
    check_operation,
    decode_body,
    fresh_revision,
)

__all__ = [
    "PLACEMENT_DOMAIN",
    "ConnectionFactory",
    "DbApiConnection",
    "DbApiCursor",
    "DoltHqStorage",
    "DoltPlacementStore",
    "Role",
    "placement_witness",
]

C = TypeVar("C")

PLACEMENT_DOMAIN: Final = "beadhive/sql-placement/v1"
"""Root ``hq_sql_placement.PLACEMENT_DOMAIN``: the ``request_sha256`` of a director row."""


class Role(StrEnum):
    """Which credential an operation needs. Root maps each to an ``HqSqlConnection`` binding."""

    OPERATOR = "operator"  # provisioning, non-placement records, append, prune
    PLACEMENT_WRITER = "placement_writer"  # placement CAS (UPDATE-only)
    PUBLISHER = "publisher"  # publish, fetch, push
    FRAME = "frame"  # liveness renewal
    READER = "reader"  # every read, lookup and age


class DbApiCursor(Protocol):
    rowcount: int

    def execute(self, operation: str, parameters: Sequence[Any] | None = None) -> Any: ...

    def fetchone(self) -> Sequence[Any] | None: ...

    def fetchall(self) -> Sequence[Sequence[Any]]: ...

    def close(self) -> Any: ...


class DbApiConnection(Protocol):
    """A PEP 249 connection with the ``format`` paramstyle (``%s``), autocommit off."""

    def cursor(self) -> DbApiCursor: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...

    def close(self) -> Any: ...


ConnectionFactory = Callable[[Role], DbApiConnection]
_CommitPoint = Callable[[DbApiConnection, DbApiCursor], Any]
"""Root-injected: a fresh connection for ``role``. Raising means nothing was sent."""


def placement_witness(prefix: str, revision: str, body: bytes) -> str:
    """Root ``hq_sql_placement.placement_witness``: the ``request_sha256`` that marks a
    director-written placement row (a receiver row carries a signed-request digest instead)."""
    record = {
        "domain": PLACEMENT_DOMAIN,
        "prefix": prefix,
        "revision": revision,
        "lease_sha256": hashlib.sha256(body).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class DoltHqStorage:
    """The Dolt HQ store. ``remote`` / ``branch`` name the publication remote; ``deadline``
    recognises the transport's operation-deadline error (``Unknown(reason="deadline")``)."""

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        remote: str = "origin",
        branch: str = "main",
        backend_identity: str | None = None,
        deadline: DeadlinePredicate | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self._connect = connect
        self._remote = check_key(remote, "remote")
        self._branch = check_key(branch, "branch")
        self._identity = backend_identity
        self._deadline = deadline
        self._cap = max_body_bytes
        self._capabilities: dict[type, object] = {}

    # -- connections -------------------------------------------------------------------------

    @contextmanager
    def _session(self, role: Role) -> Iterator[tuple[DbApiConnection, DbApiCursor]]:
        try:
            connection = self._connect(role)
        except Exception:  # noqa: BLE001 - the factory raising means nothing was sent
            raise StorageUnavailable(f"HQ {role.value} connection unavailable") from None
        try:
            cursor = connection.cursor()
            try:
                yield connection, cursor
            finally:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001, S110 - the connection is closed next anyway
                    pass
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001, S110 - nothing to recover from a failed close
                pass

    def _read(self, role: Role, fn: Callable[[DbApiCursor], C]) -> C:
        """One read-only transaction; any driver error is ``StorageUnavailable``."""
        with self._session(role) as (connection, cursor):
            try:
                cursor.execute("START TRANSACTION")
                result = fn(cursor)
                connection.rollback()
                return result
            except StorageError:
                _rollback(connection)
                raise
            except Exception:  # noqa: BLE001 - classified: a failed read applied nothing
                _rollback(connection)
                raise StorageUnavailable("HQ read unavailable") from None

    def _mutate(
        self,
        role: Role,
        body: Callable[[DbApiCursor], tuple[Outcome, _CommitPoint]],
        *,
        expected: Revision | Absent,
        operation: OperationId,
        on_conflict: Callable[[], Outcome],
    ) -> Outcome:
        """Run ``body`` in one transaction, then its commit point; classify by phase.

        ``body`` returns the outcome to report if the commit point succeeds and the commit-point
        callable. A serialization failure anywhere is ``on_conflict()``; any other error before
        the commit point is ``StorageUnavailable``; at or after it, ``Unknown``."""
        with self._session(role) as (connection, cursor):
            try:
                cursor.execute("START TRANSACTION")
                outcome, commit = body(cursor)
            except _ConflictMarker:
                _rollback(connection)
                return on_conflict()
            except StorageError:
                _rollback(connection)
                raise
            except Exception as exc:  # noqa: BLE001 - classified below; driver text never surfaces
                _rollback(connection)
                if is_conflict(exc):
                    return on_conflict()
                raise StorageUnavailable("HQ mutation unavailable; nothing applied") from None
            try:
                commit(connection, cursor)
            except Exception as exc:  # noqa: BLE001 - the acknowledgment's fate is unknown
                _rollback(connection)
                if is_conflict(exc):
                    return on_conflict()
                return Unknown(
                    operation=operation,
                    expected=expected,
                    reason=unknown_reason(exc, deadline=self._deadline),
                )
            return outcome

    # -- operator provisioning (test and bootstrap surface; not on the port) ------------------

    def provision(self, kind: RecordKind, key: str, body: Body) -> Revision:
        """Operator provisioning: seed a record. The only way an UPDATE-only kind is created."""
        kind = RecordKind(kind)
        check_key(key)
        raw = canonical(body, cap=self._cap)
        revision = fresh_revision({"kind": kind.value, "key": key, "seed": True})
        witness = str(uuid.uuid4())

        def write(cursor: DbApiCursor) -> None:
            if kind is RecordKind.PLACEMENT:
                cursor.execute(
                    f"INSERT INTO {schema.PLACEMENT_TABLE} "
                    "(prefix, revision, lease_json, request_id, request_sha256) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (key, revision, raw, witness, placement_witness(key, revision, raw)),
                )
            else:
                cursor.execute(
                    f"INSERT INTO {schema.RECORDS_TABLE} "
                    "(kind, record_key, revision, body, operation_id, recorded_at) "
                    "VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(6))",
                    (kind.value, key, revision, raw, witness),
                )

        with self._session(Role.OPERATOR) as (connection, cursor):
            try:
                cursor.execute("START TRANSACTION")
                write(cursor)
                connection.commit()
            except Exception:  # noqa: BLE001
                _rollback(connection)
                raise StorageUnavailable(f"provisioning {kind.value}/{key} failed") from None
        return revision

    def placements(self) -> DoltPlacementStore:
        """The ``PlacementStore`` view over this store's ``PLACEMENT`` records."""
        return DoltPlacementStore(self)

    # -- CapabilityHost ----------------------------------------------------------------------

    def register_capability(self, kind: type[C], implementation: C) -> None:
        self._capabilities[kind] = implementation

    def capability(self, kind: type[C]) -> C | None:
        """The Dolt-only capabilities (``DoltHistory``, ``DoltMerge``, ``DoltAccounts``) over
        this store's connection factory, plus any registered one; ``None`` otherwise."""
        if kind not in self._capabilities:
            from ._capabilities import default_hq_capability

            found = default_hq_capability(kind, self._connect)
            if found is None:
                return None
            self._capabilities[kind] = found
        return self._capabilities[kind]  # type: ignore[return-value]

    # -- VersionedRecords --------------------------------------------------------------------

    @staticmethod
    def _select_record(kind: RecordKind) -> str:
        if kind is RecordKind.PLACEMENT:
            return (
                f"SELECT prefix, revision, lease_json, request_id, UTC_TIMESTAMP(6) "
                f"FROM {schema.PLACEMENT_TABLE}"
            )
        return (
            "SELECT record_key, revision, body, operation_id, recorded_at "
            f"FROM {schema.RECORDS_TABLE} WHERE kind = '{kind.value}'"
        )

    def _one(self, cursor: DbApiCursor, kind: RecordKind, key: str) -> tuple | None:
        where = " WHERE prefix = %s" if kind is RecordKind.PLACEMENT else " AND record_key = %s"
        cursor.execute(self._select_record(kind) + where, (key,))
        row = cursor.fetchone()
        return None if row is None else tuple(row)

    @staticmethod
    def _versioned(row: Sequence[Any]) -> Versioned[Body]:
        return Versioned(_text(row[0]), Revision(_text(row[1])), decode_body(row[2]))

    def read(self, kind: RecordKind, key: str) -> Versioned[Body] | None:
        kind = RecordKind(kind)
        check_key(key)
        row = self._read(Role.READER, lambda cursor: self._one(cursor, kind, key))
        return None if row is None else self._versioned(row)

    def read_many(
        self, kind: RecordKind, keys: Collection[str] | None = None
    ) -> Sequence[Versioned[Body]]:
        kind = RecordKind(kind)
        wanted = None if keys is None else {check_key(k) for k in keys}

        def rows(cursor: DbApiCursor) -> list[tuple]:
            cursor.execute(self._select_record(kind))
            return [tuple(r) for r in cursor.fetchall()]

        found = [self._versioned(r) for r in self._read(Role.READER, rows)]
        return sorted((v for v in found if wanted is None or v.key in wanted), key=lambda v: v.key)

    def compare_and_set(
        self,
        kind: RecordKind,
        key: str,
        body: Body,
        *,
        expected: Revision | Absent,
        operation: OperationId,
    ) -> Outcome:
        kind = RecordKind(kind)
        check_key(key)
        operation = check_operation(operation)
        raw = canonical(body, cap=self._cap)
        if isinstance(expected, Absent) and kind in UPDATE_ONLY_KINDS:
            raise NotProvisioned(f"{kind.value} is UPDATE-only: operator provisioning seeds it")
        if not isinstance(expected, Absent) and not isinstance(expected, str):
            raise InvalidRequest("expected must be a Revision or ABSENT")
        revision = fresh_revision({"kind": kind.value, "key": key, "body": decode_body(raw)})

        def body_fn(cursor: DbApiCursor) -> tuple[Outcome, Callable[..., Any]]:
            if kind is RecordKind.PLACEMENT:
                cursor.execute(
                    f"UPDATE {schema.PLACEMENT_TABLE} SET revision = %s, lease_json = %s, "
                    "request_id = %s, request_sha256 = %s WHERE prefix = %s AND revision = %s",
                    (
                        revision,
                        raw,
                        operation,
                        placement_witness(key, revision, raw),
                        key,
                        expected,
                    ),
                )
            elif isinstance(expected, Absent):
                cursor.execute(
                    f"INSERT INTO {schema.RECORDS_TABLE} "
                    "(kind, record_key, revision, body, operation_id, recorded_at) "
                    "SELECT %s, %s, %s, %s, %s, UTC_TIMESTAMP(6) FROM DUAL WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {schema.RECORDS_TABLE} WHERE kind = %s AND record_key = %s)",
                    (kind.value, key, revision, raw, operation, kind.value, key),
                )
            else:
                cursor.execute(
                    f"UPDATE {schema.RECORDS_TABLE} SET revision = %s, body = %s, "
                    "operation_id = %s WHERE kind = %s AND record_key = %s AND revision = %s",
                    (revision, raw, operation, kind.value, key, expected),
                )
            if cursor.rowcount == 1:
                outcome: Outcome = Applied(revision, operation)
            else:
                row = self._one(cursor, kind, key)
                outcome = Conflict(None if row is None else self._versioned(row))
            return outcome, _commit_transaction

        return self._mutate(
            Role.PLACEMENT_WRITER if kind is RecordKind.PLACEMENT else Role.OPERATOR,
            body_fn,
            expected=expected,
            operation=operation,
            on_conflict=lambda: Conflict(self.read(kind, key)),
        )

    def lookup(self, kind: RecordKind, key: str, operation: OperationId) -> LookupOutcome:
        operation = check_operation(operation)
        kind = RecordKind(kind)
        check_key(key)
        row = self._read(Role.READER, lambda cursor: self._one(cursor, kind, key))
        if row is not None and _text(row[3]) == operation:
            # PLACEMENT has no stamp column (receiver-compatible row): its recorded_at is the
            # server clock at lookup, an upper bound on the write (bh-sqfvq may add one).
            return Confirmed(operation, Revision(_text(row[1])), aware(row[4]))
        return NotConfirmed(operation)

    # -- AppendLog ---------------------------------------------------------------------------

    _APPEND_ROW = (
        f"SELECT entry_key, revision, body, operation_id, recorded_at FROM {schema.APPEND_TABLE}"
    )

    def _entry(self, cursor: DbApiCursor, stream: StreamKind, key: str) -> tuple | None:
        cursor.execute(
            self._APPEND_ROW + " WHERE stream = %s AND entry_key = %s", (stream.value, key)
        )
        row = cursor.fetchone()
        return None if row is None else tuple(row)

    def append(
        self, stream: StreamKind, key: str, body: Body, *, operation: OperationId
    ) -> Outcome:
        stream = StreamKind(stream)
        check_key(key)
        operation = check_operation(operation)
        raw = canonical(body, cap=self._cap)
        mine = decode_body(raw)
        revision = fresh_revision({"stream": stream.value, "key": key, "body": mine})

        def body_fn(cursor: DbApiCursor) -> tuple[Outcome, Callable[..., Any]]:
            prior = self._entry(cursor, stream, key)
            if prior is not None:
                same = _text(prior[3]) == operation and decode_body(prior[2]) == mine
                versioned = self._versioned(prior)
                outcome: Outcome = (
                    Applied(versioned.revision, operation) if same else Conflict(versioned)
                )
                return outcome, _commit_transaction
            try:
                cursor.execute(
                    f"INSERT INTO {schema.APPEND_TABLE} "
                    "(stream, entry_key, revision, body, operation_id, recorded_at) "
                    "VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(6))",
                    (stream.value, key, revision, raw, operation),
                )
            except Exception as exc:  # noqa: BLE001 - a racing insert of the same key
                if error_code(exc) != DUPLICATE_KEY:
                    raise
                raise _conflict_marker() from None
            return Applied(revision, operation), _commit_transaction

        def on_conflict() -> Outcome:
            current = self._read(Role.READER, lambda cursor: self._entry(cursor, stream, key))
            return Conflict(None if current is None else self._versioned(current))

        return self._mutate(
            Role.OPERATOR,
            body_fn,
            expected=ABSENT,
            operation=operation,
            on_conflict=on_conflict,
        )

    def scan(
        self,
        stream: StreamKind,
        *,
        key: str | None = None,
        after: Revision | None = None,
        limit: int,
    ) -> Sequence[Versioned[Body]]:
        stream = StreamKind(stream)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise InvalidRequest("limit must be a positive integer")
        if key is not None:
            check_key(key)

        def rows(cursor: DbApiCursor) -> list[tuple]:
            floor = 0
            if after is not None:
                cursor.execute(
                    f"SELECT seq FROM {schema.APPEND_TABLE} WHERE stream = %s AND revision = %s",
                    (stream.value, str(after)),
                )
                found = cursor.fetchone()
                if found is None:
                    raise InvalidRequest("after is not a revision this stream issued")
                floor = int(found[0])
            sql = self._APPEND_ROW + " WHERE stream = %s AND seq > %s"
            params: list[Any] = [stream.value, floor]
            if key is not None:
                sql += " AND entry_key = %s"
                params.append(key)
            cursor.execute(sql + " ORDER BY seq LIMIT %s", (*params, limit))
            return [tuple(r) for r in cursor.fetchall()]

        return [self._versioned(r) for r in self._read(Role.READER, rows)]

    def prune(
        self, stream: StreamKind, *, older_than: timedelta, operation: OperationId
    ) -> PruneReport:
        stream = StreamKind(stream)
        operation = check_operation(operation)
        if not isinstance(older_than, timedelta) or older_than < timedelta(0):
            raise InvalidRequest("older_than must be a non-negative timedelta")
        window_us = _microseconds(older_than)
        counts: dict[str, int] = {}

        def body_fn(cursor: DbApiCursor) -> tuple[Outcome, Callable[..., Any]]:
            cursor.execute(
                f"DELETE FROM {schema.APPEND_TABLE} WHERE stream = %s AND "
                "TIMESTAMPDIFF(MICROSECOND, recorded_at, UTC_TIMESTAMP(6)) > %s",
                (stream.value, window_us),
            )
            counts["removed"] = max(0, int(cursor.rowcount))
            cursor.execute(
                f"SELECT COUNT(*) FROM {schema.APPEND_TABLE} WHERE stream = %s", (stream.value,)
            )
            counts["retained"] = int(cursor.fetchone()[0])  # type: ignore[index]
            return Applied(Revision(""), operation), _commit_transaction

        out = self._mutate(
            Role.OPERATOR,
            body_fn,
            expected=ABSENT,
            operation=operation,
            on_conflict=lambda: Conflict(None),
        )
        if isinstance(out, Unknown):
            raise StorageError(f"prune of {stream.value}: commit outcome unknown ({out.reason})")
        if isinstance(out, Conflict):
            raise StorageUnavailable(f"prune of {stream.value} lost a race; nothing applied")
        return PruneReport(stream, counts["removed"], counts["retained"], operation)

    def lookup_append(self, stream: StreamKind, key: str, operation: OperationId) -> LookupOutcome:
        operation = check_operation(operation)
        stream = StreamKind(stream)
        check_key(key)
        row = self._read(Role.READER, lambda cursor: self._entry(cursor, stream, key))
        if row is not None and _text(row[3]) == operation:
            return Confirmed(operation, Revision(_text(row[1])), aware(row[4]))
        return NotConfirmed(operation)

    # -- Liveness ----------------------------------------------------------------------------

    def now(self) -> datetime:
        def utc(cursor: DbApiCursor) -> datetime:
            cursor.execute("SELECT UTC_TIMESTAMP(6)")
            return aware(cursor.fetchone()[0])  # type: ignore[index]

        return self._read(Role.READER, utc)

    def renew(self, kind: LivenessKind, key: str, body: Body | None = None) -> datetime:
        kind = LivenessKind(kind)
        check_key(key)
        raw = None if body is None else canonical(body, cap=self._cap)
        stamp: list[datetime] = []

        def body_fn(cursor: DbApiCursor) -> tuple[Outcome, Callable[..., Any]]:
            cursor.execute(
                f"SELECT 1 FROM {schema.LIVENESS_TABLE} WHERE kind = %s AND live_key = %s",
                (kind.value, key),
            )
            if cursor.fetchone() is None:
                # The trigger overwrites renewed_at with UTC_TIMESTAMP(6) whatever is sent.
                cursor.execute(
                    f"INSERT INTO {schema.LIVENESS_TABLE} (kind, live_key, body, renewed_at) "
                    "VALUES (%s, %s, %s, UTC_TIMESTAMP(6))",
                    (kind.value, key, raw),
                )
            else:
                cursor.execute(
                    f"UPDATE {schema.LIVENESS_TABLE} SET body = %s "
                    "WHERE kind = %s AND live_key = %s",
                    (raw, kind.value, key),
                )
            cursor.execute(
                f"SELECT renewed_at FROM {schema.LIVENESS_TABLE} WHERE kind = %s AND live_key = %s",
                (kind.value, key),
            )
            stamp.append(aware(cursor.fetchone()[0]))  # type: ignore[index]
            return Applied(Revision(""), OperationId("")), _commit_transaction

        out = self._mutate(
            Role.FRAME,
            body_fn,
            expected=ABSENT,
            operation=OperationId(""),
            on_conflict=lambda: Conflict(None),
        )
        if isinstance(out, Unknown):
            raise StorageError(f"renewal of {kind.value}/{key}: commit outcome unknown")
        if isinstance(out, Conflict):
            raise StorageUnavailable(f"renewal of {kind.value}/{key} lost a race; not applied")
        return stamp[0]

    def ages(
        self, kind: LivenessKind, keys: Collection[str] | None = None
    ) -> Mapping[str, timedelta]:
        kind = LivenessKind(kind)
        wanted = None if keys is None else {check_key(k) for k in keys}
        if wanted is not None and not wanted:
            return {}

        def rows(cursor: DbApiCursor) -> list[tuple]:
            cursor.execute(
                "SELECT live_key, TIMESTAMPDIFF(MICROSECOND, renewed_at, UTC_TIMESTAMP(6)) "
                f"FROM {schema.LIVENESS_TABLE} WHERE kind = %s",
                (kind.value,),
            )
            return [tuple(r) for r in cursor.fetchall()]

        found = {
            _text(key): timedelta(microseconds=max(0, int(age)))
            for key, age in self._read(Role.READER, rows)
        }
        return {k: found[k] for k in sorted(found) if wanted is None or k in wanted}

    def stamped_at(self, kind: LivenessKind, key: str) -> datetime | None:
        kind = LivenessKind(kind)
        check_key(key)

        def row(cursor: DbApiCursor) -> tuple | None:
            cursor.execute(
                f"SELECT renewed_at FROM {schema.LIVENESS_TABLE} WHERE kind = %s AND live_key = %s",
                (kind.value, key),
            )
            found = cursor.fetchone()
            return None if found is None else tuple(found)

        found = self._read(Role.READER, row)
        return None if found is None else aware(found[0])

    # -- CommittedHistory --------------------------------------------------------------------

    @staticmethod
    def _head(cursor: DbApiCursor) -> Revision:
        cursor.execute("SELECT DOLT_HASHOF('HEAD')")
        row = cursor.fetchone()
        if not row or not row[0]:
            raise NotProvisioned("the HQ store has no head commit")
        return Revision(_text(row[0]))

    def head(self) -> Revision:
        return self._read(Role.READER, self._head)

    def read_at(self, revision: Revision) -> Sequence[tuple[str, bytes]]:
        if not isinstance(revision, str) or not revision.isalnum():
            raise NotProvisioned("no commit at that revision")

        def rows(cursor: DbApiCursor) -> list[tuple[str, bytes]]:
            try:
                cursor.execute(
                    f"SELECT path, content FROM {schema.DOCUMENTS_TABLE} AS OF %s", (revision,)
                )
            except Exception:  # noqa: BLE001 - an unknown revision is "not provisioned"
                raise NotProvisioned("no commit at that revision") from None
            return [(_text(p), _bytes(c)) for p, c in cursor.fetchall()]

        return sorted(self._read(Role.READER, rows))

    @contextmanager
    def _consistent(self) -> Iterator[ConsistencyToken]:
        with self._session(Role.READER) as (connection, cursor):
            try:
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                head = self._head(cursor)
                cursor.execute(
                    f"SELECT DATABASE(), generation FROM {schema.PUBLICATION_META_TABLE} "
                    "WHERE singleton_id = 1"
                )
                found = cursor.fetchone()
            except StorageError:
                _rollback(connection)
                raise
            except Exception:  # noqa: BLE001
                _rollback(connection)
                raise StorageUnavailable("HQ consistent read unavailable") from None
            if found is None:
                _rollback(connection)
                raise NotProvisioned("the HQ publication schema is not provisioned")
            identity = self._identity or f"dolt:{_text(found[0])}"
            try:
                yield ConsistencyToken(identity, _text(found[1]), head)
            finally:
                _rollback(connection)

    def consistent_read(self) -> Any:
        return self._consistent()

    def publish(
        self,
        changes: ChangeSet,
        *,
        expected_head: Revision | Absent,
        operation: OperationId,
        author: str,
        message: str,
    ) -> Outcome:
        operation = check_operation(operation)
        documents = _check_changes(changes)
        commit_author = _author(author)
        subject = (message if isinstance(message, str) and message.strip() else "") or (
            f"HQ storage publication {operation}"
        )
        reused = self._read(
            Role.READER,
            lambda cursor: _exists(
                cursor,
                f"SELECT 1 FROM {schema.PUBLICATIONS_TABLE} WHERE publication_id = %s",
                (operation,),
            ),
        )
        if reused:
            raise InvalidRequest("operation id already witnesses a publication; mint a new one")
        result: list[Revision] = []

        def body_fn(cursor: DbApiCursor) -> tuple[Outcome, Callable[..., Any]]:
            head = self._head(cursor)
            if isinstance(expected_head, Absent) or expected_head != head:
                return Conflict(Versioned("head", head, {})), _commit_transaction
            cursor.execute(
                f"SELECT sequence FROM {schema.PUBLICATION_META_TABLE} WHERE singleton_id = 1"
            )
            row = cursor.fetchone()
            if row is None:
                raise NotProvisioned("the HQ publication schema is not provisioned")
            sequence = int(row[0])
            cursor.execute(
                f"UPDATE {schema.PUBLICATION_META_TABLE} SET sequence = %s "
                "WHERE singleton_id = 1 AND sequence = %s",
                (sequence + 1, sequence),
            )
            if cursor.rowcount != 1:
                raise _conflict_marker()
            for path, content in documents.items():
                cursor.execute(
                    f"REPLACE INTO {schema.DOCUMENTS_TABLE} (path, content) VALUES (%s, %s)",
                    (path, content),
                )
            cursor.execute(
                f"INSERT INTO {schema.PUBLICATIONS_TABLE} "
                "(publication_id, sequence, parent_revision, author, document_count) "
                "VALUES (%s, %s, %s, %s, %s)",
                (operation, sequence + 1, head, commit_author, len(documents)),
            )
            cursor.execute(
                "CALL DOLT_ADD(" + ", ".join(["%s"] * len(schema.COMMITTED_TABLES)) + ")",
                schema.COMMITTED_TABLES,
            )

            def commit(connection: DbApiConnection, cur: DbApiCursor) -> None:
                # DOLT_COMMIT commits the SQL transaction too: this IS the commit point.
                cur.execute("CALL DOLT_COMMIT('-m', %s, '--author', %s)", (subject, commit_author))
                committed = cur.fetchone()
                if not committed or not committed[0]:
                    raise StorageError("DOLT_COMMIT returned no hash")
                result.append(Revision(_text(committed[0])))

            return Applied(Revision(""), operation), commit

        def on_conflict() -> Outcome:
            return Conflict(Versioned("head", self.head(), {}))

        out = self._mutate(
            Role.PUBLISHER,
            body_fn,
            expected=expected_head,
            operation=operation,
            on_conflict=on_conflict,
        )
        if isinstance(out, Applied):
            committed = result[0]
            parent = self._read(Role.READER, lambda cursor: _first_parent(cursor, committed))
            if parent != expected_head:
                raise IntegrityViolation(
                    f"publication {operation} committed on {parent}, not on the expected head"
                )
            return Applied(committed, operation)
        return out

    def lookup_publication(
        self, operation: OperationId, *, expected_head: Revision | Absent
    ) -> LookupOutcome:
        operation = check_operation(operation)
        if isinstance(expected_head, Absent):
            return NotConfirmed(operation)  # every publication has a parent commit

        def find(cursor: DbApiCursor) -> tuple | None:
            cursor.execute(
                "SELECT h.commit_hash, h.commit_date FROM "
                f"dolt_history_{schema.PUBLICATIONS_TABLE} h "
                "JOIN dolt_commit_ancestors a ON a.commit_hash = h.commit_hash "
                "WHERE h.publication_id = %s AND h.parent_revision = %s "
                "AND a.parent_hash = %s AND a.parent_index = 0",
                (operation, str(expected_head), str(expected_head)),
            )
            row = cursor.fetchone()
            return None if row is None else tuple(row)

        found = self._read(Role.READER, find)
        if found is None:
            return NotConfirmed(operation)
        return Confirmed(operation, Revision(_text(found[0])), aware(found[1]))

    def _remote_head(self, cursor: DbApiCursor) -> Revision | None:
        cursor.execute(
            "SELECT hash FROM dolt_remote_branches WHERE name = %s",
            (f"remotes/{self._remote}/{self._branch}",),
        )
        row = cursor.fetchone()
        return None if row is None else Revision(_text(row[0]))

    def _fetch(self, cursor: DbApiCursor) -> None:
        try:
            cursor.execute("CALL DOLT_FETCH(%s)", (self._remote,))
        except Exception:  # noqa: BLE001 - an unreachable remote; nothing was sent
            raise StorageUnavailable(f"fetch from {self._remote} unavailable") from None

    def _counts(self, cursor: DbApiCursor, remote: Revision | None) -> tuple[int, int]:
        if remote is None:
            cursor.execute("SELECT COUNT(*) FROM dolt_log(%s)", (self._branch,))
            return int(cursor.fetchone()[0]), 0  # type: ignore[index]
        tracking = f"{self._remote}/{self._branch}"
        cursor.execute("SELECT COUNT(*) FROM dolt_log(%s, '--not', %s)", (self._branch, tracking))
        ahead = int(cursor.fetchone()[0])  # type: ignore[index]
        cursor.execute("SELECT COUNT(*) FROM dolt_log(%s, '--not', %s)", (tracking, self._branch))
        return ahead, int(cursor.fetchone()[0])  # type: ignore[index]

    def fetch(self) -> SyncReport:
        def report(cursor: DbApiCursor) -> SyncReport:
            self._fetch(cursor)
            ahead, behind = self._counts(cursor, self._remote_head(cursor))
            return SyncReport(self._head(cursor), ahead, behind, ahead > 0 and behind > 0)

        return self._read(Role.PUBLISHER, report)

    def push(self) -> Outcome:
        operation = OperationId(str(uuid.uuid4()))
        with self._session(Role.PUBLISHER) as (connection, cursor):
            try:
                self._fetch(cursor)
                prior = self._remote_head(cursor)
                local = self._head(cursor)
            except StorageError:
                raise
            except Exception:  # noqa: BLE001
                raise StorageUnavailable("push preparation unavailable; nothing sent") from None
            expected: Revision | Absent = ABSENT if prior is None else prior
            try:
                cursor.execute("CALL DOLT_PUSH(%s, %s)", (self._remote, self._branch))
                cursor.fetchall()
            except Exception as exc:  # noqa: BLE001 - classified: refusal, or unknown fate
                return self._push_failure(exc, operation, expected, local)
        return Applied(local, operation)

    def _push_failure(
        self,
        exc: BaseException,
        operation: OperationId,
        expected: Revision | Absent,
        local: Revision,
    ) -> Outcome:
        code = error_code(exc)
        if is_deadline(exc, self._deadline) or code in (1180, 2006, 2013, 2055):
            return Unknown(operation, expected, unknown_reason(exc, deadline=self._deadline))

        def after(cursor: DbApiCursor) -> tuple[Revision | None, bool]:
            self._fetch(cursor)
            remote = self._remote_head(cursor)
            if remote is None:
                return None, True
            cursor.execute("SELECT dolt_merge_base(%s, %s)", (local, str(remote)))
            base = cursor.fetchone()
            return remote, base is not None and _text(base[0]) == remote

        try:
            remote, fast_forward = self._read(Role.PUBLISHER, after)
        except StorageError:
            return Unknown(operation, expected, unknown_reason(exc, deadline=self._deadline))
        if not fast_forward and remote is not None:
            return Conflict(Versioned("remote", remote, {}))
        return Unknown(operation, expected, unknown_reason(exc, deadline=self._deadline))


class DoltPlacementStore:
    """``PlacementStore`` over ``RecordKind.PLACEMENT``. The body is ``{"frame", "epoch",
    "cause"}`` as on the fake; the receiver's ``{"authority", "lease"}`` carrier is also read
    (``lease.host_id`` / ``lease.epoch``). Encoding the carrier on write is ``bh-sqfvq``'s."""

    def __init__(self, storage: DoltHqStorage) -> None:
        self._storage = storage

    def read(self, prefix: str) -> PlacementView | None:
        record = self._storage.read(RecordKind.PLACEMENT, prefix)
        if record is None:
            return None
        body = record.body
        lease = body.get("lease")
        if isinstance(lease, Mapping):
            frame, epoch = lease.get("host_id"), lease.get("epoch")
        else:
            frame, epoch = body.get("frame"), body.get("epoch")
        if not isinstance(frame, str) or not isinstance(epoch, int) or isinstance(epoch, bool):
            raise IntegrityViolation(f"placement row {prefix} carries no frame and epoch")
        return PlacementView(frame=frame, epoch=epoch, token=record.revision)

    def compare_and_set(
        self,
        prefix: str,
        frame: str,
        epoch: int,
        *,
        expected: PlacementView,
        cause: str,
        operation: OperationId,
    ) -> Outcome:
        body = {"frame": frame, "epoch": int(epoch), "cause": cause}
        return self._storage.compare_and_set(
            RecordKind.PLACEMENT, prefix, body, expected=expected.token, operation=operation
        )


# --- helpers ----------------------------------------------------------------------------------


class _ConflictMarker(Exception):
    """Internal: a lost race detected inside a transaction body (rowcount 0 on a sequence CAS,
    a duplicate natural key)."""


def _conflict_marker() -> _ConflictMarker:
    return _ConflictMarker()


def _commit_transaction(connection: DbApiConnection, _cursor: DbApiCursor) -> None:
    connection.commit()


def _rollback(connection: DbApiConnection) -> None:
    try:
        connection.rollback()
    except Exception:  # noqa: BLE001, S110 - the transaction is abandoned either way
        pass


def _text(value: object) -> str:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).decode()
    return str(value)


def _bytes(value: object) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode()
    return bytes(value)  # type: ignore[arg-type]


def _microseconds(delta: timedelta) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _exists(cursor: DbApiCursor, sql: str, params: Sequence[Any]) -> bool:
    cursor.execute(sql, params)
    return cursor.fetchone() is not None


def _first_parent(cursor: DbApiCursor, commit: Revision) -> Revision | None:
    cursor.execute(
        "SELECT parent_hash FROM dolt_commit_ancestors WHERE commit_hash = %s AND parent_index = 0",
        (str(commit),),
    )
    row = cursor.fetchone()
    return None if row is None else Revision(_text(row[0]))


def _author(author: object) -> str:
    """A Dolt ``--author`` (``Name <email>``) from the port's free-text author."""
    if not isinstance(author, str) or not author.strip() or any(c in author for c in "\r\n"):
        raise InvalidRequest("author must be a non-empty single-line string")
    text = author.strip()
    if text.endswith(">") and " <" in text:
        return text
    return f"{text.replace('<', '').replace('>', '')} <hq-storage@localhost>"


def _check_changes(changes: object) -> dict[str, bytes]:
    """The fake's change-set rules: non-empty, an explicit table allowlist, every document
    ``<table>/<name>`` in it, raw bytes, no name twice."""
    if not isinstance(changes, ChangeSet) or not changes.documents:
        raise InvalidRequest("a publication needs at least one document")
    tables = changes.tables
    if not tables or not all(isinstance(t, str) and t for t in tables):
        raise InvalidRequest("a publication needs an explicit, non-empty table allowlist")
    out: dict[str, bytes] = {}
    for name, raw in changes.documents:
        check_key(name, "document name")
        table, sep, rest = name.partition("/")
        if not sep or not rest or table not in tables:
            raise InvalidRequest(f"document {name!r} is not '<table>/<name>' in the allowlist")
        if not isinstance(raw, bytes):
            raise InvalidRequest(f"document {name!r} must be raw bytes")
        if name in out:
            raise InvalidRequest(f"document {name!r} appears twice in one change set")
        out[name] = raw
    return out
