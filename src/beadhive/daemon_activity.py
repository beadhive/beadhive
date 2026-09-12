"""Durable, exact, idempotent activity storage for the host daemon.

The existing :mod:`beadhive.run_journal` writer is intentionally best effort because
observability must never change a local launch outcome.  Network publication has the opposite
acknowledgement contract: it may report success only after the fact is durable.  This module is
that separate boundary.  It stores the authoritative run registration, activity, and dedupe key
in one SQLite transaction and never publishes a live notification itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import run_journal
from .daemon_contract import (
    ActivityAppendRequest,
    ActivityAppendResponse,
    ActivityRecord,
    ActivityViewResponse,
    decode_hive_id,
    encode_hive_id,
    encode_run_id,
)
from .modules.agents.domain.profile import (
    BEAD_ID_PATTERN,
    KNOWN_SEATS,
    BeadPolicy,
    bead_policy_for_seat,
)

STORE_SCHEMA_VERSION = 1
EMPTY_REVISION = "opaque:empty"
DEFAULT_MAX_RECORD_BYTES = 262_144
DEFAULT_MAX_RECORDS_PER_READ = 1_000
DEFAULT_IDEMPOTENCY_RETENTION_SECONDS = 604_800.0
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0

_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_KIND = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_BEAD = re.compile(BEAD_ID_PATTERN)
_SOURCE_WRITERS = {
    "baml": frozenset({run_journal.WRITER_BAML}),
    "hitch": frozenset({run_journal.WRITER_HITCH}),
    "beadhive": frozenset({run_journal.WRITER_LOCAL_LOOP, run_journal.WRITER_ROLE}),
}


def source_writers(source: str) -> frozenset[str]:
    """Return the journal writers a trusted publisher source is allowed to own."""

    return _SOURCE_WRITERS.get(source, frozenset())


class ActivityStoreError(ValueError):
    """Stable refusal from the durable activity boundary.

    Messages contain only a bounded reason code so malformed publisher values cannot enter logs or
    HTTP errors through an exception string.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ActivitySourceCapability:
    source: Literal["baml", "hitch", "beadhive", "agentguides"]
    available: bool
    reason_code: str | None = None


def source_capabilities() -> tuple[ActivitySourceCapability, ...]:
    """Return exact v1 source support without probing or importing optional integrations."""

    return (
        ActivitySourceCapability("baml", True),
        ActivitySourceCapability("hitch", True),
        ActivitySourceCapability("beadhive", True),
        ActivitySourceCapability("agentguides", False, "unsupported_v1"),
    )


