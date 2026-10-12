"""Value, outcome and fence-record types of the HQ storage port (hq-storage-port-adr section 2).

Every type here is a frozen value. Revisions are opaque, adapter-minted and compared by
equality only; no port operation accepts "latest" as an expectation.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Generic, Literal, NewType, TypeVar, Union

__all__ = [
    "ABSENT",
    "ADOPT_SENTINEL_PREFIX",
    "CLAIM_FRAME_LABEL_PREFIX",
    "RECOVERY_LABEL_PREFIX",
    "RESUMABLE_LABEL",
    "UPDATE_ONLY_KINDS",
    "Absent",
    "Applied",
    "Body",
    "BumpOutcome",
    "BumpRequest",
    "ChangeSet",
    "Confirmed",
    "Conflict",
    "ConsistencyToken",
    "EpochFloor",
    "GuardReport",
    "JsonValue",
    "LivenessKind",
    "LookupOutcome",
    "NotConfirmed",
    "OperationId",
    "Outcome",
    "PlacementView",
    "PruneReport",
    "ReclaimNote",
    "ReclaimSet",
    "RecordKind",
    "RecordWrite",
    "RefView",
    "Revision",
    "StreamKind",
    "SyncReport",
    "Unknown",
    "UnknownReason",
    "Versioned",
    "WriterRow",
    "adopt_sentinel",
    "new_operation_id",
]

T = TypeVar("T")

Revision = NewType("Revision", str)
"""Opaque, adapter-minted. Compare by equality only."""

OperationId = NewType("OperationId", str)
"""Caller-minted canonical UUID text, one per intent. The store persists it as the witness."""


def new_operation_id() -> OperationId:
    """Mint a fresh :data:`OperationId` for one intent (never reuse it for a retry)."""
    return OperationId(str(uuid.uuid4()))


class Absent:
    """The expectation "the record must not exist yet". Use the :data:`ABSENT` singleton."""

    _instance: Absent | None = None

    def __new__(cls) -> Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __reduce__(self) -> str:
        return "ABSENT"


ABSENT: Final[Absent] = Absent()

JsonValue = Union[  # noqa: UP007 - a recursive runtime alias needs the string forward refs
    None, bool, int, float, str, Sequence["JsonValue"], Mapping[str, "JsonValue"]
]
Body = Mapping[str, JsonValue]
"""Canonical JSON object. Adapters enforce a size cap and refuse non-JSON values."""


class RecordKind(StrEnum):
    """Versioned (compare-and-set) record kinds. Closed: a new kind is a port change plus
    contract cases."""

    PLACEMENT = "placement"  # hq_live_hive_leases | refs/bh/lease/<prefix>; UPDATE-only
    FAILOVER_POLICY = "failover_policy"  # hq_live_failover_policy
    AUTHORITY = "authority"  # frame grant / renewal / lifecycle record


UPDATE_ONLY_KINDS: Final[frozenset[RecordKind]] = frozenset({RecordKind.PLACEMENT})
"""Kinds a CAS may never create: operator provisioning seeds them (ADR section 2)."""


class StreamKind(StrEnum):
    """Append-only row streams."""

    INBOX = "inbox"  # per-principal signed proposals and beats
    RECEIPT = "receipt"  # trusted first-seen receipts and floors
    REPORT = "report"  # frame reports and evidence


class LivenessKind(StrEnum):
    """Liveness row kinds, stamped by the store clock on every renewal."""

    SESSION = "session"  # frame_<principal>_<epoch>_session
    EVIDENCE = "evidence"  # measured evidence rows


@dataclass(frozen=True)
class RecordWrite:
    """One member of an atomic multi-record compare-and-set (``AtomicRecords``): the record
    ``kind``/``key`` moves to ``body`` iff it is still at ``expected``."""

    kind: RecordKind
    key: str
    body: Body
    expected: Revision | Absent


@dataclass(frozen=True)
class Versioned(Generic[T]):
    """A value at the revision the store issued for it."""

    key: str
    revision: Revision
    body: T


# --- outcomes ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Applied:
    """The mutation committed. ``revision`` is the new revision (or head) the store issued."""

    revision: Revision
    operation: OperationId


@dataclass(frozen=True)
class Conflict:
    """The expectation did not hold (or a serialization failure at commit). ``current`` is what
    the store holds now; ``None`` when absent."""

    current: Versioned[Body] | None


UnknownReason = Literal["deadline", "transport", "commit"]


@dataclass(frozen=True)
class Unknown:
    """The mutation may or may not have committed. Resolve it with the matching ``lookup`` using
    the SAME ``operation``; ``expected`` is the ORIGINAL expectation, never a refreshed one.

    ``reason``: ``"deadline"`` (the operation budget ran out awaiting the result),
    ``"transport"`` (the result read failed after the request may have committed) or
    ``"commit"`` (the commit acknowledgment itself was lost)."""

    operation: OperationId
    expected: Revision | Absent
    reason: UnknownReason


Outcome = Union[Applied, Conflict, Unknown]  # noqa: UP007 - runtime alias used in isinstance


@dataclass(frozen=True)
class Confirmed:
    """The witnessed mutation is applied. ``revision`` is where the witness is visible; it may
    already be superseded, so it is not a snapshot."""

    operation: OperationId
    revision: Revision
    recorded_at: datetime  # store-clock stamp of the confirmed write


@dataclass(frozen=True)
class NotConfirmed:
    """No confirmed outcome is visible NOW. Not proof that the mutation did not apply, and never
    a licence to reissue it (ADR section 2, rules 2 and 3)."""

    operation: OperationId


LookupOutcome = Union[Confirmed, NotConfirmed]  # noqa: UP007

BumpOutcome = Union[Applied, Unknown]  # noqa: UP007
"""``HiveFence.commit_bump``'s result (bh-xxkf7.6). ``Applied``: the bump is the local head
(``revision``). ``Unknown``: the local commit may or may not exist (the commit raised, or the
head read after it failed); ``expected`` is the local head before the bump. There is no
``Conflict``: a bump is local, and the floor refusal is ``IntegrityViolation``. An ``Unknown``
bump is never re-issued: push, then resolve with ``lookup_bump`` under the SAME operation."""


# --- append log ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class PruneReport:
    """What one ``AppendLog.prune`` removed, measured against the store clock."""

    stream: StreamKind
    removed: int
    retained: int
    operation: OperationId


# --- committed history ----------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeSet:
    """One publication: ordered canonical raw documents plus the explicit staging allowlist.

    A document name is ``"<table>/<name>"`` and its table must be in ``tables``; an adapter
    stages exactly ``tables`` and never "adds everything" (settled in bh-9fg5k)."""

    documents: Sequence[tuple[str, bytes]]
    tables: frozenset[str]


@dataclass(frozen=True)
class ConsistencyToken:
    """One atomic combined read; maps onto root's ``HqConsistencyToken``."""

    backend_identity: str
    generation: str
    revision: Revision


