"""Director placement in ``dolt-server`` HQ (bh-a94qw, P-M8; ADR §1 design A, conditions 5-6).

``docs/design/hive-writer-partitioning-adr.md`` §1: placement — *who should write* a hive — is
one ``hq_live_hive_leases`` row per hive, changed only by a compare-and-swap issued with a
director/operator credential that holds ``UPDATE`` on that table and no other write right.
Frames hold ``SELECT`` on it. The CAS is::

    UPDATE hq_live_hive_leases SET revision=<fresh>, lease_json=<record>, ...
    WHERE prefix=<prefix> AND revision=<expected>

Rules this module enforces:

* **Every write rewrites the CAS token** (condition 5, ``bh-cvk70`` E6). Dolt merges concurrent
  transactions cell by cell, so a write that kept ``revision`` could commit silently over a
  competing CAS. The token is :func:`fresh_revision` — ``sha256(record ‖ uuid)`` in the
  receiver's 64-hex format, so the trusted receiver can take the row over again on a Φ3
  rollback and a director can take over a receiver-written row.
* **``rowcount 0`` or a ``1213`` serialization failure means lost** (:class:`PlacementLost`),
  never retried with the same expectation. A commit whose acknowledgment is lost is
  :class:`PlacementUnknown`: read back, never re-issue.
* **The row keeps the receiver's shape.** ``lease_json`` is the canonical
  ``{"authority": <frame identity>, "lease": <five-field HostLease record>}`` the receiver writes
  and parses (``SqlTrustedReceiver._read_prior``), so the receiver keeps working against
  director-written rows. The 0.22.8 bridge also needs the row's epoch to equal
  ``refs/bh/epoch`` (:mod:`beadhive.hq_signed_hive_lease`); the director places at an explicit
  epoch for that reason (:meth:`SqlPlacementDirector.place`).
* **A director-written row is self-identifying** without a schema change: its
  ``request_sha256`` is :func:`placement_witness` over the row's own prefix, revision and record,
  which no receiver-accepted request digest can equal. That is the data switch that retires the
  0.22.8 proposal resolver for the hive (``hq_control_plane``): only the director and the
  receiver can write the row, so the witness is as trustworthy as the grants.
* **A director row carries its cause** (bh-16347.6) — ``failover`` when the director's
  :class:`~beadhive.failover_observer.FailoverDirector` placed because the observer declared the
  placed frame dead, ``planned`` for an operator placement — in the ``request_id`` column of the
  same CAS, never in ``lease_json``: the carrier and the five-field lease stay exactly the
  receiver's, which the receiver, 0.22.x readers and the 0.22.8 bridge parse strictly. The
  column is ``CHAR(36)``; the director already filled it with a random UUID nobody reads (the
  receiver only looks a director row's ``request_id`` up in its inbox, where it is absent
  either way). :func:`cause_token` is a UUID-shaped digest bound to the row's prefix and fresh
  revision, so it cannot be copied from an earlier row; :func:`placement_cause` reads it back
  and anything else — a pre-0.23.0 director row's random UUID, a receiver row — is
  ``None`` (unknown). A frame that does not read the cause (any 0.22.x, or an older 0.23
  build) adopts with the kind unknown, which writes no reclaim: fail safe, never a rewind.
* **No trigger may sit on a frame-writable HQ table** (condition 6, ``bh-cvk70`` E7d) and frames
  never hold a write right on placement: :func:`check_triggers` and :func:`check_grants` are the
  conformance checks (:func:`conformance` runs them against a server).

The director never INSERTs: a never-placed hive gets its row from operator provisioning
(:func:`seed_statement`), so the director credential stays ``UPDATE``-only on that table.

Pure where it can be; SQL only through a DB-API connection the caller provides or
:class:`SqlPlacementDirector` opens. Typer-free.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .host_lease_contracts import HostLease, _parse_stamp, now_stamp

__all__ = [
    "DEFAULT_TENURE_S",
    "PLACEMENT_DOMAIN",
    "PLACEMENT_TABLE",
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
    "placement_cause",
    "placement_witness",
    "read_row",
    "read_rows",
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
#: MySQL/Dolt ``ER_LOCK_DEADLOCK``: Dolt's serialization failure at commit. A lost CAS.
SERIALIZATION_FAILURE = 1213
#: Default tenure stamped into a director-written lease: the receiver's own 24 h bound. Expiry
#: is a failover hint, never a write gate (bh-12hev); a receiver-mode reader still treats an
#: elapsed lease as no holder, so on a Φ3 rollback the receiver's renewal keeps it current.
#: Configurable per call; the per-role ``failover_after`` lives on the row in M8c (bh-4biq8).
DEFAULT_TENURE_S = 86400.0

_HEX64 = re.compile(r"[0-9a-f]{64}")
_PREFIX = re.compile(r"[a-z][a-z0-9-]*")
_LEASE_FIELDS = frozenset({"host_id", "label", "epoch", "adopted_at", "expires_at"})
_MAX_ROW_BYTES = 65536


def _schema_tables() -> frozenset[str]:
    from .hq_sql_runtime_schema import (
        COMMITTED_SCHEMA,
        LIVENESS_POLICY_SCHEMA,
        PROTECTED_LIVE_SCHEMA,
    )

    names = set()
    for statement in (*COMMITTED_SCHEMA, *PROTECTED_LIVE_SCHEMA, *LIVENESS_POLICY_SCHEMA):
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
    """The CAS was sent but its commit acknowledgment was lost. Read the row back: it is either
    ``revision`` (won) or still ``expected`` / something else (lost). Never re-issue with the
    same expectation."""

    def __init__(self, prefix: str, expected: str, revision: str):
        self.prefix, self.expected, self.revision = prefix, expected, revision
        super().__init__(
            f"placement CAS for {prefix} sent but its commit acknowledgment is unknown "
            f"(expected revision {expected}, candidate revision {revision}); read the row back "
            "— never re-issue with the same expectation"
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
    current ``refs/bh/epoch`` (1 when it was never fenced) so the first placement raises it."""
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise PlacementError("invalid hive prefix")
    if type(epoch) is not int or epoch < 1:
        raise PlacementError("seed epoch must be a positive integer")
    stamp = now_stamp(at)
    lease = HostLease(host_id="", label="", epoch=epoch, adopted_at=stamp, expires_at=stamp)
    body = lease_body({}, lease)
    revision = fresh_revision({"prefix": prefix, "lease": lease.to_record(), "seed": True})
    return (
        f"INSERT INTO {PLACEMENT_TABLE} (prefix,revision,lease_json,request_id,request_sha256) "
        "VALUES (%s,%s,%s,%s,%s)",
        (prefix, revision, body, str(uuid.uuid4()), placement_witness(prefix, revision, body)),
    )


