"""``DoltHqSessions``: one HQ SQL transaction shared with code that still speaks SQL (bh-sqfvq).

**Transitional and Dolt-only.** The director's placement survey reads the operator-signed
authority (``hq_sql_runtime``), every placement row, the failover policy (``failover_policy``)
and frame-session staleness (``failover_observer``) in ONE read-only transaction, and a placement
that sets ``failover_after`` writes the policy rows and the placement CAS in ONE transaction. The
placement half is on the port now; the other three are not migrated yet (``bh-xnn5p``). Until they
are, a session hands both halves the same transaction: port-shaped reads and the placement CAS
through :class:`DoltHqTransaction`, and the raw DB-API ``cursor`` for the not-yet-migrated SQL.

A generic consumer never needs this capability, and it is not on the port. Each migration bead
that moves one of the cursor consumers onto the port removes its use of ``cursor``; the
capability goes when the last one does.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from beadhive_hq_storage import (
    Absent,
    Applied,
    Body,
    Conflict,
    InvalidRequest,
    OperationId,
    Outcome,
    RecordKind,
    Revision,
    StorageError,
    StorageUnavailable,
    Unknown,
    Versioned,
)

from ._classify import is_conflict, unknown_reason
from ._hq import DbApiCursor, Role, _rollback

if TYPE_CHECKING:
    from ._hq import DoltHqStorage

__all__ = ["DoltHqSessions", "DoltHqTransaction", "SqlDoltHqSessions"]


class DoltHqTransaction:
    """One open HQ transaction: port-shaped reads and CAS, plus the raw ``cursor``."""

    def __init__(self, storage: DoltHqStorage, cursor: DbApiCursor, *, writable: bool) -> None:
        self._storage = storage
        self._cursor = cursor
        self._writable = writable

    @property
    def cursor(self) -> DbApiCursor:
        """The transaction's DB-API cursor, for the consumers not yet on the port."""
        return self._cursor

    def head(self) -> Revision:
        """``DOLT_HASHOF('HEAD')`` inside this transaction."""
        return self._storage._head(self._cursor)

    def read(self, kind: RecordKind, key: str) -> Versioned[Body] | None:
        kind = RecordKind(kind)
        row = self._storage._one(self._cursor, kind, key)
        return None if row is None else self._storage._versioned(row, kind)

    def read_many(
        self, kind: RecordKind, keys: Collection[str] | None = None
    ) -> Sequence[Versioned[Body]]:
        kind = RecordKind(kind)
        return self._storage._filter(kind, self._storage._all(self._cursor, kind), keys)

    def compare_and_set(
        self,
        kind: RecordKind,
        key: str,
        body: Body,
        *,
        expected: Revision | Absent,
        operation: OperationId,
    ) -> Outcome:
        """Issue the CAS inside the transaction. ``Applied`` here is provisional: the session's
        commit point decides the final outcome."""
        if not self._writable:
            raise InvalidRequest("a snapshot is read-only")
        write = self._storage._prepare(kind, key, body, expected, operation)
        return self._storage._apply(self._cursor, write)


@runtime_checkable
class DoltHqSessions(Protocol):
    """Transitional Dolt-only capability; see the module docstring."""

    def snapshot(self) -> AbstractContextManager[DoltHqTransaction]: ...

    def transact(
        self,
        body: Callable[[DoltHqTransaction], Outcome],
        *,
        operation: OperationId,
        expected: Revision | Absent,
        passthrough: tuple[type[BaseException], ...] = (),
        role: Role = Role.PLACEMENT_WRITER,
    ) -> Outcome: ...


class SqlDoltHqSessions:
    """:class:`DoltHqSessions` over a :class:`~beadhive_hq_storage_dolt.DoltHqStorage`."""

    def __init__(self, storage: DoltHqStorage) -> None:
        self._storage = storage

    @contextmanager
    def snapshot(self) -> Iterator[DoltHqTransaction]:
        """One read-only transaction (``READER`` role, principal-checked), always rolled back.
        Errors propagate unclassified: the caller owns what a failed read means."""
        with self._storage._session(Role.READER) as (connection, cursor):
            try:
                cursor.execute("START TRANSACTION")
                yield DoltHqTransaction(self._storage, cursor, writable=False)
            finally:
                _rollback(connection)

    def transact(
        self,
        body: Callable[[DoltHqTransaction], Outcome],
        *,
        operation: OperationId,
        expected: Revision | Absent,
        passthrough: tuple[type[BaseException], ...] = (),
        role: Role = Role.PLACEMENT_WRITER,
    ) -> Outcome:
        """Run ``body`` in one transaction and commit it only when it returns ``Applied``.

        Unlike a port mutation, a non-``Applied`` outcome rolls back instead of committing, so a
        co-written cursor change never lands without the CAS. ``passthrough`` exceptions (the
        caller's own refusals) roll back and propagate; a serialization failure anywhere is a
        ``Conflict``; any other error before the commit is ``StorageUnavailable``, and at the
        commit ``Unknown`` with the original expectation."""
        storage = self._storage
        with storage._session(role) as (connection, cursor):
            try:
                cursor.execute("START TRANSACTION")
                outcome = body(DoltHqTransaction(storage, cursor, writable=True))
            except passthrough:
                _rollback(connection)
                raise
            except StorageError:
                _rollback(connection)
                raise
            except Exception as exc:  # noqa: BLE001 - classified below; driver text never surfaces
                _rollback(connection)
                if is_conflict(exc):
                    return Conflict(None)
                raise StorageUnavailable("HQ transaction unavailable; nothing applied") from None
            if not isinstance(outcome, Applied):
                _rollback(connection)
                return outcome
            try:
                connection.commit()
            except Exception as exc:  # noqa: BLE001 - the acknowledgment's fate is unknown
                _rollback(connection)
                if is_conflict(exc):
                    return Conflict(None)
                return Unknown(
                    operation=operation,
                    expected=expected,
                    reason=unknown_reason(exc, deadline=storage._deadline),
                )
            return outcome
