"""In-memory fakes of the port with an injected clock (hq-storage-port-adr sections 3 and 8).

The fakes model the same rules real Dolt enforces (UPDATE-only kinds, fresh revisions, one-commit
bumps, store-clock stamps), so a protocol test can never depend on behaviour the adapter refuses.
They own no wall time: every stamp and age comes from the injected :class:`StorageClock`.

Test-only controls (fault injection, provisioning, a simulated competing remote writer) live on
the fake classes and on :class:`FaultPlan`; the port protocols never expose them.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar

from ._errors import (
    CapabilityUnsupported,
    IntegrityViolation,
    InvalidRequest,
    NotProvisioned,
    StorageUnavailable,
)
from ._ports import StorageClock
from ._types import (
    ABSENT,
    CLAIM_FRAME_LABEL_PREFIX,
    RECOVERY_LABEL_PREFIX,
    RESUMABLE_LABEL,
    UPDATE_ONLY_KINDS,
    Absent,
    Applied,
    Body,
    BumpOutcome,
    BumpRequest,
    ChangeSet,
    Confirmed,
    Conflict,
    ConsistencyToken,
    EpochFloor,
    GuardReport,
    LivenessKind,
    LookupOutcome,
    NotConfirmed,
    OperationId,
    Outcome,
    PlacementView,
    PruneReport,
    ReclaimSet,
    RecordKind,
    RecordWrite,
    RefView,
    Revision,
    StreamKind,
    SyncReport,
    Unknown,
    UnknownReason,
    Versioned,
    WriterRow,
    adopt_sentinel,
)

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "FaultPlan",
    "FenceSnapshot",
    "InMemoryEpochRef",
    "InMemoryHiveFence",
    "InMemoryHqStorage",
]

C = TypeVar("C")

DEFAULT_MAX_BODY_BYTES = 64 * 1024
"""The fake's canonical-JSON body cap (the Dolt adapter enforces its own column limit)."""

_MAX_KEY_LEN = 255
_DEFAULT_GUARDS = ("bh_writer_guard", "bh_epoch_live_guard", "bh_write_mark_guard")


# --- shared helpers -------------------------------------------------------------------------


def _now(clock: StorageClock) -> datetime:
    now = clock.now()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise IntegrityViolation("the injected storage clock must return an aware datetime")
    return now.astimezone(UTC)


def _check_key(key: object, what: str = "key") -> str:
    if not isinstance(key, str) or not key or len(key) > _MAX_KEY_LEN:
        raise InvalidRequest(f"{what} must be a non-empty string of at most {_MAX_KEY_LEN} chars")
    return key


def _check_operation(operation: object) -> OperationId:
    if not isinstance(operation, str):
        raise InvalidRequest("operation id must be canonical UUID text")
    try:
        canonical = str(uuid.UUID(operation))
    except ValueError:
        raise InvalidRequest("operation id must be canonical UUID text") from None
    if canonical != operation:
        raise InvalidRequest("operation id must be canonical (lower-case, hyphenated) UUID text")
    return OperationId(operation)


def _canonical(body: object, *, cap: int) -> dict[str, Any]:
    """A private deep copy of ``body`` as canonical JSON, refusing non-JSON and oversize."""
    if not isinstance(body, Mapping):
        raise InvalidRequest("body must be a JSON object")
    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"body is not canonical JSON: {type(exc).__name__}") from None
    if len(text.encode()) > cap:
        raise InvalidRequest(f"body exceeds the {cap}-byte cap")
    return json.loads(text)


class _Mint:
    """Deterministic, opaque revision minting (64 hex, like the receiver token)."""

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._n = 0

    def revision(self) -> Revision:
        self._n += 1
        return Revision(hashlib.sha256(f"{self._namespace}\x00{self._n}".encode()).hexdigest())

    def operation(self) -> OperationId:
        self._n += 1
        return OperationId(str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self._namespace}/{self._n}")))


@dataclass(frozen=True)
class _Fault:
    kind: Literal["unknown", "unavailable"]
    on: str | None
    reason: UnknownReason = "commit"
    applied: bool = False


