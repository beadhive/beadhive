"""Source-side durable activity delivery from real provider ownership seams."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from beadhive import activity_publisher, run_journal

HIVE = "github/beadhive/beadhive"
DIGEST = "sha256:" + "a" * 64


def _record(source: str, writer: str, *, now: int = 1_000) -> dict[str, object]:
    return {
        "version": run_journal.VERSION,
        "source_revision": f"opaque:{source}-1",
        "timestamp_ms": now,
        "run_id": f"run-{source}",
        "hive": HIVE,
        "bead": "bh-q0lol.11",
        "driver": source,
        "provider": "codex",
        "manifest_digest": DIGEST,
        "provider_continuation": f"provider-{source}",
        "writer": writer,
        "activity": {"kind": "provider.completed", "phase": "finished"},
    }


def _config(tmp_path: Path, **overrides) -> activity_publisher.ActivityPublisherConfig:
    values = {
        "origin": "http://127.0.0.1:8737",
        "queue_path": tmp_path / "activity-outbox.sqlite3",
        "tokens": {"baml": "baml-token", "hitch": "hitch-token", "beadhive": "bh-token"},
        "max_queue_records": 2,
        "max_queue_bytes": 8_192,
        "max_attempts": 3,
        "delivery_deadline_seconds": 10,
        "request_timeout_seconds": 0.1,
        "initial_backoff_seconds": 0.001,
        "max_backoff_seconds": 0.01,
        "event_ttl_seconds": 30,
        "shutdown_drain_seconds": 0.1,
    }
    values.update(overrides)
    return activity_publisher.ActivityPublisherConfig(**values)


def test_outage_retry_restart_uses_byte_identical_request_and_one_queue_row(
    tmp_path: Path,
) -> None:
    now = [1_000]
    attempts: list[bytes] = []

    def outage(_origin, _run_id, body, _token, _timeout):
        attempts.append(body)
        raise OSError("sink down")

    first = activity_publisher.ActivityPublisher(
        _config(tmp_path), transport=outage, clock_millis=lambda: now[0], start_worker=False
    )
    record = _record("baml", run_journal.WRITER_BAML)
    assert first.enqueue_record(record) is True
    assert first.enqueue_record(record) is True
    assert first.status().retained == 1
    first.flush_once(force=True)
    status = first.status()
    assert (status.retained, status.retried, status.dropped) == (1, 1, 0)
    assert status.retained_reasons == {"sink_unavailable": 1}
    first.cancel()

    def recovered(_origin, _run_id, body, token, _timeout):
        attempts.append(body)
        assert token == "baml-token"
        return 200

    restarted = activity_publisher.ActivityPublisher(
        _config(tmp_path),
        transport=recovered,
        clock_millis=lambda: now[0],
        start_worker=False,
    )
    restarted.flush_once(force=True)
    status = restarted.status()
    assert (status.retained, status.published, status.retried) == (0, 1, 1)
    assert attempts[0] == attempts[1]
    assert json.loads(attempts[0])["idempotencyKey"].startswith("baml:")


def test_expiry_attempt_and_queue_budgets_drop_with_visible_reasons(tmp_path: Path) -> None:
    now = [1_000]
    publisher = activity_publisher.ActivityPublisher(
        _config(tmp_path, max_queue_records=1, max_attempts=1),
        transport=lambda *_args: 503,
        clock_millis=lambda: now[0],
        start_worker=False,
    )
    assert publisher.enqueue_record(_record("baml", run_journal.WRITER_BAML)) is True
    assert publisher.enqueue_record(_record("hitch", run_journal.WRITER_HITCH)) is False
    publisher.flush_once(force=True)
    status = publisher.status()
    assert status.retained == 0
    assert status.dropped == 2
    assert status.drop_reasons == {
        "attempts_exhausted": 1,
        "queue_records_exceeded": 1,
    }

    expiring = activity_publisher.ActivityPublisher(
        _config(tmp_path / "expiry"),
        transport=lambda *_args: 201,
        clock_millis=lambda: now[0],
        start_worker=False,
    )
    assert expiring.enqueue_record(_record("beadhive", run_journal.WRITER_ROLE)) is True
    now[0] = 31_001
    expiring.flush_once(force=True)
    assert expiring.status().drop_reasons == {"expired": 1}

    byte_bounded = activity_publisher.ActivityPublisher(
        _config(tmp_path / "bytes", max_queue_bytes=1),
        transport=lambda *_args: 201,
        clock_millis=lambda: 1_000,
        start_worker=False,
    )
    assert byte_bounded.enqueue_record(_record("baml", run_journal.WRITER_BAML)) is False
    assert byte_bounded.status().drop_reasons == {"queue_bytes_exceeded": 1}


def test_worker_retries_with_backoff_and_shutdown_drains_within_budget(tmp_path: Path) -> None:
    attempts = [503, 201]

    def recovering(*_args):
        return attempts.pop(0)

    publisher = activity_publisher.ActivityPublisher(
        _config(tmp_path), transport=recovering, start_worker=True
    )
    now = time.time_ns() // 1_000_000
    assert publisher.enqueue_record(_record("baml", run_journal.WRITER_BAML, now=now))
    for _ in range(100):
        if publisher.status().published == 1:
            break
        time.sleep(0.01)
    publisher.close()

    status = publisher.status()
    assert (status.retained, status.retried, status.published) == (0, 1, 1)


def test_cancellation_stops_attempts_and_retains_for_restart(tmp_path: Path) -> None:
    attempted = 0

    def outage(*_args):
        nonlocal attempted
        attempted += 1
        return 503

    publisher = activity_publisher.ActivityPublisher(
        _config(tmp_path, initial_backoff_seconds=1, max_backoff_seconds=1),
        transport=outage,
        start_worker=True,
    )
    now = time.time_ns() // 1_000_000
    assert publisher.enqueue_record(_record("hitch", run_journal.WRITER_HITCH, now=now))
    for _ in range(100):
        if attempted:
            break
        time.sleep(0.01)
    publisher.cancel()
    attempts_at_cancel = attempted
    time.sleep(0.03)

    assert attempted == attempts_at_cancel
    assert publisher.status().retained == 1


@pytest.mark.parametrize(
    ("source", "writer"),
    [
        ("baml", run_journal.WRITER_BAML),
        ("hitch", run_journal.WRITER_HITCH),
        ("beadhive", run_journal.WRITER_LOCAL_LOOP),
    ],
)
def test_journal_provider_seam_never_changes_result_when_publisher_fails(
    tmp_path: Path, source: str, writer: str
) -> None:
    class BrokenPublisher:
        def enqueue_record(self, _record):
            raise RuntimeError("publisher failed")

    journal = run_journal.RunJournal.create(
        run_journal.RunIdentity(HIVE, "bh-q0lol.11", source, "codex", DIGEST),
        base=tmp_path / source,
        writer=writer,
        activity_publisher=BrokenPublisher(),
    )
    assert journal.append({"kind": "provider.completed", "phase": "finished"}, operation="result")
    records = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert [record["activity"]["kind"] for record in records] == [
        "run.created",
        "provider.completed",
    ]


def test_agentguides_stays_explicitly_unsupported() -> None:
    with pytest.raises(ValueError, match="unsupported_source"):
        activity_publisher.source_for_writer("agentguides.direct")


@pytest.mark.parametrize(
    ("writer", "kept"),
    [
        (run_journal.WRITER_BAML, "BH_ACTIVITY_PUBLISH_BAML_TOKEN"),
        (run_journal.WRITER_HITCH, "BH_ACTIVITY_PUBLISH_HITCH_TOKEN"),
        (run_journal.WRITER_LOCAL_LOOP, "BH_ACTIVITY_PUBLISH_BEADHIVE_TOKEN"),
    ],
)
def test_provider_child_receives_only_its_authenticated_source_token(
    writer: str, kept: str
) -> None:
    tokens = {
        "BH_ACTIVITY_PUBLISH_BAML_TOKEN": "baml",
        "BH_ACTIVITY_PUBLISH_HITCH_TOKEN": "hitch",
        "BH_ACTIVITY_PUBLISH_BEADHIVE_TOKEN": "beadhive",
        "UNRELATED": "kept",
    }
    child = activity_publisher.scoped_child_env(tokens, writer)
    assert child[kept]
    assert child["UNRELATED"] == "kept"
    assert set(child) & (set(tokens) - {"UNRELATED"}) == {kept}


def test_environment_configuration_is_finite_and_never_repr_leaks_tokens(
    tmp_path: Path,
) -> None:
    settings = activity_publisher.config_from_env(
        {
            "BH_ACTIVITY_PUBLISH_ORIGIN": "http://127.0.0.1:8737",
            "BH_ACTIVITY_PUBLISH_QUEUE": str(tmp_path / "private" / "outbox.sqlite3"),
            "BH_ACTIVITY_PUBLISH_BAML_TOKEN": "do-not-render",
            "BH_ACTIVITY_PUBLISH_MAX_RECORDS": "3",
            "BH_ACTIVITY_PUBLISH_MAX_BYTES": "4096",
            "BH_ACTIVITY_PUBLISH_MAX_ATTEMPTS": "2",
            "BH_ACTIVITY_PUBLISH_DEADLINE_SECONDS": "4",
            "BH_ACTIVITY_PUBLISH_TIMEOUT_SECONDS": "1",
            "BH_ACTIVITY_PUBLISH_LEASE_SECONDS": "3",
            "BH_ACTIVITY_PUBLISH_INITIAL_BACKOFF_SECONDS": "0.01",
            "BH_ACTIVITY_PUBLISH_MAX_BACKOFF_SECONDS": "0.1",
            "BH_ACTIVITY_PUBLISH_TTL_SECONDS": "5",
            "BH_ACTIVITY_PUBLISH_SHUTDOWN_SECONDS": "0.2",
        }
    )
    assert settings is not None
    assert (settings.max_queue_records, settings.max_queue_bytes, settings.max_attempts) == (
        3,
        4096,
        2,
    )
    assert settings.lease_seconds == 3
    assert "do-not-render" not in repr(settings)

    publisher = activity_publisher.ActivityPublisher(settings, start_worker=False)
    assert publisher.settings.queue_path.stat().st_mode & 0o777 == 0o600
    assert publisher.settings.queue_path.parent.stat().st_mode & 0o777 == 0o700

    with pytest.raises(ValueError, match="finite and positive"):
        activity_publisher.config_from_env(
            {
                "BH_ACTIVITY_PUBLISH_ORIGIN": "http://127.0.0.1:8737",
                "BH_ACTIVITY_PUBLISH_QUEUE": str(tmp_path / "bad" / "outbox.sqlite3"),
                "BH_ACTIVITY_PUBLISH_BAML_TOKEN": "token",
                "BH_ACTIVITY_PUBLISH_TIMEOUT_SECONDS": "inf",
            }
        )


def test_existing_unsafe_queue_permissions_are_refused_without_mutation(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o755)
    queue = unsafe_parent / "outbox.sqlite3"
    settings = _config(tmp_path, queue_path=queue)

    with pytest.raises(ValueError, match="parent must be private and owned"):
        activity_publisher.ActivityPublisher(settings, start_worker=False)
    assert unsafe_parent.stat().st_mode & 0o777 == 0o755

    unsafe_parent.chmod(0o700)
    queue.write_bytes(b"")
    queue.chmod(0o644)
    with pytest.raises(ValueError, match="queue must be private and owned"):
        activity_publisher.ActivityPublisher(settings, start_worker=False)
    assert queue.stat().st_mode & 0o777 == 0o644


def test_source_scoped_workers_never_touch_other_source_rows_even_when_expired(
    tmp_path: Path,
) -> None:
    now = [1_000]
    queue = tmp_path / "shared" / "outbox.sqlite3"
    hitch = activity_publisher.ActivityPublisher(
        _config(tmp_path, queue_path=queue, tokens={"hitch": "hitch-token"}),
        transport=lambda *_args: 201,
        clock_millis=lambda: now[0],
        start_worker=False,
    )
    assert hitch.enqueue_record(_record("hitch", run_journal.WRITER_HITCH))
    hitch.cancel()

    now[0] = 31_001
    delivered: list[str] = []
    baml = activity_publisher.ActivityPublisher(
        _config(tmp_path, queue_path=queue, tokens={"baml": "baml-token"}),
        transport=lambda _origin, _run, body, _token, _timeout: (
            delivered.append(json.loads(body)["source"]) or 201
        ),
        clock_millis=lambda: now[0],
        start_worker=False,
    )
    assert baml.flush_once(force=True) is False
    with sqlite3.connect(queue) as connection:
        assert connection.execute(
            "SELECT source, attempts, last_reason FROM outbox"
        ).fetchall() == [("hitch", 0, None)]
    assert (baml.status().retained, baml.status().dropped) == (1, 0)

    assert baml.enqueue_record(_record("baml", run_journal.WRITER_BAML, now=now[0]))
    assert baml.flush_once(force=True) is True
    assert delivered == ["baml"]
    assert baml.status().retained == 1

    restarted_hitch = activity_publisher.ActivityPublisher(
        _config(tmp_path, queue_path=queue, tokens={"hitch": "hitch-token"}),
        transport=lambda *_args: 201,
        clock_millis=lambda: now[0],
        start_worker=False,
    )
    assert restarted_hitch.flush_once(force=True) is True
    assert restarted_hitch.status().drop_reasons == {"expired": 1}


def test_source_scoping_preserves_one_global_queue_capacity(tmp_path: Path) -> None:
    queue = tmp_path / "global-bound" / "outbox.sqlite3"
    hitch = activity_publisher.ActivityPublisher(
        _config(
            tmp_path,
            queue_path=queue,
            tokens={"hitch": "hitch-token"},
            max_queue_records=1,
        ),
        clock_millis=lambda: 1_000,
        start_worker=False,
    )
    assert hitch.enqueue_record(_record("hitch", run_journal.WRITER_HITCH))

    baml = activity_publisher.ActivityPublisher(
        _config(
            tmp_path,
            queue_path=queue,
            tokens={"baml": "baml-token"},
            max_queue_records=1,
        ),
        clock_millis=lambda: 1_000,
        start_worker=False,
    )
    assert baml.enqueue_record(_record("baml", run_journal.WRITER_BAML)) is False
    assert baml.status().retained == 1
    assert baml.status().drop_reasons == {"queue_records_exceeded": 1}
    with sqlite3.connect(queue) as connection:
        assert connection.execute("SELECT source FROM outbox").fetchall() == [("hitch",)]


def test_concurrent_same_source_workers_lease_one_row_exactly_once(tmp_path: Path) -> None:
    now = time.time_ns() // 1_000_000
    queue = tmp_path / "shared" / "outbox.sqlite3"
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_success(*_args):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        release.wait(timeout=2)
        return 201

    settings = _config(tmp_path, queue_path=queue, tokens={"baml": "baml-token"})
    first = activity_publisher.ActivityPublisher(
        settings, transport=blocked_success, clock_millis=lambda: now, start_worker=False
    )
    second = activity_publisher.ActivityPublisher(
        settings, transport=blocked_success, clock_millis=lambda: now, start_worker=False
    )
    assert first.enqueue_record(_record("baml", run_journal.WRITER_BAML, now=now))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_flush = pool.submit(first.flush_once, force=True)
        assert entered.wait(timeout=2)
        second_flush = pool.submit(second.flush_once, force=True)
        assert second_flush.result(timeout=1) is False
        release.set()
        assert first_flush.result(timeout=2) is True

    assert calls == 1
    assert first.status().retained == 0


def test_existing_outbox_schema_gains_persistent_lease_columns_on_restart(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "legacy"
    parent.mkdir(mode=0o700)
    queue = parent / "outbox.sqlite3"
    descriptor = queue.open("xb")
    descriptor.close()
    queue.chmod(0o600)
    with sqlite3.connect(queue) as connection:
        connection.execute(
            """CREATE TABLE outbox (
                idempotency_key TEXT PRIMARY KEY, source TEXT NOT NULL, run_id TEXT NOT NULL,
                body BLOB NOT NULL, body_bytes INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                deadline_at INTEGER NOT NULL, attempts INTEGER NOT NULL,
                next_attempt_at INTEGER NOT NULL, last_reason TEXT
            )"""
        )

    activity_publisher.ActivityPublisher(
        _config(tmp_path, queue_path=queue, tokens={"baml": "token"}),
        start_worker=False,
    )
    with sqlite3.connect(queue) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outbox)")}
    assert {"lease_owner", "lease_until"} <= columns
