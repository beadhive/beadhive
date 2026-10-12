"""Director placement in ``dolt-server`` HQ (bh-a94qw, P-M8; ADR §1 design A, conditions 5-6).

``docs/design/hive-writer-partitioning-adr.md`` §1: placement — *who should write* a hive — is
one ``hq_live_hive_leases`` row per hive, changed only by a compare-and-swap issued with a
director/operator credential that holds ``UPDATE`` on that table and no other write right.
Frames hold ``SELECT`` on it.

Storage is the HQ storage port (``docs/design/hq-storage-port-adr.md``, bh-sqfvq): the row is a
``RecordKind.PLACEMENT`` record, read and compare-and-set through ``VersionedRecords``. The Dolt
adapter (``beadhive_hq_storage_dolt``) owns every SQL string and the row's wire encoding; root
composes it in :mod:`beadhive.hq_storage_composition`. Rules this module enforces:

* **Every write rewrites the CAS token** (condition 5, ``bh-cvk70`` E6). Dolt merges concurrent
  transactions cell by cell, so a write that kept ``revision`` could commit silently over a
  competing CAS. The port mints a fresh revision on every write, in the receiver's 64-hex
  format, so the trusted receiver can take the row over again on a Φ3 rollback and a director
  can take over a receiver-written row.
* **A port ``Conflict`` means lost** (:class:`PlacementLost`) — ``rowcount 0`` or a ``1213``
  serialization failure, which the port deliberately does not tell apart — never retried with
  the same expectation. An ``Unknown`` outcome is looked up by the write's ``OperationId``:
  ``Confirmed`` is the write; anything else is :class:`PlacementUnknown` — read back, never
  re-issue (``NotConfirmed`` never authorises a retry).
* **The row keeps the receiver's shape.** ``lease_json`` is the canonical
  ``{"authority": <frame identity>, "lease": <five-field HostLease record>}`` the receiver writes
  and parses (``SqlTrustedReceiver._read_prior``), so the receiver keeps working against
  director-written rows. The 0.22.8 bridge also needs the row's epoch to equal
  ``refs/bh/epoch`` (:mod:`beadhive.hq_signed_hive_lease`); the director places at an explicit
  epoch for that reason (:meth:`SqlPlacementDirector.place`). The port body is that carrier plus
  ``director`` and an optional ``cause``; the adapter stores only the carrier in ``lease_json``.
* **A director-written row is self-identifying** without a schema change: its
  ``request_sha256`` is :func:`placement_witness` over the row's own prefix, revision and record,
  which no receiver-accepted request digest can equal. That is the data switch that retires the
  0.22.8 proposal resolver for the hive (``hq_control_plane``): only the director and the
  receiver can write the row, so the witness is as trustworthy as the grants. A port read
  reports it as the body's ``director``.
* **A director row carries its cause** (bh-16347.6) — ``failover`` when the director's
  :class:`~beadhive.failover_observer.FailoverDirector` placed because the observer declared the
  placed frame dead, ``planned`` for an operator placement — in the ``request_id`` column of the
  same CAS, never in ``lease_json``: the carrier and the five-field lease stay exactly the
  receiver's, which the receiver, 0.22.x readers and the 0.22.8 bridge parse strictly. The
  column is ``CHAR(36)``. :func:`cause_token` is a UUID-shaped digest bound to the row's prefix
  and fresh revision, so it cannot be copied from an earlier row; :func:`placement_cause` reads
  it back and anything else — a pre-0.23.0 director row's random UUID, a receiver row — is
  ``None`` (unknown). A frame that does not read the cause (any 0.22.x, or an older 0.23
  build) adopts with the kind unknown, which writes no reclaim: fail safe, never a rewind.
* **No trigger may sit on a frame-writable HQ table** (condition 6, ``bh-cvk70`` E7d) and frames
  never hold a write right on placement: :func:`check_triggers` and :func:`check_grants` are the
  conformance checks (:func:`conformance` runs them against a server).

The director never INSERTs: a never-placed hive gets its row from operator provisioning
(:func:`seed_statement`), so the director credential stays ``UPDATE``-only on that table (the
port refuses a ``PLACEMENT`` CAS from ``ABSENT``).

Until ``bh-xnn5p`` moves the signed-authority read, the failover policy and session staleness
onto the port, the director shares one transaction with them through the adapter's transitional
``DoltHqSessions`` capability, so a survey stays ONE read and ``failover_after`` stays in the
placement CAS's own transaction. On storage without it (the in-memory fake), those cursor
consumers are absent: a survey carries no failover policy and ``failover_after`` is refused.

Pure where it can be; storage only through the port. Typer-free.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from beadhive_hq_storage import (
    ABSENT,
    Applied,
    Confirmed,
    Conflict,
    OperationId,
    RecordKind,
    Revision,
    StorageError,
    Unknown,
)

from .host_lease_contracts import HostLease, _parse_stamp, now_stamp

__all__ = [
    "CAS_LOST",
    "DEFAULT_TENURE_S",
    "PLACEMENT_DOMAIN",
    "PLACEMENT_TABLE",
    "POLICY_WRITES",
    "PROTECTED_TABLES",
    "SERIALIZATION_FAILURE",
    "CAUSE_DOMAIN",
    "CAUSE_FAILOVER",
    "CAUSE_PLANNED",
    "PLACEMENT_CAUSES",
    "Grant",
    "PlacementError",
    "PlacementLost",
    "PlacementRecord",
    "PlacementUnknown",
    "PlacementUnseeded",
    "SqlPlacementDirector",
    "Survey",
    "check_grants",
    "cause_token",
    "check_triggers",
    "conformance",
    "fresh_revision",
    "is_director_row",
    "lease_body",
    "parse_grant",
    "parse_row",
    "place_cas",
    "placement_body",
    "placement_cause",
    "placement_witness",
    "read_row",
    "read_rows",
    "record_from",
    "seed_statement",
]

#: The one placement row per hive (design A). Kept from the receiver era: the receiver reads and
#: CASes the same row, which is what makes the Φ3 rollback a non-event.
PLACEMENT_TABLE = "hq_live_hive_leases"
#: Domain of :func:`placement_witness` — the marker of a director-written row.
PLACEMENT_DOMAIN = "beadhive/sql-placement/v1"
#: Domain of :func:`cause_token` — the placement cause carried in a director row's
#: ``request_id`` (bh-16347.6).
CAUSE_DOMAIN = "beadhive/sql-placement-cause/v1"
#: The director's failover loop placed because the observer declared the placed frame dead:
#: the adopting frame runs M3's failover reclaim (D5a).
CAUSE_FAILOVER = "failover"
#: An operator (or any non-failover) placement: a planned handoff, reclaim applies nothing (D5c).
CAUSE_PLANNED = "planned"
PLACEMENT_CAUSES = (CAUSE_FAILOVER, CAUSE_PLANNED)
#: MySQL/Dolt ``ER_LOCK_DEADLOCK``: Dolt's serialization failure at commit. A lost CAS. The
#: adapter classifies it (a port ``Conflict``); kept for the readers that name it.
SERIALIZATION_FAILURE = 1213
#: The reason a :class:`PlacementLost` from a port ``Conflict`` carries: the port reports a
#: ``rowcount 0`` and a ``1213`` serialization failure alike (hq-storage-port-adr §2, condition 5).
CAS_LOST = "rowcount 0 or 1213 serialization failure"
#: Default tenure stamped into a director-written lease: the receiver's own 24 h bound. Expiry
#: is a failover hint, never a write gate (bh-12hev); a receiver-mode reader still treats an
#: elapsed lease as no holder, so on a Φ3 rollback the receiver's renewal keeps it current.
#: Configurable per call. The per-role ``failover_after`` lives beside the row (M8c, bh-4biq8):
#: :mod:`beadhive.failover_policy`.
DEFAULT_TENURE_S = 86400.0
#: The director's only other write surface: the failover policy beside the placement row
#: (bh-4biq8). It tunes when the director itself acts, never who may write a hive.
POLICY_WRITES = frozenset({"INSERT", "UPDATE", "DELETE"})

_HEX64 = re.compile(r"[0-9a-f]{64}")
_PREFIX = re.compile(r"[a-z][a-z0-9-]*")
_LEASE_FIELDS = frozenset({"host_id", "label", "epoch", "adopted_at", "expires_at"})
_BODY_FIELDS = frozenset({"authority", "lease", "director"})
_MAX_ROW_BYTES = 65536


def _schema_tables() -> frozenset[str]:
    from .hq_sql_runtime_schema import (
        COMMITTED_SCHEMA,
        FAILOVER_POLICY_SCHEMA,
        LIVENESS_POLICY_SCHEMA,
        PROTECTED_LIVE_SCHEMA,
    )

    names = set()
    for statement in (
        *COMMITTED_SCHEMA,
        *PROTECTED_LIVE_SCHEMA,
        *LIVENESS_POLICY_SCHEMA,
        *FAILOVER_POLICY_SCHEMA,
    ):
        match = re.match(r"CREATE TABLE (\w+)", statement)
        if match:
            names.add(match.group(1))
    return frozenset(names)


#: Every operator- or receiver-owned HQ runtime table. A frame may hold SELECT on some of them
#: and no write right on any; no trigger may touch one.
PROTECTED_TABLES: frozenset[str] = _schema_tables()


# =============================================================================================
# Errors
# =============================================================================================


class PlacementError(ValueError):
    """Placement refused or unavailable; nothing was written."""


class PlacementUnseeded(PlacementError):
    """The hive has no placement row. The director holds UPDATE only: the operator seeds the
    row (:func:`seed_statement`) before the first placement."""


class PlacementUnknown(PlacementError):
    """The CAS was sent but its outcome is unknown, and the port's lookup by the write's
    ``OperationId`` confirmed nothing. That is not proof it did not apply (a later write may
    have superseded it): read the row back. Never re-issue with the same expectation, and never
    mint a new operation for the same intent (hq-storage-port-adr §2, rule 3)."""

    def __init__(self, prefix: str, expected: str, operation: str, reason: str = "commit"):
        self.prefix, self.expected = prefix, expected
        self.operation, self.reason = operation, reason
        super().__init__(
            f"placement CAS for {prefix} sent but its commit acknowledgment is unknown "
            f"({reason}; expected revision {expected}, operation {operation}) and no confirmed "
            "write is visible; read the row back — never re-issue with the same expectation"
        )


class PlacementLost(PlacementError):
    """The placement CAS lost (condition 5): ``rowcount 0``, a ``1213`` serialization failure,
    or the row moved since it was read. Nothing was written. Never retried with the same
    expectation; re-read and decide again."""

    def __init__(self, prefix: str, expected: str, reason: str):
        self.prefix, self.expected, self.reason = prefix, expected, reason
        super().__init__(
            f"placement CAS for {prefix} lost ({reason}; expected revision {expected or '-'}); "
            "re-read placement and decide again — not retried with the same expectation"
        )


# =============================================================================================
# The row
# =============================================================================================


@dataclass(frozen=True)
class PlacementRecord:
    """One ``hq_live_hive_leases`` row, parsed. ``director`` marks a director-written row."""

    prefix: str
    revision: str
    authority: dict
    lease: HostLease
    request_id: str
    request_sha256: str
    director: bool
    #: ``failover`` / ``planned`` on a director row that carries its cause; ``None`` when
    #: unknown (a receiver row, or a director row written before the cause existed).
    cause: str | None = None

    @property
    def failover(self) -> bool | None:
        """The adopt kind this placement implies: ``True`` for a failover placement, ``False``
        for a planned one, ``None`` when the cause is unknown (no reclaim, fail safe)."""
        if self.cause == CAUSE_FAILOVER:
            return True
        if self.cause == CAUSE_PLANNED:
            return False
        return None

    @property
    def frame_id(self) -> str:
        """The placed frame (``authority.frame_id``), ``""`` for a tombstone or unknown."""
        if self.lease.is_tombstone:
            return ""
        value = self.authority.get("frame_id") if isinstance(self.authority, dict) else None
        return value if isinstance(value, str) else ""

    def describe(self) -> str:
        who = self.lease.host_id or "released"
        source = "director" if self.director else "receiver"
        return f"{who}@{self.lease.epoch} ({source} row, revision {self.revision[:12]})"


def fresh_revision(record: Mapping) -> str:
    """``sha256(canonical(record) ‖ uuid4)`` — fresh on every write, never a repeatable content
    hash, and in the receiver's 64-hex format (condition 5; Φ3 rollback)."""
    from .hq_sql_signatures import canonical

    return hashlib.sha256(canonical(dict(record)) + uuid.uuid4().hex.encode()).hexdigest()