class FaultPlan:
    """Arms faults for the NEXT matching mutation of one fake (FIFO).

    ``arm_unknown(reason, applied=...)`` makes the mutation return ``Unknown`` with the write
    applied or not, so both branches of every consumer are testable. ``arm_unavailable()``
    makes it raise ``StorageUnavailable`` with nothing applied. ``on`` restricts the fault to
    one method name (``"compare_and_set"``, ``"append"``, ``"publish"``, ``"push"``, ...)."""

    def __init__(self) -> None:
        self._armed: list[_Fault] = []

    def arm_unknown(
        self, reason: UnknownReason = "commit", *, applied: bool, on: str | None = None
    ) -> None:
        if reason not in ("deadline", "transport", "commit"):
            raise ValueError(f"unknown reason {reason!r}")
        self._armed.append(_Fault("unknown", on, reason, applied))

    def arm_unavailable(self, *, on: str | None = None) -> None:
        self._armed.append(_Fault("unavailable", on))

    def clear(self) -> None:
        self._armed.clear()

    @property
    def pending(self) -> int:
        return len(self._armed)

    def take(self, method: str, *, unknown_ok: bool = True) -> _Fault | None:
        for index, fault in enumerate(self._armed):
            if fault.on not in (None, method):
                continue
            if fault.kind == "unknown" and not unknown_ok:
                continue
            del self._armed[index]
            if fault.kind == "unavailable":
                raise StorageUnavailable(f"{method}: injected unavailability (nothing sent)")
            return fault
        return None


def _settle(
    fault: _Fault | None,
    outcome: Outcome,
    *,
    operation: OperationId,
    expected: Revision | Absent,
    rollback: Any,
) -> Outcome:
    """Turn a computed outcome into the fault's ``Unknown`` when one is armed, keeping or
    rolling back the write as the fault says."""
    if fault is None:
        return outcome
    if isinstance(outcome, Applied) and not fault.applied:
        rollback()
    return Unknown(operation=operation, expected=expected, reason=fault.reason)


# --- InMemoryHqStorage ----------------------------------------------------------------------


@dataclass
class _Record:
    versioned: Versioned[Body]
    operation: OperationId
    recorded_at: datetime


@dataclass
class _Entry:
    seq: int
    versioned: Versioned[Body]
    operation: OperationId
    recorded_at: datetime


@dataclass(frozen=True)
class _Commit:
    revision: Revision
    parent: Revision | None
    snapshot: tuple[tuple[str, bytes], ...]
    operation: OperationId | None
    recorded_at: datetime
    author: str = ""
    message: str = ""


def _chain(commits: Mapping[Revision, Any], head: Revision | None) -> list[Revision]:
    out: list[Revision] = []
    while head is not None:
        out.append(head)
        head = commits[head].parent
    return out


def _sync_counts(
    commits: Mapping[Revision, Any], local: Revision, remote: Revision
) -> tuple[int, int]:
    local_chain = _chain(commits, local)
    remote_chain = _chain(commits, remote)
    remote_set = set(remote_chain)
    ahead = next((i for i, rev in enumerate(local_chain) if rev in remote_set), len(local_chain))
    base = local_chain[ahead] if ahead < len(local_chain) else None
    behind = remote_chain.index(base) if base in remote_set else len(remote_chain)
    return ahead, behind


class _ConsistentRead:
    def __init__(self, token: ConsistencyToken) -> None:
        self._token = token

    def __enter__(self) -> ConsistencyToken:
        return self._token

    def __exit__(self, *exc: object) -> None:
        return None


class _PlacementView:
    """``PlacementStore`` over the fake's ``PLACEMENT`` records. The fake body is
    ``{"frame", "epoch", "cause"}``; the real wire encoding is the Dolt adapter's."""

    def __init__(self, storage: InMemoryHqStorage) -> None:
        self._storage = storage

    def read(self, prefix: str) -> PlacementView | None:
        record = self._storage.read(RecordKind.PLACEMENT, prefix)
        if record is None:
            return None
        return PlacementView(
            frame=str(record.body["frame"]),
            epoch=int(record.body["epoch"]),  # type: ignore[arg-type]
            token=record.revision,
        )

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


