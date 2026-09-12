"""Durable activity-store acceptance for the unified host daemon."""

from __future__ import annotations

import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from beadhive import daemon_activity, daemon_contract, run_journal

HIVE = "github/beadhive/beadhive"
RUN = "run-store-1"
DIGEST = "sha256:" + "a" * 64


def _identity(
    *,
    run_id: str = RUN,
    source: str = "baml",
    writer: str = run_journal.WRITER_BAML,
) -> daemon_activity.ActivityRunIdentity:
    return daemon_activity.ActivityRunIdentity(
        run_id=run_id,
        hive_id=HIVE,
        bead_id="bh-q0lol.9",
        seat="developer",
        provider="codex",
        writer=writer,
        source=source,
        manifest_digest=DIGEST,
    )


def _request(
    *,
    run_id: str = RUN,
    key: str = "publisher-1:event-1",
    source: str = "baml",
    kind: str = "provider.progress",
    payload: dict[str, object] | None = None,
    occurred_at: int = 1_000,
    expires_at: int = 61_000,
) -> daemon_contract.ActivityAppendRequest:
    return daemon_contract.ActivityAppendRequest(
        run_id=run_id,
        idempotency_key=key,
        source=source,
        kind=kind,
        occurred_at=occurred_at,
        expires_at=expires_at,
        payload=payload or {"phase": "running"},
    )


def _store(path: Path, *, now: int = 2_000, max_record_bytes: int = 8_192):
    return daemon_activity.DurableActivityStore(
        path,
        clock_millis=lambda: now,
        max_record_bytes=max_record_bytes,
        max_records_per_read=100,
        idempotency_retention_seconds=120,
        busy_timeout_seconds=5,
    )


def test_concurrent_and_post_restart_duplicates_are_one_durable_record(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "activity.sqlite3").absolute()
    store = _store(path)
    store.register_run(_identity())
    request = _request()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = tuple(pool.map(lambda _index: store.append(_identity(), request), range(24)))

    assert [result.status for result in results].count("created") == 1
    assert [result.status for result in results].count("duplicate") == 23
    assert len({result.activity_id for result in results}) == 1
    assert len({result.revision for result in results}) == 1

    restarted = _store(path, now=3_000)
    duplicate = restarted.append(_identity(), request)
    view = restarted.read(RUN)

    assert duplicate.status == "duplicate"
    assert len(view.activities) == 1
    assert view.activities[0].activity_id == duplicate.activity_id
    assert view.activities[0].payload == {"phase": "running"}
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_acknowledged_records_survive_restart_in_append_order(tmp_path: Path) -> None:
    path = (tmp_path / "activity.sqlite3").absolute()
    store = _store(path, now=2_000)
    store.register_run(_identity())
    first = store.append(_identity(), _request(key="baml:first"))

    restarted = _store(path, now=2_001)
    second = restarted.append(
        _identity(),
        _request(key="baml:second", kind="provider.finished", payload={"outcome": "done"}),
    )
    view = _store(path, now=2_002).read(RUN)

    assert (first.status, second.status) == ("created", "created")
    assert [item.idempotency_key for item in view.activities] == ["baml:first", "baml:second"]
    assert view.revision == second.revision


def test_read_revision_and_rows_share_one_snapshot_during_concurrent_append(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "activity.sqlite3").absolute()
    writer = _store(path)
    writer.register_run(_identity())
    first = writer.append(_identity(), _request(key="baml:first"))
    reader = _store(path)
    run_selected = threading.Event()
    append_done = threading.Event()
    original_run_row = reader._run_row

    def pause_after_run_select(connection, run_id):
        row = original_run_row(connection, run_id)
        run_selected.set()
        append_done.wait(timeout=0.5)
        return row

    reader._run_row = pause_after_run_select
    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(reader.read, RUN)
        assert run_selected.wait(timeout=2)

        def append_second():
            try:
                return writer.append(
                    _identity(),
                    _request(key="baml:second", kind="provider.finished"),
                )
            finally:
                append_done.set()

        append_future = pool.submit(append_second)
        view = read_future.result(timeout=5)
        second = append_future.result(timeout=5)

    assert view.revision == first.revision
    assert [item.idempotency_key for item in view.activities] == ["baml:first"]
    assert second.status == "created"