# =============================================================================================
# The CAS
# =============================================================================================


def _serialization_failure(exc: BaseException) -> bool:
    args = getattr(exc, "args", ())
    return bool(args) and args[0] == SERIALIZATION_FAILURE


def _rollback(connection) -> None:
    try:
        connection.rollback()
    except Exception:  # noqa: BLE001 - the transaction is abandoned either way
        pass


def _read_row(cursor, prefix: str):
    cursor.execute(
        f"SELECT revision,lease_json,request_id,request_sha256 FROM {PLACEMENT_TABLE} "
        "WHERE prefix=%s",
        (prefix,),
    )
    return cursor.fetchone()


def read_row(connection, prefix: str) -> PlacementRecord | None:
    """The current placement row on `connection` (one read-only transaction), or ``None``."""
    _check_prefix(prefix)
    try:
        with connection.cursor() as cursor:
            row = _read_row(cursor, prefix)
        return None if row is None else parse_row(prefix, row)
    finally:
        _rollback(connection)


def read_rows(cursor) -> tuple[dict[str, PlacementRecord], dict[str, str]]:
    """Every placement row on `cursor`'s open transaction: ``({prefix: record}, {prefix:
    reason})``. A row that does not parse is reported, never placed from or failed over."""
    cursor.execute(
        f"SELECT prefix,revision,lease_json,request_id,request_sha256 FROM {PLACEMENT_TABLE}"
    )
    records, invalid = {}, {}
    for prefix, *row in cursor.fetchall():
        prefix = _bytes(prefix).decode() if not isinstance(prefix, str) else prefix
        try:
            _check_prefix(prefix)
            records[prefix] = parse_row(prefix, row)
        except (PlacementError, UnicodeDecodeError, AttributeError) as exc:
            invalid[str(prefix)] = str(exc) or "placement row invalid"
    return records, invalid


def _check_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise PlacementError("invalid hive prefix")