class InMemoryHqStorage:
    """The in-memory HQ store: ``VersionedRecords``, ``AtomicRecords``, ``AppendLog``,
    ``Liveness``, ``CommittedHistory`` and ``CapabilityHost`` over one injected
    :class:`StorageClock`.

    The committed history starts with one provisioning commit shared with a simulated remote.
    ``consistent_read=False`` models an adapter (Git) that refuses the combined atomic read."""

    def __init__(
        self,
        clock: StorageClock,
        *,
        consistent_read: bool = True,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        backend_identity: str = "in-memory",
    ) -> None:
        self._clock = clock
        self._cap = max_body_bytes
        self._consistent = consistent_read
        self._identity = backend_identity
        self._mint = _Mint(backend_identity)
        self.faults = FaultPlan()
        self._records: dict[tuple[RecordKind, str], _Record] = {}
        self._streams: dict[StreamKind, dict[str, _Entry]] = {s: {} for s in StreamKind}
        self._cursors: dict[Revision, tuple[StreamKind, int]] = {}
        self._seq = 0
        self._live: dict[tuple[LivenessKind, str], tuple[datetime, dict[str, Any] | None]] = {}
        self._capabilities: dict[type, object] = {}
        genesis = _Commit(self._mint.revision(), None, (), None, _now(clock), "", "provision")
        self._commits: dict[Revision, _Commit] = {genesis.revision: genesis}
        self._local = genesis.revision
        self._remote = genesis.revision
        self._reachable = True
        self._publications: set[OperationId] = set()

    # -- test controls ------------------------------------------------------------------------

    def provision(self, kind: RecordKind, key: str, body: Body) -> Revision:
        """Operator provisioning: seed a record (the only way an UPDATE-only kind is created)."""
        revision = self._mint.revision()
        self._records[(kind, _check_key(key))] = _Record(
            Versioned(key, revision, _canonical(body, cap=self._cap)),
            self._mint.operation(),
            _now(self._clock),
        )
        return revision

    def placements(self) -> _PlacementView:
        """The ``PlacementStore`` view over this store's ``PLACEMENT`` records."""
        return _PlacementView(self)

    def register_capability(self, kind: type[C], implementation: C) -> None:
        self._capabilities[kind] = implementation

    def set_reachable(self, reachable: bool) -> None:
        """Make the simulated remote (``fetch`` / ``push``) reachable or not."""
        self._reachable = reachable

    def remote_publish(self, documents: Sequence[tuple[str, bytes]]) -> Revision:
        """Simulate a competing writer committing on the remote head."""
        parent = self._commits[self._remote]
        snapshot = dict(parent.snapshot)
        snapshot.update(documents)
        commit = _Commit(
            self._mint.revision(),
            parent.revision,
            tuple(sorted(snapshot.items())),
            self._mint.operation(),
            _now(self._clock),
            "remote",
            "competing",
        )
        self._commits[commit.revision] = commit
        self._remote = commit.revision
        return commit.revision

    # -- CapabilityHost -----------------------------------------------------------------------

    def capability(self, kind: type[C]) -> C | None:
        return self._capabilities.get(kind)  # type: ignore[return-value]

    # -- VersionedRecords ---------------------------------------------------------------------

    def read(self, kind: RecordKind, key: str) -> Versioned[Body] | None:
        record = self._records.get((RecordKind(kind), _check_key(key)))
        return None if record is None else record.versioned

    def read_many(
        self, kind: RecordKind, keys: Collection[str] | None = None
    ) -> Sequence[Versioned[Body]]:
        kind = RecordKind(kind)
        wanted = None if keys is None else {_check_key(k) for k in keys}
        return [
            record.versioned
            for (k, key), record in sorted(self._records.items(), key=lambda item: item[0][1])
            if k == kind and (wanted is None or key in wanted)
        ]

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
        _check_key(key)
        operation = _check_operation(operation)
        canonical = _canonical(body, cap=self._cap)
        if isinstance(expected, Absent) and kind in UPDATE_ONLY_KINDS:
            raise NotProvisioned(f"{kind.value} is UPDATE-only: operator provisioning seeds it")
        fault = self.faults.take("compare_and_set")
        slot = (kind, key)
        prior = self._records.get(slot)
        current = None if prior is None else prior.versioned.revision
        holds = current is None if isinstance(expected, Absent) else current == expected
        outcome: Outcome
        if not holds:
            outcome = Conflict(None if prior is None else prior.versioned)
        else:
            revision = self._mint.revision()
            self._records[slot] = _Record(
                Versioned(key, revision, canonical), operation, _now(self._clock)
            )
            outcome = Applied(revision, operation)

        def rollback() -> None:
            if prior is None:
                self._records.pop(slot, None)
            else:
                self._records[slot] = prior

        return _settle(fault, outcome, operation=operation, expected=expected, rollback=rollback)

    def compare_and_set_all(
        self, writes: Sequence[RecordWrite], *, operation: OperationId
    ) -> Outcome:
        operation = _check_operation(operation)
        prepared = _check_writes(writes, cap=self._cap)
        fault = self.faults.take("compare_and_set_all")
        priors = {slot: self._records.get(slot) for slot, _body, _expected in prepared}
        outcome: Outcome | None = None
        for slot, _body, expected in prepared:
            prior = priors[slot]
            current = None if prior is None else prior.versioned.revision
            if (current is not None) if isinstance(expected, Absent) else current != expected:
                outcome = Conflict(None if prior is None else prior.versioned)
                break
        if outcome is None:
            stamp = _now(self._clock)
            first: Revision | None = None
            for (kind, key), body, _expected in prepared:
                revision = self._mint.revision()
                first = first or revision
                self._records[(kind, key)] = _Record(
                    Versioned(key, revision, body), operation, stamp
                )
            outcome = Applied(first, operation)  # type: ignore[arg-type]

        def rollback() -> None:
            for slot, prior in priors.items():
                if prior is None:
                    self._records.pop(slot, None)
                else:
                    self._records[slot] = prior

        return _settle(
            fault, outcome, operation=operation, expected=prepared[0][2], rollback=rollback
        )

    def lookup(self, kind: RecordKind, key: str, operation: OperationId) -> LookupOutcome:
        operation = _check_operation(operation)
        record = self._records.get((RecordKind(kind), _check_key(key)))
        if record is not None and record.operation == operation:
            return Confirmed(operation, record.versioned.revision, record.recorded_at)
        return NotConfirmed(operation)

    # -- AppendLog ------------------------------------------------------------------------------

    def append(
        self, stream: StreamKind, key: str, body: Body, *, operation: OperationId
    ) -> Outcome:
        stream = StreamKind(stream)
        _check_key(key)
        operation = _check_operation(operation)
        canonical = _canonical(body, cap=self._cap)
        fault = self.faults.take("append")
        entries = self._streams[stream]
        prior = entries.get(key)
        outcome: Outcome
        if prior is not None:
            same = prior.operation == operation and prior.versioned.body == canonical
            outcome = (
                Applied(prior.versioned.revision, operation) if same else Conflict(prior.versioned)
            )
            return _settle(
                fault, outcome, operation=operation, expected=ABSENT, rollback=lambda: None
            )
        self._seq += 1
        revision = self._mint.revision()
        entries[key] = _Entry(
            self._seq, Versioned(key, revision, canonical), operation, _now(self._clock)
        )
        self._cursors[revision] = (stream, self._seq)
        outcome = Applied(revision, operation)

        def rollback() -> None:
            entries.pop(key, None)
            self._cursors.pop(revision, None)

        return _settle(fault, outcome, operation=operation, expected=ABSENT, rollback=rollback)

    def scan(
        self,
        stream: StreamKind,
        *,
        key: str | None = None,
        after: Revision | None = None,
        limit: int,
    ) -> Sequence[Versioned[Body]]:
        stream = StreamKind(stream)
        if not isinstance(limit, int) or limit < 1:
            raise InvalidRequest("limit must be a positive integer")
        floor = 0
        if after is not None:
            cursor = self._cursors.get(after)
            if cursor is None or cursor[0] != stream:
                raise InvalidRequest("after is not a revision this stream issued")
            floor = cursor[1]
        rows = sorted(self._streams[stream].values(), key=lambda entry: entry.seq)
        picked = [
            entry.versioned
            for entry in rows
            if entry.seq > floor and (key is None or entry.versioned.key == key)
        ]
        return picked[:limit]

    def prune(
        self, stream: StreamKind, *, older_than: timedelta, operation: OperationId
    ) -> PruneReport:
        stream = StreamKind(stream)
        operation = _check_operation(operation)
        if not isinstance(older_than, timedelta) or older_than < timedelta(0):
            raise InvalidRequest("older_than must be a non-negative timedelta")
        self.faults.take("prune", unknown_ok=False)
        now = _now(self._clock)
        entries = self._streams[stream]
        doomed = [key for key, entry in entries.items() if now - entry.recorded_at > older_than]
        for key in doomed:
            del entries[key]
        return PruneReport(stream, len(doomed), len(entries), operation)

    def lookup_append(self, stream: StreamKind, key: str, operation: OperationId) -> LookupOutcome:
        operation = _check_operation(operation)
        entry = self._streams[StreamKind(stream)].get(_check_key(key))
        if entry is not None and entry.operation == operation:
            return Confirmed(operation, entry.versioned.revision, entry.recorded_at)
        return NotConfirmed(operation)

    # -- Liveness -------------------------------------------------------------------------------

    def now(self) -> datetime:
        return _now(self._clock)

    def renew(self, kind: LivenessKind, key: str, body: Body | None = None) -> datetime:
        kind = LivenessKind(kind)
        _check_key(key)
        canonical = None if body is None else _canonical(body, cap=self._cap)
        self.faults.take("renew", unknown_ok=False)
        stamp = _now(self._clock)
        self._live[(kind, key)] = (stamp, canonical)
        return stamp

    def ages(
        self, kind: LivenessKind, keys: Collection[str] | None = None
    ) -> Mapping[str, timedelta]:
        kind = LivenessKind(kind)
        now = _now(self._clock)
        wanted = None if keys is None else set(keys)
        return {
            key: max(timedelta(0), now - stamp)
            for (k, key), (stamp, _) in sorted(self._live.items(), key=lambda item: item[0][1])
            if k == kind and (wanted is None or key in wanted)
        }

    def stamped_at(self, kind: LivenessKind, key: str) -> datetime | None:
        found = self._live.get((LivenessKind(kind), _check_key(key)))
        return None if found is None else found[0]

    # -- CommittedHistory -----------------------------------------------------------------------

    def head(self) -> Revision:
        return self._local

    def read_at(self, revision: Revision) -> Sequence[tuple[str, bytes]]:
        commit = self._commits.get(revision)
        if commit is None:
            raise NotProvisioned("no commit at that revision")
        return list(commit.snapshot)

    def consistent_read(self) -> _ConsistentRead:
        if not self._consistent:
            raise CapabilityUnsupported("this store cannot provide one atomic combined read")
        return _ConsistentRead(ConsistencyToken(self._identity, "0", self._local))

    def publish(
        self,
        changes: ChangeSet,
        *,
        expected_head: Revision | Absent,
        operation: OperationId,
        author: str,
        message: str,
    ) -> Outcome:
        operation = _check_operation(operation)
        documents = _check_changes(changes)
        if operation in self._publications:
            raise InvalidRequest("operation id already witnesses a publication; mint a new one")
        fault = self.faults.take("publish")
        prior = self._local
        holds = not isinstance(expected_head, Absent) and expected_head == prior
        outcome: Outcome
        if not holds:
            outcome = Conflict(Versioned("head", prior, {}))
        else:
            snapshot = dict(self._commits[prior].snapshot)
            snapshot.update(documents)
            commit = _Commit(
                self._mint.revision(),
                prior,
                tuple(sorted(snapshot.items())),
                operation,
                _now(self._clock),
                author,
                message,
            )
            self._commits[commit.revision] = commit
            self._local = commit.revision
            self._publications.add(operation)
            outcome = Applied(commit.revision, operation)

        def rollback() -> None:
            self._commits.pop(self._local, None)
            self._local = prior
            self._publications.discard(operation)

        return _settle(
            fault, outcome, operation=operation, expected=expected_head, rollback=rollback
        )

    def lookup_publication(
        self, operation: OperationId, *, expected_head: Revision | Absent
    ) -> LookupOutcome:
        operation = _check_operation(operation)
        parent = None if isinstance(expected_head, Absent) else expected_head
        for revision in _chain(self._commits, self._local):
            commit = self._commits[revision]
            if commit.operation == operation and commit.parent == parent:
                return Confirmed(operation, revision, commit.recorded_at)
        return NotConfirmed(operation)

    def fetch(self) -> SyncReport:
        if not self._reachable:
            raise StorageUnavailable("fetch: remote unreachable")
        self.faults.take("fetch", unknown_ok=False)
        ahead, behind = _sync_counts(self._commits, self._local, self._remote)
        return SyncReport(self._local, ahead, behind, ahead > 0 and behind > 0)

    def push(self) -> Outcome:
        if not self._reachable:
            raise StorageUnavailable("push: remote unreachable")
        fault = self.faults.take("push")
        operation = self._mint.operation()
        prior = self._remote
        outcome: Outcome
        if prior in _chain(self._commits, self._local):
            self._remote = self._local
            outcome = Applied(self._local, operation)
        else:
            outcome = Conflict(Versioned("remote", prior, {}))

        def rollback() -> None:
            self._remote = prior

        return _settle(fault, outcome, operation=operation, expected=prior, rollback=rollback)