def test_unknown_ambiguous_and_path_payload_identity_are_refused(tmp_path: Path) -> None:
    store = _store((tmp_path / "activity.sqlite3").absolute())
    with pytest.raises(daemon_activity.ActivityStoreError, match="run_unknown") as unknown:
        store.append(_identity(), _request())
    assert unknown.value.code == "run_unknown"

    store.register_run(_identity())
    with pytest.raises(daemon_activity.ActivityStoreError, match="run_identity_conflict"):
        store.register_run(replace(_identity(), provider="claude-code"))
    with pytest.raises(daemon_activity.ActivityStoreError, match="run_identity_mismatch"):
        store.append(_identity(run_id="run-other"), _request())
    with pytest.raises(daemon_activity.ActivityStoreError, match="payload_identity_mismatch"):
        store.append(_identity(), _request(payload={"runId": "run-other"}))
    with pytest.raises(daemon_activity.ActivityStoreError, match="payload_identity_mismatch"):
        store.append(_identity(), _request(payload={"source": "hitch"}))
    with pytest.raises(daemon_activity.ActivityStoreError, match="payload_identity_mismatch"):
        store.append(
            _identity(),
            _request(payload={"manifestDigest": "sha256:" + "b" * 64}),
        )

    exact = {
        "runId": RUN,
        "hiveId": HIVE,
        "beadId": "bh-q0lol.9",
        "seat": "developer",
        "provider": "codex",
        "writer": run_journal.WRITER_BAML,
        "source": "baml",
        "manifestDigest": DIGEST,
    }
    assert store.append(_identity(), _request(key="baml:exact", payload=exact)).status == "created"
    assert store.read(RUN).activities[0].payload == exact


@pytest.mark.parametrize(
    "hive_id",
    ["github/acme corp/activity store", "github/研究チーム/蜂の巣"],
)
def test_shared_canonical_hive_identities_with_spaces_and_unicode_are_accepted(
    tmp_path: Path, hive_id: str
) -> None:
    store = _store((tmp_path / "activity.sqlite3").absolute())
    identity = replace(_identity(), hive_id=hive_id)

    encoded = daemon_contract.encode_hive_id(hive_id)
    assert daemon_contract.decode_hive_id(encoded) == hive_id
    store.register_run(identity)
    result = store.append(
        identity,
        _request(payload={"hiveId": hive_id}),
    )

    assert result.status == "created"
    assert store.read(RUN).activities[0].payload == {"hiveId": hive_id}


@pytest.mark.parametrize(
    "hive_id",
    [
        "github/acme\ncorp/activity",
        "github/acme\tcorp/activity",
        "github/acme\x00corp/activity",
        "github/acme\x7fcorp/activity",
        "github/acme%20corp/activity",
        "github/acme\\corp/activity",
        "github/./activity",
        "github/acme/activity/extra",
    ],
)
def test_non_routeable_hive_identity_is_refused_without_registration(
    tmp_path: Path, hive_id: str
) -> None:
    store = _store((tmp_path / "activity.sqlite3").absolute())

    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_hive_id"):
        store.register_run(replace(_identity(), hive_id=hive_id))
    with pytest.raises(daemon_activity.ActivityStoreError, match="run_unknown"):
        store.read(RUN)