def lease_body(authority: Mapping, lease: HostLease) -> bytes:
    """The row's ``lease_json``: exactly the receiver's carrier shape."""
    from .hq_sql_signatures import canonical

    return canonical({"authority": dict(authority), "lease": lease.to_record()})


def placement_witness(prefix: str, revision: str, body: bytes) -> str:
    """The ``request_sha256`` of a director-written row: a digest over the row's own content
    under :data:`PLACEMENT_DOMAIN`. A receiver row carries the digest of a signed frame request
    instead, which cannot equal this."""
    from .hq_sql_signatures import canonical

    return hashlib.sha256(
        canonical(
            {
                "domain": PLACEMENT_DOMAIN,
                "prefix": prefix,
                "revision": revision,
                "lease_sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    ).hexdigest()


def cause_token(prefix: str, revision: str, cause: str) -> str:
    """The ``request_id`` of a director row placed for `cause`: a UUID-shaped (36-char, fits the
    receiver's ``CHAR(36)`` column) digest under :data:`CAUSE_DOMAIN` over the row's prefix, its
    fresh revision and the cause. Bound to the revision, so it never survives into another row."""
    from .hq_sql_signatures import canonical

    if cause not in PLACEMENT_CAUSES:
        raise PlacementError(f"placement cause must be one of {', '.join(PLACEMENT_CAUSES)}")
    digest = hashlib.sha256(
        canonical({"domain": CAUSE_DOMAIN, "prefix": prefix, "revision": revision, "cause": cause})
    ).hexdigest()[:32]
    # RFC 9562 version 8 (custom) with the RFC variant, spelled by hand: Python 3.11's
    # ``uuid.UUID(version=...)`` accepts only 1-5.
    h = digest[:12] + "8" + digest[13:16] + "89ab"[int(digest[16], 16) & 3] + digest[17:]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def placement_cause(prefix: str, revision: str, request_id) -> str | None:
    """The cause a director row's ``request_id`` carries, or ``None`` (unknown). Never raises."""
    if not isinstance(request_id, str) or not isinstance(revision, str):
        return None
    for cause in PLACEMENT_CAUSES:
        try:
            if request_id == cause_token(prefix, revision, cause):
                return cause
        except Exception:  # noqa: BLE001 - an unhashable row is simply "unknown"
            return None
    return None


def _bytes(value) -> bytes | None:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, str):
        value = value.encode()
    return value if isinstance(value, bytes) else None


def _lease(raw) -> HostLease:
    if (
        not isinstance(raw, dict)
        or set(raw) != _LEASE_FIELDS
        or any(type(raw[k]) is not str for k in ("host_id", "label", "adopted_at", "expires_at"))
        or type(raw["epoch"]) is not int
        or raw["epoch"] < 1
        or _parse_stamp(raw["adopted_at"]) <= 0
        or _parse_stamp(raw["expires_at"]) <= 0
    ):
        raise PlacementError("placement row lease record invalid")
    return HostLease(**raw)


def is_director_row(prefix: str, row: Sequence) -> bool:
    """Whether a raw ``(revision, lease_json, request_id, request_sha256)`` row is
    director-written (its witness verifies). Never raises."""
    try:
        revision, body, _request_id, request_sha = row
        body = _bytes(body)
        return (
            body is not None
            and isinstance(revision, str)
            and request_sha == placement_witness(prefix, revision, body)
        )
    except (ValueError, TypeError):
        return False


def parse_row(prefix: str, row: Sequence) -> PlacementRecord:
    """Parse and validate a raw row the way the receiver does (canonical carrier, exact
    five-field lease). Raises :class:`PlacementError` on any deviation."""
    from .hq_sql_signatures import SqlSignatureError, canonical

    try:
        revision, body, request_id, request_sha = row
    except (ValueError, TypeError):
        raise PlacementError("placement row shape invalid") from None
    body = _bytes(body)
    if body is None or len(body) > _MAX_ROW_BYTES or not isinstance(revision, str):
        raise PlacementError("placement row carrier invalid")
    try:
        envelope = json.loads(body)
        if body != canonical(envelope) or set(envelope) != {"authority", "lease"}:
            raise PlacementError("placement row carrier invalid")
    except (ValueError, SqlSignatureError):
        raise PlacementError("placement row carrier invalid") from None
    if not isinstance(envelope["authority"], dict):
        raise PlacementError("placement row authority invalid")
    director = is_director_row(prefix, (revision, body, request_id, request_sha))
    return PlacementRecord(
        prefix=prefix,
        revision=revision,
        authority=envelope["authority"],
        lease=_lease(envelope["lease"]),
        request_id=str(request_id),
        request_sha256=str(request_sha),
        director=director,
        cause=placement_cause(prefix, revision, request_id) if director else None,
    )


def seed_statement(prefix: str, *, epoch: int, at: float | None = None) -> tuple[str, tuple]:
    """Operator DML seeding a never-placed hive's row as a released tombstone at `epoch`.

    Run by the operator's provisioning account (the director has no INSERT). Seed at the hive's
    current ``refs/bh/epoch`` (1 when it was never fenced) so the first placement raises it.
    Root mints the row; the statement text is the Dolt adapter's
    (:func:`beadhive_hq_storage_dolt.placement_seed_statement`)."""
    from beadhive_hq_storage_dolt import placement_seed_statement

    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise PlacementError("invalid hive prefix")
    if type(epoch) is not int or epoch < 1:
        raise PlacementError("seed epoch must be a positive integer")
    stamp = now_stamp(at)
    lease = HostLease(host_id="", label="", epoch=epoch, adopted_at=stamp, expires_at=stamp)
    body = lease_body({}, lease)
    revision = fresh_revision({"prefix": prefix, "lease": lease.to_record(), "seed": True})
    return placement_seed_statement(prefix, revision, body, str(uuid.uuid4()))


# =============================================================================================
# The record on the port
# =============================================================================================


def placement_body(authority: Mapping, lease: HostLease, *, cause: str | None = None) -> dict:
    """The port body of a director write: the receiver's carrier, ``director`` and the cause.

    The Dolt adapter stores only ``{"authority", "lease"}`` in ``lease_json`` (:func:`lease_body`)
    and the cause in ``request_id`` (:func:`cause_token`); the fake keeps the body as given."""
    body = {"authority": dict(authority), "lease": lease.to_record(), "director": True}
    if cause is not None:
        body["cause"] = cause
    return body


def record_from(prefix: str, record) -> PlacementRecord:
    """Parse a port ``PLACEMENT`` record (``Versioned``) the way :func:`parse_row` parses a raw
    row: the exact carrier, a five-field lease, ``director`` from the witness. Raises
    :class:`PlacementError` on any deviation (the adapter reads a non-canonical carrier without
    ``director``, so it is refused here).

    ``request_id`` and ``request_sha256`` are reconstructed from the record (the port does not
    carry the raw columns): the cause token, and the witness of a director row."""
    body = getattr(record, "body", None)
    if (
        not isinstance(body, Mapping)
        or not _BODY_FIELDS <= set(body)
        or not set(body) <= _BODY_FIELDS | {"cause"}
        or type(body["director"]) is not bool
    ):
        raise PlacementError("placement row carrier invalid")
    revision = getattr(record, "revision", None)
    if not isinstance(revision, str):
        raise PlacementError("placement row carrier invalid")
    authority = body["authority"]
    if not isinstance(authority, dict):
        raise PlacementError("placement row authority invalid")
    lease = _lease(body["lease"])
    director = body["director"]
    cause = body.get("cause") if director else None
    if cause is not None and cause not in PLACEMENT_CAUSES:
        raise PlacementError("placement row cause invalid")
    return PlacementRecord(
        prefix=prefix,
        revision=revision,
        authority=dict(authority),
        lease=lease,
        request_id=cause_token(prefix, revision, cause) if cause is not None else "",
        request_sha256=(
            placement_witness(prefix, revision, lease_body(authority, lease)) if director else ""
        ),
        director=director,
        cause=cause,
    )


def read_row(records, prefix: str) -> PlacementRecord | None:
    """The current placement row through the port (`records`: ``VersionedRecords``), or ``None``.
    A storage failure propagates as the port's ``StorageError``."""
    _check_prefix(prefix)
    record = records.read(RecordKind.PLACEMENT, prefix)
    return None if record is None else record_from(prefix, record)


def read_rows(records: Iterable) -> tuple[dict[str, PlacementRecord], dict[str, str]]:
    """Every placement record of a ``read_many``: ``({prefix: record}, {prefix: reason})``. A row
    that does not parse is reported, never placed from or failed over."""
    placed, invalid = {}, {}
    for record in records:
        prefix = getattr(record, "key", None)
        try:
            _check_prefix(prefix)
            placed[prefix] = record_from(prefix, record)
        except PlacementError as exc:
            invalid[str(prefix)] = str(exc) or "placement row invalid"
    return placed, invalid


def _check_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise PlacementError("invalid hive prefix")


# =============================================================================================
# The CAS
# =============================================================================================


def _sessions(records):
    """The adapter's transitional ``DoltHqSessions`` capability, or ``None`` (the fake)."""
    from beadhive_hq_storage_dolt import DoltHqSessions

    capability = getattr(records, "capability", None)
    return None if capability is None else capability(DoltHqSessions)


def _placed(prefix, revision, authority, lease, cause, operation) -> PlacementRecord:
    return PlacementRecord(
        prefix=prefix,
        revision=str(revision),
        authority=dict(authority),
        lease=lease,
        request_id=operation if cause is None else cause_token(prefix, revision, cause),
        request_sha256=placement_witness(prefix, revision, lease_body(authority, lease)),
        director=True,
        cause=cause,
    )


def place_cas(
    records,
    *,
    prefix: str,
    lease: HostLease,
    authority: Mapping,
    expected_revision: str,
    cause: str | None = None,
    failover_after: Mapping[str, int | None] | None = None,
    operation: str | None = None,
) -> PlacementRecord:
    """Move `prefix`'s placement row to `lease` iff it is still at `expected_revision`.

    `records` is the HQ storage port (``VersionedRecords``). `cause` (``failover`` /
    ``planned``) rides in the same CAS (the adapter's ``request_id`` :func:`cause_token`);
    ``None`` (a release) carries none, which reads as unknown. `failover_after` (``{role:
    seconds | None}``) also sets the hive's failover policy in the same transaction
    (:func:`beadhive.failover_policy.apply_changes`; refused, never clamped) — through the
    adapter's transitional ``DoltHqSessions`` capability, so it is refused on storage without
    one. `operation` is the write's ``OperationId`` (minted when ``None``).

    Read the row, check it is the expected one and that the epoch rule holds (a placement
    raises the epoch; a release keeps it as a tombstone), then the single guarded CAS with a
    fresh revision and the director witness. A ``Conflict`` raises :class:`PlacementLost`;
    nothing is retried. An ``Unknown`` is looked up by `operation`: ``Confirmed`` returns the
    write, anything else raises :class:`PlacementUnknown`. Raises :class:`PlacementUnseeded`
    when the row does not exist."""
    _check_prefix(prefix)
    if not isinstance(expected_revision, str) or not _HEX64.fullmatch(expected_revision):
        raise PlacementError("expected placement revision must be the row's 64-hex revision")
    if cause is not None and cause not in PLACEMENT_CAUSES:
        raise PlacementError(f"placement cause must be one of {', '.join(PLACEMENT_CAUSES)}")
    if cause is not None and lease.is_tombstone:
        raise PlacementError("a release carries no placement cause")
    operation = OperationId(operation or str(uuid.uuid4()))
    expected = Revision(expected_revision)
    body = placement_body(authority, lease, cause=cause)

    def check(found) -> None:
        if found is None:
            raise PlacementUnseeded(
                f"hive {prefix} has no placement row; the operator seeds it first "
                "(the director credential holds UPDATE only)"
            )
        current = record_from(prefix, found)
        if current.revision != expected_revision:
            raise PlacementLost(prefix, expected_revision, "the row moved since it was read")
        if lease.is_tombstone:
            if lease.epoch != current.lease.epoch:
                raise PlacementError("a release keeps the placed epoch")
        elif lease.epoch <= current.lease.epoch:
            raise PlacementError(
                f"placement must raise the epoch (row at {current.lease.epoch}, "
                f"asked {lease.epoch})"
            )

    try:
        if failover_after:
            outcome = _cas_with_policy(
                records, prefix, body, expected, operation, check, failover_after
            )
        else:
            check(records.read(RecordKind.PLACEMENT, prefix))
            outcome = records.compare_and_set(
                RecordKind.PLACEMENT, prefix, body, expected=expected, operation=operation
            )
    except PlacementError:
        raise
    except Exception:  # noqa: BLE001 - a StorageError, or anything raw; its text never surfaces
        raise PlacementError(f"placement CAS for {prefix} unavailable; nothing written") from None
    if isinstance(outcome, Applied):
        return _placed(prefix, outcome.revision, authority, lease, cause, operation)
    if isinstance(outcome, Conflict):
        raise PlacementLost(prefix, expected_revision, CAS_LOST)
    reason = outcome.reason if isinstance(outcome, Unknown) else "commit"
    try:
        found = records.lookup(RecordKind.PLACEMENT, prefix, operation)
    except StorageError:
        found = None
    if not isinstance(found, Confirmed):
        raise PlacementUnknown(prefix, expected_revision, operation, reason)
    return _placed(prefix, found.revision, authority, lease, cause, operation)


def _cas_with_policy(records, prefix, body, expected, operation, check, failover_after):
    """The CAS and the failover-policy rows in ONE transaction (transitional; ``bh-xnn5p``)."""
    from .failover_policy import FailoverPolicyError, apply_changes

    sessions = _sessions(records)
    if sessions is None:
        raise PlacementError(
            "failover_after rides the placement CAS's own transaction, which needs dolt-server "
            "HQ storage; nothing written"
        )

    def body_fn(tx):
        check(tx.read(RecordKind.PLACEMENT, prefix))
        try:
            apply_changes(tx.cursor, prefix, failover_after)
        except FailoverPolicyError as exc:
            raise PlacementError(str(exc)) from None
        return tx.compare_and_set(
            RecordKind.PLACEMENT, prefix, body, expected=expected, operation=operation
        )

    return sessions.transact(
        body_fn, operation=operation, expected=expected, passthrough=(PlacementError,)
    )


# =============================================================================================
# The director
# =============================================================================================


@dataclass(frozen=True)
class Survey:
    """One verified read of placement (:meth:`SqlPlacementDirector.survey`)."""

    state: Mapping
    policies: Mapping
    placements: Mapping[str, PlacementRecord]
    invalid: Mapping[str, str]
    observed: object = None
    #: The validated failover policy (:class:`beadhive.failover_policy.FailoverPolicy`).
    failover: object = None


class _PortSession:
    """A survey/verify session over storage without ``DoltHqSessions`` (the fake): port reads,
    and no cursor for the consumers not yet on the port."""

    cursor = None

    def __init__(self, records):
        self._records = records

    def head(self):
        return None

    def read(self, kind, key):
        return self._records.read(kind, key)

    def read_many(self, kind, keys=None):
        return self._records.read_many(kind, keys)


class SqlPlacementDirector:
    """The director/operator side of SQL placement, bound by ``placement_writer`` settings.

    The credential holds ``UPDATE`` on :data:`PLACEMENT_TABLE` and ``SELECT`` on what it must
    verify (the signed authority and registry); no other write right (:func:`check_grants`).
    The placed frame's identity comes from the operator-signed authority, verified exactly as
    a frame verifies it, so the row names a real active grant the receiver also recognises.

    Storage is the HQ port: `storage` when given (the in-memory fake in tests), else the Dolt
    adapter composed over the ``placement_writer`` binding
    (:func:`beadhive.hq_storage_composition.placement_storage`, `connect` replacing the
    transport), whose every session checks it is that principal on that database's ``main``."""

    def __init__(
        self,
        settings,
        *,
        broker=None,
        clock=time.time,
        connect: Callable | None = None,
        storage=None,
    ):
        self.settings = settings
        self.broker = broker
        self.clock = clock
        self._connect = connect
        self._store = storage

    def _storage(self):
        binding = self.settings.get("placement_writer")
        if not binding or self.settings.get("runtime") is not None:
            raise PlacementError("separate placement writer capability unavailable")
        if self._store is None:
            from .hq_storage_composition import placement_storage

            self._store = placement_storage(binding, broker=self.broker, connect=self._connect)
        return self._store

    @contextmanager
    def _session(self) -> Iterator:
        """One read-only session: the adapter's single transaction, or port reads on the fake."""
        records = self._storage()
        sessions = _sessions(records)
        if sessions is None:
            yield _PortSession(records)
            return
        with sessions.snapshot() as session:
            yield session

    def _verified(self, session):
        """The operator-signed state and hive policies at the session's head."""
        from .hq_sql_runtime import SqlRuntimeAuthority

        if session.cursor is None:
            raise PlacementError("verified placement needs dolt-server HQ storage")
        authority = SqlRuntimeAuthority(self.settings, broker=self.broker, clock=self.clock)
        state, _crossref, policies = authority.verified_state_at(session.cursor, session.head())
        return state, policies

    def read(self, prefix: str) -> PlacementRecord | None:
        records = self._storage()
        try:
            return read_row(records, prefix)
        except StorageError as exc:
            raise PlacementError(f"placement read for {prefix} unavailable ({exc})") from None

    def survey(self, observe=None) -> Survey:
        """Every placement row plus the operator-signed state, in ONE verified read-only
        transaction. `observe(cursor, head, state)` runs inside the same transaction (the
        failover loop reads session staleness there); its result is :attr:`Survey.observed`.
        The failover policy is read in the same transaction (:attr:`Survey.failover`)."""
        from .failover_policy import read_policy

        self._storage()
        try:
            with self._session() as session:
                state, policies = self._verified(session)
                placements, invalid = read_rows(session.read_many(RecordKind.PLACEMENT))
                failover = None if session.cursor is None else read_policy(session.cursor)
                observed = None
                if observe is not None:
                    observed = observe(session.cursor, session.head(), state)
            return Survey(state, policies, placements, invalid, observed, failover)
        except PlacementError:
            raise
        except Exception:  # noqa: BLE001 - authority/transport failures never leak details
            raise PlacementError("verified placement survey unavailable") from None

    @staticmethod
    def placeable(state: Mapping, policies: Mapping, prefix: str, frame_id: str) -> dict:
        """The frame identity the row names, from the operator-signed state; refuses a frame
        that is not active, is cordoned, or holds no grant bound to the hive's signed policy."""
        entry = (state.get("frames") or {}).get(frame_id) or {}
        record = entry.get("active")
        if record is None or record.get("state") != "active" or record.get("cordoned"):
            raise PlacementError(
                f"PLACEMENT: frame {frame_id} has no active, uncordoned grant; nothing was placed"
            )
        policy = policies.get(prefix)
        if policy is None or policy.get("config_revision") != record["authority"].get(
            "config_revision"
        ):
            raise PlacementError(
                f"PLACEMENT: hive {prefix} has no operator-signed frame policy bound to frame "
                f"{frame_id}'s grant; nothing was placed"
            )
        return {"frame_id": frame_id, **record["authority"]}

    def place(
        self,
        prefix: str,
        *,
        frame_id: str,
        expected_revision: str,
        epoch: int | None = None,
        label: str | None = None,
        tenure_s: float = DEFAULT_TENURE_S,
        at: float | None = None,
        cause: str = CAUSE_PLANNED,
        failover_after: Mapping[str, int | None] | None = None,
    ) -> PlacementRecord:
        """Place `frame_id` on `prefix` at `epoch` (default: the row's epoch + 1), and set the
        hive's `failover_after` overrides in the same transaction when given.

        `cause` defaults to ``planned`` (an operator handoff: the adopting frame reclaims
        nothing). Only the director's failover loop passes ``failover``
        (:class:`beadhive.director_failover.SqlFailoverPorts`); it is not an operator flag.

        Pass the epoch the adopt will carry when ``refs/bh/epoch`` is ahead of the row
        (``max(refs/bh/epoch, placement, ...) + 1``), so the row's epoch equals the fence the
        frame then installs (0.22.8 bridge invariant)."""
        if not 0 < tenure_s <= DEFAULT_TENURE_S:
            raise PlacementError("placement tenure must be within (0, 86400] seconds")
        if cause not in PLACEMENT_CAUSES:
            raise PlacementError(f"placement cause must be one of {', '.join(PLACEMENT_CAUSES)}")
        records = self._storage()
        try:
            with self._session() as session:
                state, policies = self._verified(session)
                identity = self.placeable(state, policies, prefix, frame_id)
                row = session.read(RecordKind.PLACEMENT, prefix)
            if row is None:
                raise PlacementUnseeded(
                    f"hive {prefix} has no placement row; the operator seeds it first"
                )
            current = record_from(prefix, row)
            target = current.lease.epoch + 1 if epoch is None else epoch
            started = self.clock() if at is None else at
            lease = HostLease(
                host_id=identity["holder_identity"],
                label=label if label is not None else frame_id,
                epoch=target,
                adopted_at=now_stamp(started),
                expires_at=now_stamp(started + tenure_s),
            )
            return place_cas(
                records,
                prefix=prefix,
                lease=lease,
                authority=identity,
                expected_revision=expected_revision,
                cause=cause,
                failover_after=failover_after,
            )
        except PlacementError:
            raise
        except Exception:  # noqa: BLE001 - authority/transport failures never leak details
            raise PlacementError(f"verified placement for {prefix} unavailable") from None

    @staticmethod
    def _cursor(session):
        if session.cursor is None:
            raise PlacementError("the failover policy needs dolt-server HQ storage")
        return session.cursor

    def failover_policy(self, scope: str | None = None, changes: Mapping | None = None):
        """The validated failover policy (read-only; :mod:`beadhive.failover_policy`). With
        `changes`, the policy as it WOULD load after them for `scope` — refused, never clamped,
        exactly as :meth:`set_failover_policy` would refuse — writing nothing."""
        from .failover_policy import FLEET_SCOPE, FailoverPolicyError, plan_changes, read_policy

        self._storage()
        try:
            with self._session() as session:
                cursor = self._cursor(session)
                if changes:
                    return plan_changes(cursor, scope or FLEET_SCOPE, changes)
                return read_policy(cursor)
        except FailoverPolicyError as exc:
            raise PlacementError(str(exc)) from None
        except PlacementError:
            raise
        except Exception:  # noqa: BLE001 - transport failures never leak details
            raise PlacementError("failover policy read unavailable") from None

    def set_failover_policy(self, scope: str, changes: Mapping[str, int | None]):
        """Set (``seconds``) or clear (``None``) `scope`'s settings in one transaction;
        refused, never clamped, when a value would be refused on load. Returns the policy as
        it now loads. Never retried: a lost or unknown commit is reported; re-read and decide."""
        from .failover_policy import FailoverPolicyError, apply_changes

        sessions = _sessions(self._storage())
        if sessions is None:
            raise PlacementError("failover policy write unavailable; nothing written")
        operation = OperationId(str(uuid.uuid4()))
        written = []

        def body(tx):
            written.append(apply_changes(tx.cursor, scope, changes))
            return Applied(Revision(""), operation)

        try:
            outcome = sessions.transact(
                body,
                operation=operation,
                expected=ABSENT,
                passthrough=(FailoverPolicyError, PlacementError),
            )
        except FailoverPolicyError as exc:
            raise PlacementError(str(exc)) from None
        except PlacementError:
            raise
        except StorageError:
            raise PlacementError("failover policy write unavailable; nothing written") from None
        if isinstance(outcome, Conflict):
            raise PlacementLost(scope, "", "1213 serialization failure")
        if not isinstance(outcome, Applied):
            raise PlacementError(
                "failover policy commit acknowledgment unknown; read it back "
                "(`bh hq placement policy`) before deciding again"
            )
        return written[0]

    def release(
        self, prefix: str, *, expected_revision: str, at: float | None = None
    ) -> PlacementRecord:
        """Release `prefix`'s placement to a tombstone that keeps the epoch (planned handoff)."""
        current = self.read(prefix)
        if current is None:
            raise PlacementUnseeded(f"hive {prefix} has no placement row")
        if current.lease.is_tombstone:
            raise PlacementError(f"hive {prefix} placement is already released")
        stamp = now_stamp(self.clock() if at is None else at)
        tombstone = HostLease(
            host_id="",
            label="",
            epoch=current.lease.epoch,
            adopted_at=current.lease.adopted_at,
            expires_at=stamp,
        )
        return place_cas(
            self._storage(),
            prefix=prefix,
            lease=tombstone,
            authority=current.authority,
            expected_revision=expected_revision,
        )


# =============================================================================================
# Conformance: grants and triggers (conditions 5-6)
# =============================================================================================

_WRITE_PRIVILEGES = frozenset(
    {
        "ALL",
        "ALL PRIVILEGES",
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE",
        "DROP",
        "ALTER",
        "INDEX",
        "TRIGGER",
        "EXECUTE",
        "CREATE VIEW",
        "CREATE ROUTINE",
        "ALTER ROUTINE",
        "REFERENCES",
        "GRANT OPTION",
        "SUPER",
        "CREATE USER",
        "RELOAD",
        "SHUTDOWN",
        "PROCESS",
        "FILE",
        "EVENT",
        "LOCK TABLES",
        "CREATE TEMPORARY TABLES",
    }
)
_READ_PRIVILEGES = frozenset({"SELECT", "USAGE", "SHOW VIEW"})
_GRANT_LINE = re.compile(
    r"^GRANT (?P<privs>.+?) ON (?:(?:PROCEDURE|FUNCTION|TABLE) )?(?P<target>\S+) TO \S+"
    r"(?P<option> WITH GRANT OPTION)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Grant:
    """One ``SHOW GRANTS`` line, normalized. ``database``/``table`` are ``*`` for wildcards."""

    privileges: frozenset[str]
    database: str
    table: str
    grant_option: bool


def _unquote(part: str) -> str:
    return part.strip().strip("`'\"")


def parse_grant(line: str) -> Grant | None:
    """Parse one ``SHOW GRANTS`` line; ``None`` for a role grant or anything unrecognised
    (which :func:`check_grants` then reports, never ignores)."""
    match = _GRANT_LINE.match(line.strip())
    if match is None:
        return None
    target = match.group("target")
    database, _, table = target.partition(".")
    privileges = frozenset(
        re.sub(r"\s*\(.*?\)", "", p).strip().upper() for p in match.group("privs").split(",")
    )
    return Grant(
        privileges=privileges,
        database=_unquote(database),
        table=_unquote(table) if table else "*",
        grant_option=bool(match.group("option")),
    )


def check_grants(
    lines: Iterable[str],
    *,
    role: str,
    database: str,
    writable: Iterable[str] = (),
) -> list[str]:
    """Violations of the placement grant shape for one principal's ``SHOW GRANTS`` lines.

    ``role="director"``: must hold UPDATE on ``<database>.hq_live_hive_leases``; may hold
    INSERT/UPDATE/DELETE on ``<database>.hq_live_failover_policy`` (bh-4biq8); may hold
    SELECT anywhere; holds no other write right anywhere and no grant option.

    ``role="frame"``: holds no write right on any protected HQ table, the database or the
    server; ``writable`` lists the frame-owned tables it may write (its inbox, and from M9 its
    session/evidence tables)."""
    if role not in ("director", "frame"):
        raise ValueError("role must be 'director' or 'frame'")
    allowed = set(writable)
    problems, can_update = [], False
    for line in lines:
        grant = parse_grant(line)
        if grant is None:
            problems.append(f"unrecognised grant: {line.strip()[:120]}")
            continue
        writes = grant.privileges - _READ_PRIVILEGES
        unknown = writes - _WRITE_PRIVILEGES
        if unknown:
            problems.append(f"unrecognised privileges {sorted(unknown)} on {grant.table}")
        if grant.grant_option:
            problems.append(f"grant option on {grant.database}.{grant.table}")
        if not writes:
            continue
        scope = f"{grant.database}.{grant.table}"
        if grant.database == "*" or grant.table == "*":
            problems.append(f"{role} holds {sorted(writes)} on {scope} (wider than a table)")
            continue
        if role == "director":
            if (
                grant.database == database
                and grant.table == PLACEMENT_TABLE
                and writes == {"UPDATE"}
            ):
                can_update = True
            elif (
                grant.database == database
                and grant.table == _policy_table()
                and writes <= POLICY_WRITES
            ):
                pass
            else:
                problems.append(
                    f"director holds {sorted(writes)} on {scope}; only UPDATE on "
                    f"{database}.{PLACEMENT_TABLE} (and INSERT/UPDATE/DELETE on "
                    f"{database}.{_policy_table()}) is allowed"
                )
        elif grant.table in PROTECTED_TABLES or grant.table not in allowed:
            problems.append(f"frame holds {sorted(writes)} on {scope}")
    if role == "director" and not can_update:
        problems.append(f"director lacks UPDATE on {database}.{PLACEMENT_TABLE}")
    return problems


def _policy_table() -> str:
    from .hq_sql_runtime_schema import FAILOVER_POLICY_TABLE

    return FAILOVER_POLICY_TABLE


_TRIGGER_DML = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|CALL)\b", re.IGNORECASE)


def check_triggers(
    triggers: Iterable[tuple[str, str, str]], *, frame_writable: Iterable[str] = ()
) -> list[str]:
    """Violations of condition 6 for ``(trigger_name, event_object_table, action_statement)``.

    A trigger fails when it sits ON a protected table, when its body names a protected table
    (trigger DML is a definer-like path into placement, ``bh-cvk70`` E7d), or when it sits on a
    frame-writable table and runs any DML at all. An operator ``BEFORE`` trigger that only
    stamps ``NEW`` columns (M9's ``renewed_at``) passes."""
    frame_tables = set(frame_writable)
    problems = []
    for name, table, body in triggers:
        text = body or ""
        named = sorted(t for t in PROTECTED_TABLES if re.search(rf"\b{re.escape(t)}\b", text))
        if table in PROTECTED_TABLES:
            problems.append(f"trigger {name} sits on protected table {table}")
        elif named:
            problems.append(f"trigger {name} on {table} touches protected {', '.join(named)}")
        elif (
            table in frame_tables
            or table.startswith("hq_live_inbox_")
            or re.fullmatch(r"frame_\w+_(session|evidence)", table)
        ) and _TRIGGER_DML.search(text):
            problems.append(f"trigger {name} on frame-writable {table} runs DML")
    return problems


def conformance(
    cursor,
    *,
    database: str,
    director: str,
    frames: Mapping[str, Iterable[str]],
) -> list[str]:
    """Run both checks against a server with an operator account that can read grants.

    `director` is ``'user'@'host'``; `frames` maps each frame account to the tables it may
    write. Returns every violation (empty means conformant). The reads are the Dolt adapter's
    (:func:`beadhive_hq_storage_dolt.placement_conformance_rows`) on the operator's `cursor`."""
    from beadhive_hq_storage_dolt import placement_conformance_rows

    tables = {account: tuple(writable) for account, writable in frames.items()}
    grants, triggers = placement_conformance_rows(
        cursor, accounts=[director, *tables], database=database
    )
    (_director, lines), *frame_grants = grants
    problems = [f"{director}: {p}" for p in check_grants(lines, role="director", database=database)]
    writable: set[str] = set()
    for account, lines in frame_grants:
        writable.update(tables[account])
        problems += [
            f"{account}: {p}"
            for p in check_grants(lines, role="frame", database=database, writable=tables[account])
        ]
    problems += check_triggers(triggers, frame_writable=writable)
    return problems