def _check_writes(
    writes: Sequence[RecordWrite], *, cap: int
) -> list[tuple[tuple[RecordKind, str], dict[str, Any], Revision | Absent]]:
    """``AtomicRecords`` validation, refused before any write: a non-empty list of distinct
    ``(kind, key)`` members, each a valid single-record compare-and-set."""
    if isinstance(writes, RecordWrite) or not isinstance(writes, Sequence) or not writes:
        raise InvalidRequest("an atomic compare-and-set needs a non-empty list of writes")
    out: list[tuple[tuple[RecordKind, str], dict[str, Any], Revision | Absent]] = []
    seen: set[tuple[RecordKind, str]] = set()
    for write in writes:
        if not isinstance(write, RecordWrite):
            raise InvalidRequest("every member of an atomic compare-and-set is a RecordWrite")
        slot = (RecordKind(write.kind), _check_key(write.key))
        if slot in seen:
            raise InvalidRequest(f"{slot[0].value}/{slot[1]} appears twice in one write")
        seen.add(slot)
        body = _canonical(write.body, cap=cap)
        expected = write.expected
        if not isinstance(expected, Absent) and not isinstance(expected, str):
            raise InvalidRequest("expected must be a Revision or ABSENT")
        if isinstance(expected, Absent) and slot[0] in UPDATE_ONLY_KINDS:
            raise NotProvisioned(f"{slot[0].value} is UPDATE-only: operator provisioning seeds it")
        out.append((slot, body, expected))
    return out