@pytest.mark.parametrize(
    ("source", "writer"),
    [
        ("baml", run_journal.WRITER_BAML),
        ("hitch", run_journal.WRITER_HITCH),
        ("beadhive", run_journal.WRITER_ROLE),
        ("beadhive", run_journal.WRITER_LOCAL_LOOP),
    ],
)
def test_baml_hitch_and_beadhive_provenance_fixtures_pass(
    tmp_path: Path, source: str, writer: str
) -> None:
    store = _store((tmp_path / f"{source}-{writer}.sqlite3").absolute())
    identity = _identity(source=source, writer=writer)
    store.register_run(identity)

    result = store.append(identity, _request(source=source, key=f"{source}:event-1"))

    assert result.status == "created"
    assert store.read(RUN).activities[0].source == source


def test_invalid_provenance_and_agentguides_are_explicitly_unsupported(tmp_path: Path) -> None:
    path = (tmp_path / "activity.sqlite3").absolute()
    store = _store(path)
    with pytest.raises(daemon_activity.ActivityStoreError, match="source_writer_mismatch"):
        store.register_run(_identity(source="baml", writer=run_journal.WRITER_HITCH))
    with pytest.raises(daemon_activity.ActivityStoreError, match="unsupported_source"):
        store.register_run(_identity(source="agentguides", writer=run_journal.WRITER_ROLE))
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_seat"):
        store.register_run(replace(_identity(), seat="developer/../../root"))
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_bead"):
        store.register_run(replace(_identity(), bead_id="q0lol"))
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_provider"):
        store.register_run(replace(_identity(), provider="Codex Latest"))

    capabilities = {item.source: item for item in daemon_activity.source_capabilities()}
    assert capabilities["baml"].available is True
    assert capabilities["hitch"].available is True
    assert capabilities["beadhive"].available is True
    assert capabilities["agentguides"].available is False
    assert capabilities["agentguides"].reason_code == "unsupported_v1"


def test_expiry_size_kind_and_conflicting_key_are_refused_without_partial_write(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "activity.sqlite3").absolute()
    store = _store(path, now=2_000, max_record_bytes=1_024)
    store.register_run(_identity())

    with pytest.raises(daemon_activity.ActivityStoreError, match="activity_expired"):
        store.append(_identity(), _request(key="baml:expired", expires_at=1_999))
    with pytest.raises(daemon_activity.ActivityStoreError, match="expiry_window_exceeded"):
        store.append(
            _identity(),
            _request(key="baml:far", occurred_at=2_001, expires_at=122_001),
        )
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_kind"):
        store.append(_identity(), _request(key="baml:kind", kind="progress"))
    with pytest.raises(daemon_activity.ActivityStoreError, match="record_too_large"):
        store.append(_identity(), _request(key="baml:large", payload={"value": "x" * 2_000}))

    original = _request(key="baml:conflict")
    assert store.append(_identity(), original).status == "created"
    with pytest.raises(daemon_activity.ActivityStoreError, match="idempotency_conflict"):
        store.append(
            _identity(),
            _request(key="baml:conflict", payload={"phase": "different"}),
        )
    assert [item.idempotency_key for item in store.read(RUN).activities] == ["baml:conflict"]


def test_expiry_equal_to_ack_clock_is_refused_without_persistence(tmp_path: Path) -> None:
    now = [2_000]
    path = (tmp_path / "activity.sqlite3").absolute()
    store = daemon_activity.DurableActivityStore(
        path,
        clock_millis=lambda: now[0],
        max_record_bytes=8_192,
        max_records_per_read=100,
        idempotency_retention_seconds=120,
        busy_timeout_seconds=5,
    )
    store.register_run(_identity())
    boundary = _request(occurred_at=1_000, expires_at=2_000)

    with pytest.raises(daemon_activity.ActivityStoreError, match="activity_expired"):
        store.append(_identity(), boundary)
    assert store.read(RUN).activities == ()

    now[0] = 1_999
    created = store.append(_identity(), boundary)
    assert created.status == "created"

    now[0] = 2_000
    with pytest.raises(daemon_activity.ActivityStoreError, match="activity_expired"):
        store.append(_identity(), boundary)
    assert [item.activity_id for item in store.read(RUN).activities] == [created.activity_id]


