"""The port protocols (hq-storage-port-adr sections 2 and 3).

Small protocols, not one wide interface: a consumer depends only on what it uses, and a fake or
adapter satisfies them one at a time. No signature carries SQL or a Dolt-only operation; those
are reached through :meth:`CapabilityHost.capability`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Protocol, TypeVar, runtime_checkable

from ._errors import CapabilityUnsupported
from ._types import (
    Absent,
    Body,
    BumpOutcome,
    BumpRequest,
    ChangeSet,
    ConsistencyToken,
    EpochFloor,
    GuardReport,
    LivenessKind,
    LookupOutcome,
    OperationId,
    Outcome,
    PlacementView,
    PruneReport,
    RecordKind,
    RecordWrite,
    RefView,
    Revision,
    StreamKind,
    SyncReport,
    Versioned,
    WriterRow,
)

__all__ = [
    "AppendLog",
    "AtomicRecords",
    "CapabilityHost",
    "CommittedHistory",
    "EpochRefStore",
    "HiveFence",
    "Liveness",
    "PlacementStore",
    "StorageClock",
    "VersionedRecords",
    "require_capability",
]

C = TypeVar("C")


@runtime_checkable
class StorageClock(Protocol):
    """The clock staleness is measured by. ``now()`` is timezone-aware UTC. Satisfied
    structurally by root's ``StateClock`` and ``tests/harness/clock.FakeClock``."""

    def now(self) -> datetime: ...


@runtime_checkable
class VersionedRecords(Protocol):
    """Compare-and-set records. Every successful CAS issues a FRESH revision; UPDATE-only kinds
    (``PLACEMENT``) raise ``NotProvisioned`` on a CAS from ``ABSENT``."""

    def read(self, kind: RecordKind, key: str) -> Versioned[Body] | None: ...

    def read_many(
        self, kind: RecordKind, keys: Collection[str] | None = None
    ) -> Sequence[Versioned[Body]]: ...

    def compare_and_set(
        self,
        kind: RecordKind,
        key: str,
        body: Body,
        *,
        expected: Revision | Absent,
        operation: OperationId,
    ) -> Outcome: ...

    def lookup(self, kind: RecordKind, key: str, operation: OperationId) -> LookupOutcome: ...


@runtime_checkable
class AtomicRecords(Protocol):
    """Several compare-and-sets as ONE all-or-nothing write (settled by bh-xnn5p).

    Every expectation must hold, or nothing is written and the outcome is ``Conflict`` with the
    current value of the FIRST write whose expectation failed. ``Applied.revision`` is the fresh
    revision of ``writes[0]``; every member gets its own fresh revision and records
    ``operation`` as its witness, so ``VersionedRecords.lookup`` of ANY member resolves an
    ``Unknown``. Each member obeys the single-record rules (UPDATE-only kinds refuse
    ``ABSENT``); an empty list or a repeated ``(kind, key)`` is ``InvalidRequest``. Optional:
    a store that cannot write several records atomically does not implement it."""

    def compare_and_set_all(
        self, writes: Sequence[RecordWrite], *, operation: OperationId
    ) -> Outcome: ...


@runtime_checkable
class AppendLog(Protocol):
    """Append-only rows whose recorded-at stamp and retention age come from the STORE clock.

    ``append`` is idempotent for the same key, operation and body (same ``Applied``); any other
    second append of a key is a ``Conflict`` (replay detection). ``lookup_append`` resolves an
    ``Unknown`` append by its natural key (bh-9fg5k addition)."""

    def append(
        self, stream: StreamKind, key: str, body: Body, *, operation: OperationId
    ) -> Outcome: ...

    def scan(
        self,
        stream: StreamKind,
        *,
        key: str | None = None,
        after: Revision | None = None,
        limit: int,
    ) -> Sequence[Versioned[Body]]: ...

    def prune(
        self, stream: StreamKind, *, older_than: timedelta, operation: OperationId
    ) -> PruneReport: ...

    def lookup_append(
        self, stream: StreamKind, key: str, operation: OperationId
    ) -> LookupOutcome: ...