def _check_changes(changes: ChangeSet) -> dict[str, bytes]:
    if not isinstance(changes, ChangeSet) or not changes.documents:
        raise InvalidRequest("a publication needs at least one document")
    tables = changes.tables
    if not tables or not all(isinstance(t, str) and t for t in tables):
        raise InvalidRequest("a publication needs an explicit, non-empty table allowlist")
    out: dict[str, bytes] = {}
    for name, raw in changes.documents:
        _check_key(name, "document name")
        table, sep, rest = name.partition("/")
        if not sep or not rest or table not in tables:
            raise InvalidRequest(f"document {name!r} is not '<table>/<name>' in the allowlist")
        if not isinstance(raw, bytes):
            raise InvalidRequest(f"document {name!r} must be raw bytes")
        if name in out:
            raise InvalidRequest(f"document {name!r} appears twice in one change set")
        out[name] = raw
    return out


# --- InMemoryHiveFence ----------------------------------------------------------------------


@dataclass(frozen=True)
class FenceSnapshot:
    """The fence tables (and the reclaimable bead rows) at one commit. Test-side read."""

    writer: WriterRow | None
    live_epoch: int
    marks: frozenset[tuple[str, int]]
    beads: Mapping[str, tuple[str, str]] = field(default_factory=dict)  # id -> (status, assignee)
    labels: frozenset[tuple[str, str]] = frozenset()  # (bead id, label)
    #: ``(comment id, bead id, author, text, created_at as 'YYYY-MM-DD HH:MM:SS' UTC)``
    comments: frozenset[tuple[str, str, str, str, str]] = frozenset()