@dataclass(frozen=True)
class SyncReport:
    """Local head against the remote, as observed by one fetch or sync."""

    head: Revision
    ahead: int
    behind: int
    diverged: bool


# --- hive fence -----------------------------------------------------------------------------


@dataclass(frozen=True)
class WriterRow:
    """The ``bh_writer`` singleton (``id = 1``). Root ``writer_adopt`` re-exports it."""

    frame: str
    epoch: int
    revision: str = ""


@dataclass(frozen=True)
class PlacementView:
    """Placement as read from HQ, with the CAS token the next write must present.

    ``token`` defaults to empty for a view that carries no CAS token: one built from a cached
    lease or for an audit (``doctor``, ``fence_audit``). No store mints an empty revision, so a
    store that compares tokens by equality reports a compare-and-set against such a view as a
    ``Conflict``. Root ``writer_adopt`` re-exports this record (bh-xxkf7.4)."""

    frame: str
    epoch: int
    token: Revision = Revision("")


@dataclass(frozen=True)
class RefView:
    """``refs/bh/epoch`` as read from the hive remote, with its sha (the CAS expectation)."""

    frame: str
    epoch: int
    sha: Revision


@dataclass(frozen=True)
class EpochFloor:
    """The fence reads the domain's next-epoch choice needs."""

    history_max: int  # highest bh_writer.epoch ever committed on main (0 when none)
    writer: int | None  # live bh_writer.epoch (None: not cut over)


@dataclass(frozen=True)
class ReclaimNote:
    """The audit comment a reclaim leaves on one bead, inside the bump commit (M14 D5a).

    The caller mints ``comment_id`` and stamps ``created_at`` (aware UTC, whole seconds are
    stored); the store writes them as given."""

    bead_id: str
    comment_id: str
    author: str
    text: str
    created_at: datetime


@dataclass(frozen=True)
class ReclaimSet:
    """The failover reclaim that rides inside the bump commit (writer-partitioning condition 7):
    in_progress beads claimed by ``dead_frame`` that the bump reverts. The one bead-data write
    the port carries (operator-accepted scope exception, bh-xxkf7).

    Every bead in ``bead_ids`` that is still ``in_progress`` is left exactly as ``bd unclaim
    --force`` leaves it (``open``, no assignee, no ``started_at``) with its ``claim-frame``
    label cleared. A bead also in ``resumable`` has every ``recovery:*`` label replaced by
    ``recovery:resumable``, and each ``notes`` entry adds its audit comment. Every write is
    guarded on ``in_progress``, so a bead that moved on is never touched. ``resumable`` and
    every note's bead must be members of ``bead_ids`` (settled by bh-jhgum)."""

    dead_frame: str
    bead_ids: frozenset[str]
    resumable: frozenset[str] = frozenset()
    notes: tuple[ReclaimNote, ...] = ()


@dataclass(frozen=True)
class BumpRequest:
    """One fence bump: the writer row, the implied ``adopt-<epoch>`` sentinel and the optional
    reclaim, committed as ONE commit (conditions 1, 2 and 7)."""

    frame: str
    epoch: int
    revision: str
    operation: OperationId
    reclaim: ReclaimSet | None = None


@dataclass(frozen=True)
class GuardReport:
    """``bh_*`` guard-trigger conformance."""

    expected: int
    present: int
    missing: tuple[str, ...]


CLAIM_FRAME_LABEL_PREFIX: Final[str] = "claim-frame:"
"""Root ``work_backup.CLAIM_FRAME_DIMENSION`` as a label prefix: a reclaim clears it."""
RECOVERY_LABEL_PREFIX: Final[str] = "recovery:"
"""Root ``work_backup.RECOVERY_DIMENSION`` as a label prefix: a resumable reclaim replaces it."""
RESUMABLE_LABEL: Final[str] = "recovery:resumable"
"""The label a resumable reclaim leaves (root ``work_backup.RESUMABLE``)."""

ADOPT_SENTINEL_PREFIX: Final[str] = "adopt-"
"""The bump witness: the ``adopt-<epoch>`` mark id (condition 1)."""


def adopt_sentinel(epoch: int) -> str:
    """The sentinel mark id a bump to ``epoch`` writes."""
    return f"{ADOPT_SENTINEL_PREFIX}{int(epoch)}"