def _canonical_hive(value: str) -> bool:
    try:
        return decode_hive_id(encode_hive_id(value)) == value
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class ActivityRunIdentity:
    """Authoritative run registration supplied by the trusted launch/directory boundary."""

    run_id: str
    hive_id: str
    bead_id: str | None
    seat: str
    provider: str
    writer: str
    source: str
    manifest_digest: str

    def __post_init__(self) -> None:
        try:
            encode_run_id(self.run_id)
        except (TypeError, ValueError) as exc:
            raise ActivityStoreError("invalid_run_id") from exc
        if not _canonical_hive(self.hive_id):
            raise ActivityStoreError("invalid_hive_id")
        if self.seat not in KNOWN_SEATS:
            raise ActivityStoreError("invalid_seat")
        policy = bead_policy_for_seat(self.seat)
        if self.bead_id is not None and _BEAD.fullmatch(self.bead_id) is None:
            raise ActivityStoreError("invalid_bead")
        if policy is BeadPolicy.REQUIRED and self.bead_id is None:
            raise ActivityStoreError("invalid_bead")
        if policy is BeadPolicy.FORBIDDEN and self.bead_id is not None:
            raise ActivityStoreError("invalid_bead")
        if not isinstance(self.provider, str) or _PROVIDER.fullmatch(self.provider) is None:
            raise ActivityStoreError("invalid_provider")
        if self.writer not in run_journal.WRITERS:
            raise ActivityStoreError("invalid_writer")
        writers = _SOURCE_WRITERS.get(self.source)
        if writers is None:
            raise ActivityStoreError("unsupported_source")
        if self.writer not in writers:
            raise ActivityStoreError("source_writer_mismatch")
        if (
            not isinstance(self.manifest_digest, str)
            or _DIGEST.fullmatch(self.manifest_digest) is None
        ):
            raise ActivityStoreError("invalid_manifest_digest")

    def values(self) -> tuple[str | None, ...]:
        return (
            self.run_id,
            self.hive_id,
            self.bead_id,
            self.seat,
            self.provider,
            self.writer,
            self.source,
            self.manifest_digest,
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    hive_id TEXT NOT NULL,
    bead_id TEXT,
    seat TEXT NOT NULL,
    provider TEXT NOT NULL,
    writer TEXT NOT NULL,
    source TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    revision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    idempotency_key TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    appended_at INTEGER NOT NULL,
    revision TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activities_run_sequence ON activities(run_id, sequence);
CREATE TABLE IF NOT EXISTS idempotency (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    activity_id TEXT NOT NULL REFERENCES activities(activity_id),
    retain_until INTEGER NOT NULL,
    PRIMARY KEY (run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idempotency_retention ON idempotency(retain_until);
"""


class DurableActivityStore:
    """One transactionally durable activity and idempotency authority.

    Connections are intentionally request-scoped.  SQLite serializes writers across both threads
    and daemon processes, while ``BEGIN IMMEDIATE`` ensures the duplicate check and append are one
    atomic decision.  ``synchronous=FULL`` makes commit the acknowledgement boundary.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock_millis=lambda: time.time_ns() // 1_000_000,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        max_records_per_read: int = DEFAULT_MAX_RECORDS_PER_READ,
        idempotency_retention_seconds: float = DEFAULT_IDEMPOTENCY_RETENTION_SECONDS,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.clock_millis = clock_millis
        self.max_record_bytes = max_record_bytes
        self.max_records_per_read = max_records_per_read
        self.idempotency_retention_ms = int(idempotency_retention_seconds * 1_000)
        self.busy_timeout_seconds = busy_timeout_seconds
        if (
            not self.path.is_absolute()
            or type(max_record_bytes) is not int
            or max_record_bytes < 1
            or type(max_records_per_read) is not int
            or max_records_per_read < 1
            or not math.isfinite(idempotency_retention_seconds)
            or idempotency_retention_seconds <= 0
            or not math.isfinite(busy_timeout_seconds)
            or busy_timeout_seconds <= 0
        ):
            raise ActivityStoreError("invalid_store_path")
        created = self._prepare_path()
        self._initialize(created=created)

    @staticmethod
    def _private_directory(path: Path) -> bool:
        metadata = path.lstat()
        return (
            not path.is_symlink()
            and stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700
        )

    def _prepare_path(self) -> bool:
        try:
            current = Path(self.path.anchor)
            missing: list[Path] = []
            for part in self.path.parent.parts[1:]:
                current /= part
                try:
                    metadata = current.lstat()
                except FileNotFoundError:
                    missing.append(current)
                    continue
                if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                    raise ActivityStoreError("invalid_store_path")
            for directory in missing:
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                if not self._private_directory(directory):
                    raise ActivityStoreError("invalid_store_path")
            if not self._private_directory(self.path.parent):
                raise ActivityStoreError("invalid_store_path")

            created = False
            try:
                metadata = self.path.lstat()
            except FileNotFoundError:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(self.path, flags, 0o600)
                os.close(descriptor)
                created = True
                metadata = self.path.lstat()
            if (
                self.path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ActivityStoreError("invalid_store_path")
            return created
        except ActivityStoreError:
            raise
        except OSError as exc:
            raise ActivityStoreError("activity_store_unavailable") from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1_000)}")
        except sqlite3.Error as exc:
            raise ActivityStoreError("activity_store_unavailable") from exc
        try:
            with closing(connection):
                yield connection
        except sqlite3.Error as exc:
            raise ActivityStoreError("activity_store_unavailable") from exc

    def _initialize(self, *, created: bool) -> None:
        with self._connect() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current not in {0, STORE_SCHEMA_VERSION}:
                raise ActivityStoreError("activity_store_schema_unsupported")
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {STORE_SCHEMA_VERSION}")
        try:
            if created:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise ActivityStoreError("activity_store_unavailable") from exc

    @staticmethod
    def _run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT run_id, hive_id, bead_id, seat, provider, writer, source,
                      manifest_digest, revision
                 FROM runs WHERE run_id = ?""",
            (run_id,),
        ).fetchone()

    @staticmethod
    def _row_identity(row: sqlite3.Row) -> tuple[str | None, ...]:
        return tuple(
            row[name]
            for name in (
                "run_id",
                "hive_id",
                "bead_id",
                "seat",
                "provider",
                "writer",
                "source",
                "manifest_digest",
            )
        )

    def register_run(self, identity: ActivityRunIdentity) -> None:
        """Register one trusted exact run identity; replay is idempotent, drift is a conflict."""

        if not isinstance(identity, ActivityRunIdentity):
            raise ActivityStoreError("invalid_activity_schema")
        self._identity_bytes(identity)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._run_row(connection, identity.run_id)
                if row is None:
                    connection.execute(
                        """INSERT INTO runs
                           (run_id, hive_id, bead_id, seat, provider, writer, source,
                            manifest_digest, revision)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (*identity.values(), EMPTY_REVISION),
                    )
                elif self._row_identity(row) != identity.values():
                    raise ActivityStoreError("run_identity_conflict")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_payload_identity(
        identity: ActivityRunIdentity, payload: dict[str, object]
    ) -> None:
        expected = {
            "runId": identity.run_id,
            "hiveId": identity.hive_id,
            "beadId": identity.bead_id,
            "seat": identity.seat,
            "provider": identity.provider,
            "writer": identity.writer,
            "source": identity.source,
            "manifestDigest": identity.manifest_digest,
        }
        noncanonical = {"run_id", "hive_id", "bead_id", "manifest_digest"} & payload.keys()
        if noncanonical:
            raise ActivityStoreError("payload_identity_noncanonical")
        for name, value in expected.items():
            if name in payload and payload[name] != value:
                raise ActivityStoreError("payload_identity_mismatch")

    def _request_snapshot(
        self, identity: ActivityRunIdentity, request: ActivityAppendRequest
    ) -> tuple[bytes, str]:
        request_document = request.to_wire()
        payload = request_document.get("payload")
        if not isinstance(payload, dict):
            raise ActivityStoreError("invalid_activity_schema")
        self._validate_payload_identity(identity, payload)
        document = {
            "identity": self._identity_document(identity),
            "request": request_document,
        }
        encoded = self._bounded_bytes(document)
        payload_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        return encoded, payload_json

    @staticmethod
    def _identity_document(identity: ActivityRunIdentity) -> dict[str, object]:
        return {
            "runId": identity.run_id,
            "hiveId": identity.hive_id,
            "beadId": identity.bead_id,
            "seat": identity.seat,
            "provider": identity.provider,
            "writer": identity.writer,
            "source": identity.source,
            "manifestDigest": identity.manifest_digest,
        }

    def _identity_bytes(self, identity: ActivityRunIdentity) -> bytes:
        return self._bounded_bytes(
            {"schemaVersion": STORE_SCHEMA_VERSION, "identity": self._identity_document(identity)}
        )

    def _bounded_bytes(self, document: dict[str, object]) -> bytes:
        try:
            encoded = json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ActivityStoreError("invalid_activity_schema") from exc
        if len(encoded) > self.max_record_bytes:
            raise ActivityStoreError("record_too_large")
        return encoded

    @staticmethod
    def _append_response(row: sqlite3.Row, status: Literal["created", "duplicate"]):
        return ActivityAppendResponse(
            status=status,
            run_id=row["run_id"],
            idempotency_key=row["idempotency_key"],
            activity_id=row["activity_id"],
            revision=row["revision"],
        )

    def append(
        self,
        identity: ActivityRunIdentity,
        request: ActivityAppendRequest,
        *,
        register_unknown: bool = False,
    ) -> ActivityAppendResponse:
        """Validate, optionally register, and append in one durable transaction."""

        if not isinstance(request, ActivityAppendRequest):
            raise ActivityStoreError("invalid_activity_schema")
        if request.run_id != identity.run_id:
            raise ActivityStoreError("run_identity_mismatch")
        if request.source != identity.source:
            raise ActivityStoreError("source_identity_mismatch")
        if _KIND.fullmatch(request.kind) is None:
            raise ActivityStoreError("invalid_kind")
        encoded, payload_json = self._request_snapshot(identity, request)
        digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        now = int(self.clock_millis())
        if now >= request.expires_at:
            raise ActivityStoreError("activity_expired")
        if request.expires_at > now + self.idempotency_retention_ms:
            raise ActivityStoreError("expiry_window_exceeded")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._run_row(connection, identity.run_id)
                if run is None:
                    if not register_unknown:
                        raise ActivityStoreError("run_unknown")
                    connection.execute(
                        """INSERT INTO runs
                           (run_id, hive_id, bead_id, seat, provider, writer, source,
                            manifest_digest, revision)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (*identity.values(), EMPTY_REVISION),
                    )
                elif self._row_identity(run) != identity.values():
                    raise ActivityStoreError("run_identity_conflict")

                # A v1 accepted activity is itself permanent, so its run-scoped key ledger is
                # permanent too.  Expiry bounds admission/retry windows; wall-clock rollback can
                # never make a previously acknowledged key reusable.
                duplicate = connection.execute(
                    """SELECT a.run_id, a.idempotency_key, a.activity_id, a.revision,
                              i.request_digest
                         FROM idempotency i
                         JOIN activities a ON a.activity_id = i.activity_id
                        WHERE i.run_id = ? AND i.idempotency_key = ?""",
                    (identity.run_id, request.idempotency_key),
                ).fetchone()
                if duplicate is not None:
                    if duplicate["request_digest"] != digest:
                        raise ActivityStoreError("idempotency_conflict")
                    connection.execute("COMMIT")
                    return self._append_response(duplicate, "duplicate")

                activity_id = f"activity-{uuid.uuid4()}"
                revision = f"opaque:{uuid.uuid4()}"
                connection.execute(
                    """INSERT INTO activities
                       (activity_id, run_id, idempotency_key, source, kind, occurred_at,
                        expires_at, appended_at, revision, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        activity_id,
                        identity.run_id,
                        request.idempotency_key,
                        identity.source,
                        request.kind,
                        request.occurred_at,
                        request.expires_at,
                        now,
                        revision,
                        payload_json,
                    ),
                )
                connection.execute(
                    """INSERT INTO idempotency
                       (run_id, idempotency_key, request_digest, activity_id, retain_until)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        identity.run_id,
                        request.idempotency_key,
                        digest,
                        activity_id,
                        max(request.expires_at, now + self.idempotency_retention_ms),
                    ),
                )
                connection.execute(
                    "UPDATE runs SET revision = ? WHERE run_id = ?", (revision, identity.run_id)
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        return ActivityAppendResponse(
            status="created",
            run_id=identity.run_id,
            idempotency_key=request.idempotency_key,
            activity_id=activity_id,
            revision=revision,
        )

    def read_registered(self, run_id: str) -> tuple[ActivityRunIdentity, ActivityViewResponse]:
        """Read trusted registration and rows from one SQLite snapshot."""

        identity, view, complete, _total = self.read_registered_page(run_id)
        if not complete:
            raise ActivityStoreError("activity_read_limit_exceeded")
        return identity, view

    def read_registered_page(
        self, run_id: str, *, offset: int = 0
    ) -> tuple[ActivityRunIdentity, ActivityViewResponse, bool, int]:
        """Read one bounded durable page and its run revision from one SQLite snapshot."""

        try:
            encode_run_id(run_id)
        except (TypeError, ValueError) as exc:
            raise ActivityStoreError("invalid_run_id") from exc
        if type(offset) is not int or offset < 0:
            raise ActivityStoreError("invalid_activity_cursor")
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                run = self._run_row(connection, run_id)
                if run is None:
                    raise ActivityStoreError("run_unknown")
                rows = connection.execute(
                    """SELECT activity_id, run_id, idempotency_key, source, kind, occurred_at,
                              appended_at, revision, payload_json
                         FROM activities WHERE run_id = ? ORDER BY sequence LIMIT ? OFFSET ?""",
                    (run_id, self.max_records_per_read + 1, offset),
                ).fetchall()
                total = connection.execute(
                    "SELECT COUNT(*) AS value FROM activities WHERE run_id = ?", (run_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        complete = len(rows) <= self.max_records_per_read
        rows = rows[: self.max_records_per_read]
        activities = tuple(
            ActivityRecord(
                activity_id=row["activity_id"],
                run_id=row["run_id"],
                idempotency_key=row["idempotency_key"],
                source=row["source"],
                kind=row["kind"],
                occurred_at=row["occurred_at"],
                appended_at=row["appended_at"],
                revision=row["revision"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        )
        identity = ActivityRunIdentity(
            run_id=run["run_id"],
            hive_id=run["hive_id"],
            bead_id=run["bead_id"],
            seat=run["seat"],
            provider=run["provider"],
            writer=run["writer"],
            source=run["source"],
            manifest_digest=run["manifest_digest"],
        )
        return (
            identity,
            ActivityViewResponse(run_id=run_id, revision=run["revision"], activities=activities),
            complete,
            int(total["value"]),
        )

    def read(self, run_id: str) -> ActivityViewResponse:
        """Read one exact run in durable append order without manufacturing partial success."""

        return self.read_registered(run_id)[1]


__all__ = [
    "ActivityRunIdentity",
    "ActivitySourceCapability",
    "ActivityStoreError",
    "DurableActivityStore",
    "source_writers",
    "source_capabilities",
]