def test_store_refuses_relative_or_symlink_database_paths(tmp_path: Path) -> None:
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_store_path"):
        _store(Path("relative.sqlite3"))

    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_store_path"):
        _store(link.absolute())


def test_store_refuses_unsafe_existing_parent_without_mutating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broad = tmp_path / "broad"
    broad.mkdir(mode=0o750)
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_store_path"):
        _store((broad / "activity.sqlite3").absolute())
    assert stat.S_IMODE(broad.stat().st_mode) == 0o750

    real = tmp_path / "real"
    real.mkdir(mode=0o750)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_store_path"):
        _store((linked / "activity.sqlite3").absolute())
    assert stat.S_IMODE(real.stat().st_mode) == 0o750

    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    monkeypatch.setattr(daemon_activity.os, "geteuid", lambda: owned.stat().st_uid + 1)
    with pytest.raises(daemon_activity.ActivityStoreError, match="invalid_store_path"):
        _store((owned / "activity.sqlite3").absolute())


def test_store_creates_every_missing_private_directory_with_private_mode(tmp_path: Path) -> None:
    path = (tmp_path / "one" / "two" / "activity.sqlite3").absolute()
    _store(path)

    assert stat.S_IMODE((tmp_path / "one").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_large_registered_identity_is_refused_before_persistence(tmp_path: Path) -> None:
    store = _store((tmp_path / "activity.sqlite3").absolute(), max_record_bytes=512)
    large = replace(_identity(), hive_id=f"github/{'x' * 1_000}/beadhive")

    with pytest.raises(daemon_activity.ActivityStoreError, match="record_too_large"):
        store.register_run(large)
    with pytest.raises(daemon_activity.ActivityStoreError, match="run_unknown"):
        store.read(RUN)


def test_stable_idempotency_survives_expiry_restart_and_clock_rollback(tmp_path: Path) -> None:
    now = [1_000]
    path = (tmp_path / "activity.sqlite3").absolute()
    store = daemon_activity.DurableActivityStore(
        path,
        clock_millis=lambda: now[0],
        max_record_bytes=8_192,
        max_records_per_read=100,
        idempotency_retention_seconds=1,
        busy_timeout_seconds=5,
    )
    store.register_run(_identity())
    request = _request(occurred_at=1_000, expires_at=2_000)
    first = store.append(_identity(), request)
    assert first.status == "created"

    now[0] = 3_001
    assert (
        store.append(
            _identity(),
            _request(key="baml:later", occurred_at=3_001, expires_at=4_001),
        ).status
        == "created"
    )

    now[0] = 1_500
    restarted = daemon_activity.DurableActivityStore(
        path,
        clock_millis=lambda: now[0],
        max_record_bytes=8_192,
        max_records_per_read=100,
        idempotency_retention_seconds=1,
        busy_timeout_seconds=5,
    )
    duplicate = restarted.append(_identity(), request)

    assert duplicate.status == "duplicate"
    assert duplicate.activity_id == first.activity_id
    assert [item.idempotency_key for item in restarted.read(RUN).activities] == [
        "publisher-1:event-1",
        "baml:later",
    ]


def test_read_limit_refuses_instead_of_returning_a_plausible_partial_view(tmp_path: Path) -> None:
    path = (tmp_path / "activity.sqlite3").absolute()
    writer = _store(path)
    writer.register_run(_identity())
    writer.append(_identity(), _request(key="baml:first"))
    writer.append(_identity(), _request(key="baml:second"))

    bounded = daemon_activity.DurableActivityStore(
        path,
        clock_millis=lambda: 2_000,
        max_record_bytes=8_192,
        max_records_per_read=1,
        idempotency_retention_seconds=120,
        busy_timeout_seconds=5,
    )
    with pytest.raises(daemon_activity.ActivityStoreError, match="activity_read_limit_exceeded"):
        bounded.read(RUN)