COMMENT_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
"""How a reclaim note's ``created_at`` is stored (whole seconds, UTC)."""


def check_reclaim(reclaim: ReclaimSet) -> None:
    """Refuse a malformed :class:`ReclaimSet` before any write (the Dolt adapter restates it)."""
    if not isinstance(reclaim, ReclaimSet):
        raise InvalidRequest("reclaim must be a ReclaimSet")
    _check_key(reclaim.dead_frame, "dead frame")
    for bead in reclaim.bead_ids:
        _check_key(bead, "bead id")
    if not reclaim.resumable <= reclaim.bead_ids:
        raise InvalidRequest("every resumable bead must be in the reclaim set")
    seen: set[str] = set()
    for note in reclaim.notes:
        if note.bead_id not in reclaim.bead_ids:
            raise InvalidRequest("every reclaim note must name a bead in the reclaim set")
        _check_key(note.comment_id, "comment id")
        _check_key(note.author, "comment author")
        if not isinstance(note.text, str) or not note.text:
            raise InvalidRequest("a reclaim note needs its text")
        if not isinstance(note.created_at, datetime) or note.created_at.tzinfo is None:
            raise InvalidRequest("a reclaim note's created_at must be an aware datetime")
        if note.comment_id in seen:
            raise InvalidRequest("reclaim note comment ids must be unique")
        seen.add(note.comment_id)


def _apply_reclaim(state: FenceSnapshot, reclaim: ReclaimSet) -> tuple[dict, set, set]:
    beads = dict(state.beads)
    labels = set(state.labels)
    comments = set(state.comments)
    live = {
        bead
        for bead in reclaim.bead_ids
        if beads.get(bead) is not None and beads[bead][0] == "in_progress"
    }
    for bead in sorted(live):
        beads[bead] = ("open", "")
        drop = (CLAIM_FRAME_LABEL_PREFIX,)
        if bead in reclaim.resumable:
            drop += (RECOVERY_LABEL_PREFIX,)
        labels = {(b, label) for b, label in labels if b != bead or not label.startswith(drop)}
        if bead in reclaim.resumable:
            labels.add((bead, RESUMABLE_LABEL))
    for note in reclaim.notes:
        if note.bead_id in live:
            stamp = note.created_at.astimezone(UTC).strftime(COMMENT_STAMP_FORMAT)
            comments.add((note.comment_id, note.bead_id, note.author, note.text, stamp))
    return beads, labels, comments


@dataclass(frozen=True)
class _FenceCommit:
    revision: Revision
    parent: Revision | None
    state: FenceSnapshot
    operation: OperationId | None
    recorded_at: datetime
    sentinel: str | None = None