@runtime_checkable
class Liveness(Protocol):
    """Liveness rows stamped by the store's own clock; a caller can never freshen its own
    stamp. ``ages`` is computed in ONE read against the store clock and is non-negative."""

    def now(self) -> datetime: ...

    def renew(self, kind: LivenessKind, key: str, body: Body | None = None) -> datetime: ...

    def ages(
        self, kind: LivenessKind, keys: Collection[str] | None = None
    ) -> Mapping[str, timedelta]: ...

    def stamped_at(self, kind: LivenessKind, key: str) -> datetime | None: ...


@runtime_checkable
class CommittedHistory(Protocol):
    """Committed, versioned documents (config publication) with head, read-at-revision and
    remote sync. ``publish``'s ``Applied.revision`` is the exact committed hash; ``fetch`` only
    reports and never moves the local head; ``push`` reports a non-fast-forward as
    ``Conflict``. A store always has a head (the provisioning commit at least); ``head``
    raises ``NotProvisioned`` otherwise. ``consistent_read`` raises ``CapabilityUnsupported``
    where one atomic read is impossible; it never joins independent reads."""

    def head(self) -> Revision: ...

    def read_at(self, revision: Revision) -> Sequence[tuple[str, bytes]]: ...

    def consistent_read(self) -> AbstractContextManager[ConsistencyToken]: ...

    def publish(
        self,
        changes: ChangeSet,
        *,
        expected_head: Revision | Absent,
        operation: OperationId,
        author: str,
        message: str,
    ) -> Outcome: ...

    def lookup_publication(
        self, operation: OperationId, *, expected_head: Revision | Absent
    ) -> LookupOutcome: ...

    def fetch(self) -> SyncReport: ...

    def push(self) -> Outcome: ...


@runtime_checkable
class HiveFence(Protocol):
    """One hive's fence tables. ``commit_bump`` writes the writer row, the ``adopt-<epoch>``
    sentinel and the optional reclaim as ONE local commit; ``push`` publishes it; and
    ``lookup_bump`` finds the sentinel on the remote head. The port exposes the epoch reads;
    the domain chooses the epoch.

    ``commit_bump`` raises ``StorageUnavailable`` only when nothing was committed. Once the
    commit may exist it answers ``Unknown`` (bh-xxkf7.6): the caller never re-issues that bump;
    it pushes and resolves the outcome with ``lookup_bump`` under the bump's own operation."""

    def sync_to_remote(self) -> SyncReport: ...

    def writer(self) -> WriterRow | None: ...

    def remote_writer(self) -> WriterRow | None: ...

    def epoch_floor(self) -> EpochFloor: ...

    def guard_conformance(self) -> GuardReport: ...

    def commit_bump(self, bump: BumpRequest) -> BumpOutcome: ...

    def push(self) -> Outcome: ...

    def lookup_bump(self, frame: str, epoch: int, *, operation: OperationId) -> LookupOutcome: ...


@runtime_checkable
class EpochRefStore(Protocol):
    """The legacy ``refs/bh/epoch`` ref on the hive remote."""

    def read(self) -> RefView | None: ...

    def compare_and_set(self, frame: str, epoch: int, *, expected: RefView) -> Outcome: ...


@runtime_checkable
class PlacementStore(Protocol):
    """A typed view over ``RecordKind.PLACEMENT`` (absorbs root's ``PlacementAuthority``)."""

    def read(self, prefix: str) -> PlacementView | None: ...

    def compare_and_set(
        self,
        prefix: str,
        frame: str,
        epoch: int,
        *,
        expected: PlacementView,
        cause: str,
        operation: OperationId,
    ) -> Outcome: ...


@runtime_checkable
class CapabilityHost(Protocol):
    """Optional (Dolt-only) capability access. ``None`` for a capability the adapter lacks."""

    def capability(self, kind: type[C]) -> C | None: ...


def require_capability(host: CapabilityHost, kind: type[C]) -> C:
    """The consumer-side accessor: the capability, or ``CapabilityUnsupported``."""
    found = host.capability(kind)
    if found is None:
        raise CapabilityUnsupported(f"capability {kind.__name__} is not supported here")
    return found