def place_cas(
    connection,
    *,
    prefix: str,
    lease: HostLease,
    authority: Mapping,
    expected_revision: str,
    cause: str | None = None,
) -> PlacementRecord:
    """Move `prefix`'s placement row to `lease` iff it is still at `expected_revision`.

    `cause` (``failover`` / ``planned``) rides in the same UPDATE as the row's ``request_id``
    (:func:`cause_token`); ``None`` (a release) writes a random one, which reads as unknown.

    One transaction: read the row, check it is the expected one and that the epoch rule holds
    (a placement raises the epoch; a release keeps it as a tombstone), then the single guarded
    ``UPDATE`` with a fresh revision and the director witness. ``rowcount != 1`` or ``1213``
    raise :class:`PlacementLost`; nothing is retried. Raises :class:`PlacementUnseeded` when
    the row does not exist and :class:`PlacementUnknown` when the commit's fate is unknown."""
    _check_prefix(prefix)
    if not isinstance(expected_revision, str) or not _HEX64.fullmatch(expected_revision):
        raise PlacementError("expected placement revision must be the row's 64-hex revision")
    if cause is not None and cause not in PLACEMENT_CAUSES:
        raise PlacementError(f"placement cause must be one of {', '.join(PLACEMENT_CAUSES)}")
    if cause is not None and lease.is_tombstone:
        raise PlacementError("a release carries no placement cause")
    revision = fresh_revision({"prefix": prefix, "lease": lease.to_record()})
    body = lease_body(authority, lease)
    witness = placement_witness(prefix, revision, body)
    request_id = str(uuid.uuid4()) if cause is None else cause_token(prefix, revision, cause)
    committing = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION")
            row = _read_row(cursor, prefix)
            if row is None:
                _rollback(connection)
                raise PlacementUnseeded(
                    f"hive {prefix} has no placement row; the operator seeds it first "
                    "(the director credential holds UPDATE only)"
                )
            current = parse_row(prefix, row)
            if current.revision != expected_revision:
                _rollback(connection)
                raise PlacementLost(prefix, expected_revision, "the row moved since it was read")
            if lease.is_tombstone:
                if lease.epoch != current.lease.epoch:
                    _rollback(connection)
                    raise PlacementError("a release keeps the placed epoch")
            elif lease.epoch <= current.lease.epoch:
                _rollback(connection)
                raise PlacementError(
                    f"placement must raise the epoch (row at {current.lease.epoch}, "
                    f"asked {lease.epoch})"
                )
            matched = cursor.execute(
                f"UPDATE {PLACEMENT_TABLE} SET revision=%s,lease_json=%s,request_id=%s,"
                "request_sha256=%s WHERE prefix=%s AND revision=%s",
                (revision, body, request_id, witness, prefix, expected_revision),
            )
            if matched != 1:
                _rollback(connection)
                raise PlacementLost(prefix, expected_revision, f"rowcount {matched or 0}")
        committing = True
        connection.commit()
    except PlacementError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified below; driver text never surfaces
        _rollback(connection)
        if _serialization_failure(exc):
            raise PlacementLost(prefix, expected_revision, "1213 serialization failure") from None
        if committing:
            raise PlacementUnknown(prefix, expected_revision, revision) from None
        raise PlacementError(f"placement CAS for {prefix} unavailable; nothing written") from None
    return PlacementRecord(
        prefix=prefix,
        revision=revision,
        authority=dict(authority),
        lease=lease,
        request_id=request_id,
        request_sha256=witness,
        director=True,
        cause=cause,
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


class SqlPlacementDirector:
    """The director/operator side of SQL placement, bound by ``placement_writer`` settings.

    The credential holds ``UPDATE`` on :data:`PLACEMENT_TABLE` and ``SELECT`` on what it must
    verify (the signed authority and registry); no other write right (:func:`check_grants`).
    The placed frame's identity comes from the operator-signed authority, verified exactly as
    a frame verifies it, so the row names a real active grant the receiver also recognises."""

    def __init__(self, settings, *, broker=None, clock=time.time, connect=None):
        self.settings = settings
        self.broker = broker
        self.clock = clock
        self._connect = connect

    def _open(self):
        binding = self.settings.get("placement_writer")
        if not binding or self.settings.get("runtime") is not None:
            raise PlacementError("separate placement writer capability unavailable")
        if self._connect is not None:
            return self._connect(binding)
        from .hq_sql_transport import FnoxBroker, SqlTransportError, connect

        deadline = time.monotonic() + binding["operation_timeout"]
        try:
            return connect(binding, self.broker or FnoxBroker(), deadline=deadline)
        except SqlTransportError:
            raise PlacementError("verified placement writer connection unavailable") from None

    def _identity(self, cursor) -> None:
        binding = self.settings["placement_writer"]
        cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH()")
        user, database, branch = cursor.fetchone()
        if (
            not str(user).startswith(binding["user"] + "@")
            or database != binding["database"]
            or branch != "main"
        ):
            raise PlacementError("placement writer principal or endpoint mismatch")

    def _verified(self, cursor):
        from .hq_sql_runtime import SqlRuntimeAuthority

        cursor.execute("START TRANSACTION")
        cursor.execute("SELECT DOLT_HASHOF('HEAD')")
        head = cursor.fetchone()[0]
        authority = SqlRuntimeAuthority(self.settings, broker=self.broker, clock=self.clock)
        state, _crossref, policies = authority.verified_state_at(cursor, head)
        return state, policies

    def read(self, prefix: str) -> PlacementRecord | None:
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                self._identity(cursor)
            return read_row(connection, prefix)
        finally:
            connection.close()

    def survey(self, observe=None) -> Survey:
        """Every placement row plus the operator-signed state, in ONE verified read-only
        transaction. `observe(cursor, head, state)` runs inside the same transaction (the
        failover loop reads session staleness there); its result is :attr:`Survey.observed`."""
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                self._identity(cursor)
                state, policies = self._verified(cursor)
                placements, invalid = read_rows(cursor)
                observed = None
                if observe is not None:
                    cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                    observed = observe(cursor, cursor.fetchone()[0], state)
            return Survey(state, policies, placements, invalid, observed)
        except PlacementError:
            raise
        except Exception:  # noqa: BLE001 - authority/transport failures never leak details
            raise PlacementError("verified placement survey unavailable") from None
        finally:
            _rollback(connection)
            connection.close()

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
    ) -> PlacementRecord:
        """Place `frame_id` on `prefix` at `epoch` (default: the row's epoch + 1).

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
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                self._identity(cursor)
                state, policies = self._verified(cursor)
                identity = self.placeable(state, policies, prefix, frame_id)
                row = _read_row(cursor, prefix)
            _rollback(connection)
            if row is None:
                raise PlacementUnseeded(
                    f"hive {prefix} has no placement row; the operator seeds it first"
                )
            current = parse_row(prefix, row)
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
                connection,
                prefix=prefix,
                lease=lease,
                authority=identity,
                expected_revision=expected_revision,
                cause=cause,
            )
        except PlacementError:
            raise
        except Exception:  # noqa: BLE001 - authority/transport failures never leak details
            _rollback(connection)
            raise PlacementError(f"verified placement for {prefix} unavailable") from None
        finally:
            connection.close()

    def release(
        self, prefix: str, *, expected_revision: str, at: float | None = None
    ) -> PlacementRecord:
        """Release `prefix`'s placement to a tombstone that keeps the epoch (planned handoff)."""
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                self._identity(cursor)
                row = _read_row(cursor, prefix)
            _rollback(connection)
            if row is None:
                raise PlacementUnseeded(f"hive {prefix} has no placement row")
            current = parse_row(prefix, row)
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
                connection,
                prefix=prefix,
                lease=tombstone,
                authority=current.authority,
                expected_revision=expected_revision,
            )
        finally:
            connection.close()


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
            else:
                problems.append(
                    f"director holds {sorted(writes)} on {scope}; only UPDATE on "
                    f"{database}.{PLACEMENT_TABLE} is allowed"
                )
        elif grant.table in PROTECTED_TABLES or grant.table not in allowed:
            problems.append(f"frame holds {sorted(writes)} on {scope}")
    if role == "director" and not can_update:
        problems.append(f"director lacks UPDATE on {database}.{PLACEMENT_TABLE}")
    return problems


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
    write. Returns every violation (empty means conformant)."""
    problems: list[str] = []

    def grants(account: str) -> list[str]:
        cursor.execute(f"SHOW GRANTS FOR {account}")
        return [row[0] for row in cursor.fetchall()]

    problems += [
        f"{director}: {p}"
        for p in check_grants(grants(director), role="director", database=database)
    ]
    writable: set[str] = set()
    for account, tables in frames.items():
        tables = tuple(tables)
        writable.update(tables)
        problems += [
            f"{account}: {p}"
            for p in check_grants(grants(account), role="frame", database=database, writable=tables)
        ]
    cursor.execute(
        "SELECT trigger_name,event_object_table,action_statement FROM information_schema.triggers "
        "WHERE trigger_schema=%s",
        (database,),
    )
    problems += check_triggers(cursor.fetchall(), frame_writable=writable)
    return problems
