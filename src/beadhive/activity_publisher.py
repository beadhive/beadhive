"""Bounded, durable, outcome-neutral publication of provider activity.

Provider owners write the local run journal first.  This adapter copies that already-bound
record into a small SQLite outbox and retries the authenticated daemon POST independently.
Every public method is finite; callers deliberately treat failures as observability loss, never
as a provider result.  The outbox keeps the byte-identical request across process restart so the
daemon's idempotency boundary remains authoritative.
"""

from __future__ import annotations

import atexit
import hashlib
import ipaddress
import json
import math
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from . import config, run_journal

_WRITER_SOURCES = {
    run_journal.WRITER_BAML: "baml",
    run_journal.WRITER_HITCH: "hitch",
    run_journal.WRITER_LOCAL_LOOP: "beadhive",
    run_journal.WRITER_ROLE: "beadhive",
}
_TOKEN_ENV = {
    "baml": "BH_ACTIVITY_PUBLISH_BAML_TOKEN",
    "hitch": "BH_ACTIVITY_PUBLISH_HITCH_TOKEN",
    "beadhive": "BH_ACTIVITY_PUBLISH_BEADHIVE_TOKEN",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    idempotency_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    run_id TEXT NOT NULL,
    body BLOB NOT NULL,
    body_bytes INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    deadline_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at INTEGER NOT NULL,
    last_reason TEXT,
    lease_owner TEXT,
    lease_until INTEGER
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS drop_reasons (
    reason TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

Transport = Callable[[str, str, bytes, str, float], int]


def source_for_writer(writer: str) -> str:
    try:
        return _WRITER_SOURCES[writer]
    except KeyError as exc:
        raise ValueError("unsupported_source") from exc


def scoped_child_env(env: Mapping[str, str], writer: str) -> dict[str, str]:
    """Expose at most the authenticated source credential owned by this journal writer."""

    source = source_for_writer(writer)
    allowed = _TOKEN_ENV[source]
    child = dict(env)
    for name in _TOKEN_ENV.values():
        if name != allowed:
            child.pop(name, None)
    return child


@dataclass(frozen=True)
class ActivityPublisherConfig:
    origin: str
    queue_path: Path
    tokens: Mapping[str, str] = field(repr=False)
    max_queue_records: int = 256
    max_queue_bytes: int = 4 * 1_048_576
    max_attempts: int = 5
    delivery_deadline_seconds: float = 30.0
    request_timeout_seconds: float = 2.0
    lease_seconds: float = 5.0
    initial_backoff_seconds: float = 0.1
    max_backoff_seconds: float = 2.0
    event_ttl_seconds: float = 60.0
    shutdown_drain_seconds: float = 2.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("activity publisher origin must be an exact HTTP origin")
        if parsed.scheme == "http":
            try:
                loopback = (
                    parsed.hostname == "localhost"
                    or ipaddress.ip_address(parsed.hostname or "").is_loopback
                )
            except ValueError:
                loopback = False
            if not loopback:
                raise ValueError("plaintext activity publisher origin must be loopback")
        if not self.queue_path.is_absolute():
            raise ValueError("activity publisher queue path must be absolute")
        if set(self.tokens) - frozenset(_TOKEN_ENV):
            raise ValueError("activity publisher token source is unsupported")
        if any(not isinstance(value, str) or not value for value in self.tokens.values()):
            raise ValueError("activity publisher tokens must be non-empty strings")
        integers = (self.max_queue_records, self.max_queue_bytes, self.max_attempts)
        if any(type(value) is not int or value < 1 for value in integers):
            raise ValueError("activity publisher integer budgets must be positive")
        durations = (
            self.delivery_deadline_seconds,
            self.request_timeout_seconds,
            self.lease_seconds,
            self.initial_backoff_seconds,
            self.max_backoff_seconds,
            self.event_ttl_seconds,
            self.shutdown_drain_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in durations):
            raise ValueError("activity publisher time budgets must be finite and positive")
        if self.initial_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("activity publisher initial backoff exceeds maximum")
        if self.lease_seconds <= self.request_timeout_seconds:
            raise ValueError("activity publisher lease must exceed request timeout")


@dataclass(frozen=True)
class ActivityPublisherStatus:
    retained: int
    retained_bytes: int
    published: int
    retried: int
    expired: int
    dropped: int
    retained_reasons: Mapping[str, int]
    drop_reasons: Mapping[str, int]


def _http_transport(origin: str, run_id: str, body: bytes, token: str, timeout: float) -> int:
    # The route requires the stricter unreserved-only spelling even though run ids themselves
    # also admit a colon.  Never place credentials or request bodies in a URL.
    encoded_run = quote(run_id, safe="-._~")
    response = httpx.post(
        f"{origin.rstrip('/')}/api/v1/runs/{encoded_run}/activity",
        content=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    return response.status_code


class ActivityPublisher:
    """A persistent finite outbox with one optional retry worker."""

    def __init__(
        self,
        settings: ActivityPublisherConfig,
        *,
        transport: Transport = _http_transport,
        clock_millis: Callable[[], int] | None = None,
        start_worker: bool = True,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._clock_millis = clock_millis or (lambda: time.time_ns() // 1_000_000)
        self._condition = threading.Condition()
        self._database_lock = threading.RLock()
        self._closing = False
        self._cancelled = False
        self._drain_until = 0.0
        self._lease_owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        self._thread: threading.Thread | None = None
        self._prepare_path()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(outbox)").fetchall()
                }
                if "lease_owner" not in columns:
                    connection.execute("ALTER TABLE outbox ADD COLUMN lease_owner TEXT")
                if "lease_until" not in columns:
                    connection.execute("ALTER TABLE outbox ADD COLUMN lease_until INTEGER")
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS outbox_delivery
                       ON outbox(source, next_attempt_at, lease_until)"""
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if start_worker:
            self._thread = threading.Thread(
                target=self._worker,
                name="beadhive-activity-publisher",
                daemon=True,
            )
            self._thread.start()

    def _prepare_path(self) -> None:
        path = self.settings.queue_path
        parent = path.parent
        current = Path(path.anchor)
        missing: list[Path] = []
        for part in parent.parts[1:]:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                missing.append(current)
                continue
            if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("activity publisher queue path is unsafe")
        for directory in missing:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = directory.lstat()
            if (
                directory.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise ValueError("activity publisher queue parent must be private and owned")
        metadata = parent.lstat()
        if (
            parent.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ValueError("activity publisher queue parent must be private and owned")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("activity publisher queue must be private and owned")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.settings.queue_path, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        return connection

    @staticmethod
    def _increment(connection: sqlite3.Connection, name: str) -> None:
        connection.execute(
            """INSERT INTO counters(name, value) VALUES (?, 1)
               ON CONFLICT(name) DO UPDATE SET value = value + 1""",
            (name,),
        )

    @classmethod
    def _drop(cls, connection: sqlite3.Connection, reason: str, *, expired: bool = False) -> None:
        cls._increment(connection, "dropped")
        if expired:
            cls._increment(connection, "expired")
        connection.execute(
            """INSERT INTO drop_reasons(reason, value) VALUES (?, 1)
               ON CONFLICT(reason) DO UPDATE SET value = value + 1""",
            (reason,),
        )

    @staticmethod
    def _request(record: Mapping[str, Any], source: str, ttl_ms: int) -> bytes:
        canonical = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        key = f"{source}:{hashlib.sha256(canonical).hexdigest()}"
        occurred_at = int(record["timestamp_ms"])
        payload = {
            "schemaVersion": 1,
            "runId": record["run_id"],
            "idempotencyKey": key,
            "source": source,
            "kind": record["activity"]["kind"],
            "occurredAt": occurred_at,
            "expiresAt": occurred_at + ttl_ms,
            "payload": {**record["activity"], "writer": record["writer"]},
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def enqueue_record(self, record: Mapping[str, Any]) -> bool:
        """Persist one exact request, returning false only for a visible bounded drop."""

        source = source_for_writer(str(record.get("writer", "")))
        token = self.settings.tokens.get(source)
        now = int(self._clock_millis())
        if token is None:
            with self._database_lock, self._connect() as connection:
                self._drop(connection, "credential_unavailable")
            return False
        body = self._request(record, source, int(self.settings.event_ttl_seconds * 1_000))
        request = json.loads(body)
        deadline = min(
            int(request["expiresAt"]),
            now + int(self.settings.delivery_deadline_seconds * 1_000),
        )
        with self._database_lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT body FROM outbox WHERE idempotency_key = ?",
                (request["idempotencyKey"],),
            ).fetchone()
            if existing is not None:
                return bytes(existing["body"]) == body
            totals = connection.execute(
                "SELECT COUNT(*) AS records, COALESCE(SUM(body_bytes), 0) AS bytes FROM outbox"
            ).fetchone()
            if int(totals["records"]) >= self.settings.max_queue_records:
                self._drop(connection, "queue_records_exceeded")
                return False
            if int(totals["bytes"]) + len(body) > self.settings.max_queue_bytes:
                self._drop(connection, "queue_bytes_exceeded")
                return False
            connection.execute(
                """INSERT INTO outbox
                   (idempotency_key, source, run_id, body, body_bytes, expires_at,
                    deadline_at, attempts, next_attempt_at, last_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)""",
                (
                    request["idempotencyKey"],
                    source,
                    record["run_id"],
                    body,
                    len(body),
                    request["expiresAt"],
                    deadline,
                    now,
                ),
            )
        with self._condition:
            self._condition.notify_all()
        return True

    def _claim(self, *, force: bool) -> sqlite3.Row | None:
        now = int(self._clock_millis())
        sources = tuple(sorted(self.settings.tokens))
        if not sources:
            return None
        placeholders = ",".join("?" for _ in sources)
        lease_until = now + max(1, int(self.settings.lease_seconds * 1_000))
        with self._database_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"""SELECT * FROM outbox
                        WHERE source IN ({placeholders})
                          AND (? OR next_attempt_at <= ?)
                          AND (lease_owner IS NULL OR lease_until <= ?)
                        ORDER BY next_attempt_at, idempotency_key LIMIT 1""",  # noqa: S608
                    (*sources, 1 if force else 0, now, now),
                ).fetchone()
                if row is not None:
                    claimed = connection.execute(
                        """UPDATE outbox SET lease_owner = ?, lease_until = ?
                           WHERE idempotency_key = ?
                             AND (lease_owner IS NULL OR lease_until <= ?)""",
                        (
                            self._lease_owner,
                            lease_until,
                            row["idempotency_key"],
                            now,
                        ),
                    ).rowcount
                    if claimed != 1:
                        row = None
                connection.execute("COMMIT")
                return row
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _delete_claimed(self, connection: sqlite3.Connection, key: str) -> bool:
        return (
            connection.execute(
                "DELETE FROM outbox WHERE idempotency_key = ? AND lease_owner = ?",
                (key, self._lease_owner),
            ).rowcount
            == 1
        )

    def flush_once(self, *, force: bool = False) -> bool:
        """Attempt or expire at most one row; useful to deterministic owners and tests."""

        row = self._claim(force=force)
        if row is None:
            return False
        now = int(self._clock_millis())
        key = str(row["idempotency_key"])
        if now >= int(row["expires_at"]):
            with self._database_lock, self._connect() as connection:
                if self._delete_claimed(connection, key):
                    self._drop(connection, "expired", expired=True)
            return True
        if now >= int(row["deadline_at"]):
            with self._database_lock, self._connect() as connection:
                if self._delete_claimed(connection, key):
                    self._drop(connection, "deadline_exceeded")
            return True

        attempts = int(row["attempts"]) + 1
        reason = "sink_unavailable"
        status = 0
        try:
            status = self.transport(
                self.settings.origin,
                str(row["run_id"]),
                bytes(row["body"]),
                self.settings.tokens[str(row["source"])],
                self.settings.request_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - sink failure is deliberately outcome-neutral
            pass
        if status in {200, 201}:
            with self._database_lock, self._connect() as connection:
                if self._delete_claimed(connection, key):
                    self._increment(connection, "published")
            return True
        retryable = status in {0, 408, 429} or 500 <= status <= 599
        if status:
            reason = f"http_status_{status}"
        with self._database_lock, self._connect() as connection:
            if not retryable:
                if self._delete_claimed(connection, key):
                    self._drop(connection, reason)
                return True
            if attempts >= self.settings.max_attempts:
                if self._delete_claimed(connection, key):
                    self._increment(connection, "retried")
                    self._drop(connection, "attempts_exhausted")
                return True
            delay = min(
                self.settings.initial_backoff_seconds * (2 ** (attempts - 1)),
                self.settings.max_backoff_seconds,
            )
            updated = connection.execute(
                """UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_reason = ?
                                      , lease_owner = NULL, lease_until = NULL
                   WHERE idempotency_key = ? AND lease_owner = ?""",
                (
                    attempts,
                    now + max(1, int(delay * 1_000)),
                    reason,
                    key,
                    self._lease_owner,
                ),
            ).rowcount
            if updated == 1:
                self._increment(connection, "retried")
        return True

    def _release_owned_leases(self) -> None:
        with self._database_lock, self._connect() as connection:
            connection.execute(
                """UPDATE outbox SET lease_owner = NULL, lease_until = NULL
                   WHERE lease_owner = ?""",
                (self._lease_owner,),
            )

    def status(self) -> ActivityPublisherStatus:
        with self._database_lock, self._connect() as connection:
            totals = connection.execute(
                "SELECT COUNT(*) AS records, COALESCE(SUM(body_bytes), 0) AS bytes FROM outbox"
            ).fetchone()
            counters = {
                str(row["name"]): int(row["value"])
                for row in connection.execute("SELECT name, value FROM counters")
            }
            reasons = {
                str(row["reason"]): int(row["value"])
                for row in connection.execute("SELECT reason, value FROM drop_reasons")
            }
            retained_reasons = {
                str(row["last_reason"]): int(row["value"])
                for row in connection.execute(
                    """SELECT last_reason, COUNT(*) AS value FROM outbox
                       WHERE last_reason IS NOT NULL GROUP BY last_reason"""
                )
            }
        return ActivityPublisherStatus(
            retained=int(totals["records"]),
            retained_bytes=int(totals["bytes"]),
            published=counters.get("published", 0),
            retried=counters.get("retried", 0),
            expired=counters.get("expired", 0),
            dropped=counters.get("dropped", 0),
            retained_reasons=retained_reasons,
            drop_reasons=reasons,
        )

    def _worker(self) -> None:
        while True:
            with self._condition:
                if self._cancelled:
                    return
                if self._closing and (
                    self.status().retained == 0 or time.monotonic() >= self._drain_until
                ):
                    return
            progressed = self.flush_once()
            with self._condition:
                if not progressed:
                    self._condition.wait(timeout=0.05)

    def cancel(self) -> None:
        """Stop retry work immediately, retaining every queued row for restart."""

        with self._condition:
            self._cancelled = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=self.settings.request_timeout_seconds + 0.1)
        if self._thread is None or not self._thread.is_alive():
            self._release_owned_leases()

    def close(self) -> None:
        """Drain only within the configured shutdown budget, then retain the remainder."""

        with self._condition:
            self._closing = True
            self._drain_until = time.monotonic() + self.settings.shutdown_drain_seconds
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(
                timeout=self.settings.shutdown_drain_seconds
                + self.settings.request_timeout_seconds
                + 0.1
            )
            if self._thread.is_alive():
                self.cancel()


_singleton_lock = threading.Lock()
_singleton: ActivityPublisher | None = None


def _positive_env(env: Mapping[str, str], name: str, default: float) -> float:
    value = float(env.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def config_from_env(env: Mapping[str, str] | None = None) -> ActivityPublisherConfig | None:
    source = os.environ if env is None else env
    origin = source.get("BH_ACTIVITY_PUBLISH_ORIGIN")
    if not origin:
        return None
    tokens = {provider: source[name] for provider, name in _TOKEN_ENV.items() if source.get(name)}
    queue_path = (
        Path(
            source.get(
                "BH_ACTIVITY_PUBLISH_QUEUE",
                str(config.home() / "activity-publisher" / "outbox.sqlite3"),
            )
        )
        .expanduser()
        .absolute()
    )
    request_timeout = _positive_env(source, "BH_ACTIVITY_PUBLISH_TIMEOUT_SECONDS", 2.0)
    return ActivityPublisherConfig(
        origin=origin,
        queue_path=queue_path,
        tokens=tokens,
        max_queue_records=int(source.get("BH_ACTIVITY_PUBLISH_MAX_RECORDS", "256")),
        max_queue_bytes=int(source.get("BH_ACTIVITY_PUBLISH_MAX_BYTES", str(4 * 1_048_576))),
        max_attempts=int(source.get("BH_ACTIVITY_PUBLISH_MAX_ATTEMPTS", "5")),
        delivery_deadline_seconds=_positive_env(
            source, "BH_ACTIVITY_PUBLISH_DEADLINE_SECONDS", 30.0
        ),
        request_timeout_seconds=request_timeout,
        lease_seconds=_positive_env(
            source,
            "BH_ACTIVITY_PUBLISH_LEASE_SECONDS",
            max(5.0, request_timeout * 2),
        ),
        initial_backoff_seconds=_positive_env(
            source, "BH_ACTIVITY_PUBLISH_INITIAL_BACKOFF_SECONDS", 0.1
        ),
        max_backoff_seconds=_positive_env(source, "BH_ACTIVITY_PUBLISH_MAX_BACKOFF_SECONDS", 2.0),
        event_ttl_seconds=_positive_env(source, "BH_ACTIVITY_PUBLISH_TTL_SECONDS", 60.0),
        shutdown_drain_seconds=_positive_env(source, "BH_ACTIVITY_PUBLISH_SHUTDOWN_SECONDS", 2.0),
    )


def publisher_for_env() -> ActivityPublisher | None:
    """Return the one process-owned outbox, or explicit disabled state when unconfigured."""

    global _singleton
    settings = config_from_env()
    if settings is None:
        return None
    with _singleton_lock:
        if _singleton is None:
            _singleton = ActivityPublisher(settings)
            atexit.register(_singleton.close)
        elif _singleton.settings != settings:
            raise RuntimeError("activity publisher configuration changed during the process")
        return _singleton


__all__ = [
    "ActivityPublisher",
    "ActivityPublisherConfig",
    "ActivityPublisherStatus",
    "config_from_env",
    "publisher_for_env",
    "scoped_child_env",
    "source_for_writer",
]