class InMemoryHiveFence:
    """The in-memory ``HiveFence`` with a local ``main`` and a simulated remote.

    ``writer=None`` models a hive that is not cut over (bumps raise ``NotProvisioned``).
    ``beads`` seeds bead rows as ``id -> (status, assignee)`` and ``labels`` their
    ``(bead, label)`` rows, for the reclaim."""

    def __init__(
        self,
        clock: StorageClock,
        *,
        writer: WriterRow | None = WriterRow(frame="", epoch=0),  # noqa: B008 - frozen value
        beads: Mapping[str, tuple[str, str]] | None = None,
        labels: Collection[tuple[str, str]] = (),
        guards: Sequence[str] = _DEFAULT_GUARDS,
        missing_guards: Collection[str] = (),
        namespace: str = "in-memory-fence",
    ) -> None:
        self._clock = clock
        self._mint = _Mint(namespace)
        self.faults = FaultPlan()
        self._guards = tuple(guards)
        self._missing = tuple(g for g in self._guards if g in set(missing_guards))
        state = FenceSnapshot(
            writer=writer,
            live_epoch=0 if writer is None else writer.epoch,
            marks=frozenset(),
            beads=dict(beads or {}),
            labels=frozenset(labels),
        )
        genesis = _FenceCommit(self._mint.revision(), None, state, None, _now(clock))
        self._commits: dict[Revision, _FenceCommit] = {genesis.revision: genesis}
        self._local = genesis.revision
        self._remote = genesis.revision
        self._reachable = True

    # -- test controls ------------------------------------------------------------------------

    def set_reachable(self, reachable: bool) -> None:
        self._reachable = reachable

    def snapshot_at(self, revision: Revision) -> FenceSnapshot:
        commit = self._commits.get(revision)
        if commit is None:
            raise NotProvisioned("no fence commit at that revision")
        return commit.state

    def local_head(self) -> Revision:
        return self._local

    def remote_head(self) -> Revision:
        return self._remote

    def parent_of(self, revision: Revision) -> Revision | None:
        return self._commits[revision].parent

    def remote_bump(self, bump: BumpRequest) -> Revision:
        """Simulate a competing node landing a bump directly on the remote head."""
        commit = self._bump_commit(self._remote, bump)
        self._commits[commit.revision] = commit
        self._remote = commit.revision
        return commit.revision

    # -- HiveFence ------------------------------------------------------------------------------

    def sync_to_remote(self) -> SyncReport:
        if not self._reachable:
            raise StorageUnavailable("sync_to_remote: remote unreachable")
        self.faults.take("sync_to_remote", unknown_ok=False)
        ahead, behind = _sync_counts(self._commits, self._local, self._remote)
        self._local = self._remote
        return SyncReport(self._local, ahead, behind, ahead > 0 and behind > 0)

    def writer(self) -> WriterRow | None:
        return self._commits[self._local].state.writer

    def remote_writer(self) -> WriterRow | None:
        if not self._reachable:
            raise StorageUnavailable("remote_writer: remote unreachable")
        return self._commits[self._remote].state.writer

    def epoch_floor(self) -> EpochFloor:
        epochs = [
            self._commits[rev].state.writer.epoch  # type: ignore[union-attr]
            for rev in _chain(self._commits, self._local)
            if self._commits[rev].state.writer is not None
        ]
        writer = self.writer()
        return EpochFloor(
            history_max=max(epochs, default=0), writer=None if writer is None else writer.epoch
        )

    def guard_conformance(self) -> GuardReport:
        return GuardReport(
            expected=len(self._guards),
            present=len(self._guards) - len(self._missing),
            missing=self._missing,
        )

    def commit_bump(self, bump: BumpRequest) -> BumpOutcome:
        """``Applied`` with the new local head; ``Unknown`` (an armed fault, the bump committed
        or not as it says) when the commit's fate is unknown; ``StorageUnavailable`` only when
        nothing was committed. Validation and the floor run before any fault fires."""
        prior = self._local
        commit = self._bump_commit(prior, bump)
        fault = self.faults.take("commit_bump")
        if fault is None or fault.applied:
            self._commits[commit.revision] = commit
            self._local = commit.revision
        if fault is not None:
            return Unknown(operation=bump.operation, expected=prior, reason=fault.reason)
        return Applied(commit.revision, bump.operation)

    def push(self) -> Outcome:
        if not self._reachable:
            raise StorageUnavailable("push: remote unreachable")
        fault = self.faults.take("push")
        operation = self._commits[self._local].operation or self._mint.operation()
        prior = self._remote
        outcome: Outcome
        if prior in _chain(self._commits, self._local):
            self._remote = self._local
            outcome = Applied(self._local, operation)
        else:
            outcome = Conflict(_writer_versioned(self._commits[prior]))

        def rollback() -> None:
            self._remote = prior

        return _settle(fault, outcome, operation=operation, expected=prior, rollback=rollback)

    def lookup_bump(self, frame: str, epoch: int, *, operation: OperationId) -> LookupOutcome:
        """``Confirmed`` while the ``adopt-<epoch>`` sentinel naming ``frame`` is visible on the
        remote head; ``NotConfirmed`` otherwise (including once a later bump retired it)."""
        operation = _check_operation(operation)
        if not self._reachable:
            raise StorageUnavailable("lookup_bump: remote unreachable")
        sentinel = adopt_sentinel(epoch)
        head = self._commits[self._remote]
        if any(mark == sentinel for mark, _ in head.state.marks):
            for revision in _chain(self._commits, self._remote):
                commit = self._commits[revision]
                if commit.sentinel == sentinel:
                    writer = commit.state.writer
                    if writer is not None and writer.frame == frame:
                        return Confirmed(operation, revision, commit.recorded_at)
                    break
        return NotConfirmed(operation)

    # -- internals ----------------------------------------------------------------------------

    def _bump_commit(self, parent_rev: Revision, bump: BumpRequest) -> _FenceCommit:
        if not isinstance(bump, BumpRequest):
            raise InvalidRequest("commit_bump takes a BumpRequest")
        _check_key(bump.frame, "frame")
        operation = _check_operation(bump.operation)
        if not isinstance(bump.epoch, int) or bump.epoch < 1:
            raise InvalidRequest("epoch must be a positive integer")
        if bump.reclaim is not None:
            check_reclaim(bump.reclaim)
        parent = self._commits[parent_rev]
        state = parent.state
        if state.writer is None:
            raise NotProvisioned("the hive is not cut over: no bh_writer row")
        history_max = max(
            (
                self._commits[rev].state.writer.epoch  # type: ignore[union-attr]
                for rev in _chain(self._commits, parent_rev)
                if self._commits[rev].state.writer is not None
            ),
            default=0,
        )
        floor = max(history_max, state.writer.epoch)
        if bump.epoch <= floor:
            raise IntegrityViolation(f"epoch {bump.epoch} does not exceed the floor {floor}")
        sentinel = adopt_sentinel(bump.epoch)
        beads, labels, comments = dict(state.beads), set(state.labels), set(state.comments)
        if bump.reclaim is not None:
            beads, labels, comments = _apply_reclaim(state, bump.reclaim)
        marks = frozenset(
            {(mark, epoch) for mark, epoch in state.marks if epoch >= bump.epoch}
            | {(sentinel, bump.epoch)}
        )
        new_state = FenceSnapshot(
            writer=WriterRow(bump.frame, bump.epoch, bump.revision),
            live_epoch=bump.epoch,
            marks=marks,
            beads=beads,
            labels=frozenset(labels),
            comments=frozenset(comments),
        )
        return _FenceCommit(
            self._mint.revision(), parent_rev, new_state, operation, _now(self._clock), sentinel
        )


def _writer_versioned(commit: _FenceCommit) -> Versioned[Body]:
    writer = commit.state.writer
    body: dict[str, Any] = {} if writer is None else {"frame": writer.frame, "epoch": writer.epoch}
    return Versioned("bh_writer", commit.revision, body)


# --- InMemoryEpochRef -----------------------------------------------------------------------


class InMemoryEpochRef:
    """The in-memory legacy ``refs/bh/epoch`` (``EpochRefStore``). ``None`` start: the ref does
    not exist, so it is not maintained and every CAS conflicts."""

    def __init__(self, initial: tuple[str, int] | None = None, *, namespace: str = "epoch-ref"):
        self._mint = _Mint(namespace)
        self.faults = FaultPlan()
        self._ref: RefView | None = None
        if initial is not None:
            self._ref = RefView(initial[0], int(initial[1]), self._mint.revision())

    def read(self) -> RefView | None:
        return self._ref

    def compare_and_set(self, frame: str, epoch: int, *, expected: RefView) -> Outcome:
        _check_key(frame, "frame")
        fault = self.faults.take("compare_and_set")
        operation = self._mint.operation()
        prior = self._ref
        outcome: Outcome
        if prior is None or prior.sha != expected.sha:
            current = (
                None
                if prior is None
                else Versioned(
                    "refs/bh/epoch", prior.sha, {"frame": prior.frame, "epoch": prior.epoch}
                )
            )
            outcome = Conflict(current)
        else:
            self._ref = RefView(frame, int(epoch), self._mint.revision())
            outcome = Applied(self._ref.sha, operation)

        def rollback() -> None:
            self._ref = prior

        return _settle(
            fault, outcome, operation=operation, expected=expected.sha, rollback=rollback
        )
