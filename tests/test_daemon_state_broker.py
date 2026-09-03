"""Daemon-owned composition and lifecycle tests for the authoritative state broker."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.requests import Request

from beadhive import (
    daemon_state_broker,
    host_daemon,
    operator_api,
    operator_sources,
    operator_sse,
    run_journal,
    state_stream,
)
from beadhive.agent_run_summary import Freshness
from beadhive.daemon_config import HostDaemonConfig
from beadhive.public_readers import AgentRunSnapshot, Coverage
from beadhive.state_stream_polling import PollingStateStreamProvider

HIVE = "github/beadhive/beadhive"
OTHER_HIVE = "github/beadhive/other"
NOW = datetime(2026, 9, 3, tzinfo=UTC).isoformat().replace("+00:00", "Z")
DIGEST = "sha256:" + "a" * 64


def _snapshot(revision: str, status: str, *, hive: str = HIVE) -> state_stream.ProviderSnapshot:
    return state_stream.ProviderSnapshot(
        scope="hive",
        revision=revision,
        as_of=NOW,
        issues=(
            state_stream.StreamIssue(
                id="bh-1",
                hive=hive,
                issue_type="task",
                status=status,
                priority="P0",
                title="Authoritative state",
                updated_at=NOW,
            ),
        ),
    )


class MutableProvider:
    def __init__(self) -> None:
        self.current = _snapshot("source-revision-9000", "open")

    def refresh(self, _request):
        return self.current


def _sources(tmp_path: Path, provider: MutableProvider) -> operator_sources.OperatorSources:
    cfg = {
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": "beadhive",
                "prefix": "bh",
                "kind": "org-native",
            }
        ]
    }
    return operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-1",
        provider=provider,
        summary_reader=lambda _path, host, source: AgentRunSnapshot(
            host_id=host,
            source_id=source,
            revision="runtime-source-revision-7000",
            summaries=(),
            coverage=Coverage.UNKNOWN,
            coverage_reason="source_missing",
            freshness=Freshness(),
        ),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )


def _settings(tmp_path: Path | None = None, *, max_records_per_read: int = 5) -> HostDaemonConfig:
    raw = {
        "sse": {
            "replay_events_per_hive": 3,
            "replay_total_bytes": 65_536,
            "client_queue_events": 2,
            "max_clients": 4,
            "heartbeat_seconds": 7,
        },
        "activity": {
            "max_records_per_read": max_records_per_read,
            "max_body_bytes": 1_024,
        },
    }
    if tmp_path is not None:
        credential_file = tmp_path / "credentials.json"
        credential_file.write_text("[]")
        raw["auth"] = {"credential_file": credential_file}
    return HostDaemonConfig.model_validate(raw)


def _write_journal(tmp_path: Path, run_id: str = "run-1", *, hive: str = HIVE) -> Path:
    path = run_journal.journal_path_for_hive(hive, run_id, base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "version": run_journal.VERSION,
        "source_revision": "journal-source-revision-3000",
        "timestamp_ms": 1,
        "run_id": run_id,
        "hive": hive,
        "bead": "bh-1",
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": DIGEST,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created"},
    }
    path.write_text(json.dumps(record) + "\n")
    return path


def test_broker_applies_typed_daemon_bounds_to_every_owned_buffer(tmp_path: Path) -> None:
    broker = daemon_state_broker.DaemonStateBroker(
        sources=_sources(tmp_path, MutableProvider()),
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )

    assert broker.relay.replay_event_limit == 3
    assert broker.relay.replay_byte_limit == 65_536
    assert broker.relay.global_byte_limit == 65_536
    assert broker.relay.client_event_limit == 2
    assert broker.relay.client_byte_limit <= 65_536
    assert broker.relay.heartbeat_interval == 7
    assert broker.feed.max_cached_activity_runs == 5
    assert broker.feed.max_cached_activity_bytes == 5 * 1_024
    assert broker.feed.max_activity_discoveries == 5
    assert broker.feed.max_hive_admissions == 5
    assert broker.feed.max_run_ownerships == 5

    asyncio.run(broker.close())


def test_unknown_snapshot_and_event_reads_retain_no_hive_admissions(tmp_path: Path) -> None:
    broker = daemon_state_broker.DaemonStateBroker(
        sources=_sources(tmp_path, MutableProvider()),
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )

    for index in range(1_000):
        identity = f"github/unknown/snapshot-{index}"
        with pytest.raises(operator_sources.OperatorSourceError) as unknown:
            broker.snapshot_with_cursor(identity)
        assert unknown.value.code == "hive_not_found"

    async def unknown_events() -> None:
        for index in range(1_000):
            identity = f"github/unknown/events-{index}"
            encoded = identity.replace("/", "%2F")
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/api/v1/hives/{identity}/events",
                    "raw_path": f"/api/v1/hives/{encoded}/events".encode(),
                    "query_string": (f"subscription=hive%3A{encoded}&cursor=unknown%3A0").encode(),
                    "headers": [],
                    "path_params": {"hive_id": identity},
                }
            )
            response = await broker.events(request)
            assert response.status_code == 404
            assert json.loads(response.body)["error"]["code"] == "hive_not_found"

    asyncio.run(unknown_events())
    retained = broker.retained_state()
    assert not broker.feed._admissions
    assert not broker.feed.tracked_hive_ids()
    assert retained["hiveAdmissions"] == 0
    assert retained["hiveAdmissionCapacity"] == 5
    assert retained["hiveAdmissionRejections"] == 0
    asyncio.run(broker.close())


def test_broker_snapshot_delta_identity_and_transport_sequence_are_independent(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    broker = daemon_state_broker.DaemonStateBroker(
        sources=_sources(tmp_path, provider),
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
        now_millis=lambda: 1_000,
    )
    first = broker.snapshot_with_cursor(HIVE)
    epoch = str(first["cursor"]["producerEpoch"])
    loop = asyncio.new_event_loop()
    client = broker.relay.subscribe(
        HIVE,
        subscription_id=f"hive:{HIVE}",
        cursor=operator_sse.EventCursor(epoch, 0),
        loop=loop,
    )

    provider.current = _snapshot("source-revision-1000000", "closed")
    second = broker.snapshot_with_cursor(HIVE)
    frame, closed = broker.relay._take(client)

    assert first["hive"]["prefix"] == HIVE
    assert second["workItems"][0]["ref"]["hiveId"] == HIVE
    assert second["cursor"] == {
        "subscriptionId": f"hive:{HIVE}",
        "producerEpoch": epoch,
        "sequence": 1,
        "observedAt": 1_000,
    }
    assert frame is not None and closed is False
    assert b'"revision":"source-revision-1000000"' in frame
    assert b'"sequence":1' in frame
    assert b'"sequence":1000000' not in frame

    client.close()
    loop.close()
    asyncio.run(broker.close())


def test_hive_removal_closes_clients_drops_all_state_and_restarts_epoch(tmp_path: Path) -> None:
    provider = MutableProvider()
    broker = daemon_state_broker.DaemonStateBroker(
        sources=_sources(tmp_path, provider),
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    first = broker.snapshot_with_cursor(HIVE)
    old_epoch = str(first["cursor"]["producerEpoch"])
    _write_journal(tmp_path)
    activity = broker.activity_with_cursor("run-1")
    assert activity["hiveId"] == HIVE
    assert broker.feed.cached_activity_runs == 1

    asyncio.run(broker.remove_hive("github/other/repository"))
    untouched_snapshot = broker.snapshot_with_cursor(HIVE)
    untouched_activity = broker.activity_with_cursor(
        "run-1",
        after=(str(activity["producerEpoch"]), int(activity["sequence"])),
    )
    assert untouched_snapshot["cursor"]["producerEpoch"] == old_epoch
    assert untouched_activity["producerEpoch"] == activity["producerEpoch"]

    async def exercise():
        client = broker.relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(old_epoch, 0),
            loop=asyncio.get_running_loop(),
        )
        broker.feed.allocate_events(HIVE, broker.relay._heartbeat)
        await broker.remove_hive(HIVE)
        return client

    client = asyncio.run(exercise())
    state = broker.retained_state()
    assert client.closed and client.close_reason == "hive_removed"
    assert HIVE not in state["hives"]
    assert state["events"] == state["bytes"] == state["clients"] == 0
    assert HIVE not in broker.feed._hives
    assert broker.feed.cached_activity_runs == 0
    assert broker.feed.cached_run_ownerships == 0
    assert all(hive_id != HIVE for hive_id, _run_id in broker.feed._activities)

    replacement = broker.snapshot_with_cursor(HIVE)
    assert replacement["cursor"]["producerEpoch"] != old_epoch
    replacement_activity = broker.activity_with_cursor("run-1")
    assert replacement_activity["producerEpoch"] != activity["producerEpoch"]
    asyncio.run(broker.close())


def test_snapshot_captured_before_removal_cannot_republish_after_removal(tmp_path: Path) -> None:
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    captured = threading.Event()
    release = threading.Event()
    original_resolve = sources.resolve_hive
    responses: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def delayed_resolve(identity: str):
        hive = original_resolve(identity)
        captured.set()
        assert release.wait(2)
        return hive

    def read_snapshot() -> None:
        try:
            responses.append(broker.snapshot_with_cursor(HIVE))
        except BaseException as exc:
            failures.append(exc)

    sources.resolve_hive = delayed_resolve  # type: ignore[method-assign]
    worker = threading.Thread(target=read_snapshot)
    worker.start()
    assert captured.wait(2)

    async def remove_while_read_is_pinned() -> None:
        removal = asyncio.create_task(broker.remove_hive(HIVE))
        await asyncio.sleep(0.01)
        assert not removal.done()
        release.set()
        await asyncio.wait_for(removal, 2)

    asyncio.run(remove_while_read_is_pinned())
    assert HIVE not in broker.feed._hives
    worker.join(2)
    assert not worker.is_alive()

    assert not responses
    assert len(failures) == 1
    assert isinstance(failures[0], operator_sources.OperatorSourceError)
    assert failures[0].code == "hive_generation_expired"
    assert HIVE not in broker.feed._hives
    assert HIVE not in broker.retained_state()["hives"]
    assert not broker.feed._admissions
    assert not broker.feed._removal_markers
    assert not broker.feed._activity_discoveries
    asyncio.run(broker.close())


def test_activity_captured_before_removal_cannot_recreate_evicted_cache(tmp_path: Path) -> None:
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    _write_journal(tmp_path)
    captured = threading.Event()
    release = threading.Event()
    original_locate = sources.locate_run
    responses: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def delayed_locate(run_id: str):
        located = original_locate(run_id)
        captured.set()
        assert release.wait(2)
        return located

    def read_activity() -> None:
        try:
            responses.append(broker.activity_with_cursor("run-1"))
        except BaseException as exc:
            failures.append(exc)

    sources.locate_run = delayed_locate  # type: ignore[method-assign]
    worker = threading.Thread(target=read_activity)
    worker.start()
    assert captured.wait(2)

    async def remove_while_discovery_is_pinned() -> None:
        removal = asyncio.create_task(broker.remove_hive(HIVE))
        await asyncio.sleep(0.01)
        assert not removal.done()
        removal.cancel()
        await asyncio.sleep(0.01)
        assert not removal.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(removal, 2)

    asyncio.run(remove_while_discovery_is_pinned())
    assert broker.feed.cached_activity_runs == 0
    worker.join(2)
    assert not worker.is_alive()

    assert not responses
    assert len(failures) == 1
    assert isinstance(failures[0], operator_sources.OperatorSourceError)
    assert failures[0].code == "hive_generation_expired"
    assert broker.feed.cached_activity_runs == 0
    assert HIVE not in broker.retained_state()["hives"]
    assert not broker.feed._admissions
    assert not broker.feed._removal_markers
    assert not broker.feed._activity_discoveries
    asyncio.run(broker.close())


def test_snapshot_to_subscribe_gap_cannot_recreate_removed_hive_state(tmp_path: Path) -> None:
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    snapshot = broker.snapshot_with_cursor(HIVE)
    old_epoch = str(snapshot["cursor"]["producerEpoch"])
    captured = threading.Event()
    release = threading.Event()
    original_installed = broker.feed.installed_snapshot

    def paused_after_installed_snapshot(identity: str):
        installed = original_installed(identity)
        captured.set()
        assert release.wait(2)
        return installed

    broker.feed.installed_snapshot = paused_after_installed_snapshot  # type: ignore[method-assign]
    encoded_hive = HIVE.replace("/", "%2F")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/v1/hives/{HIVE}/events",
            "raw_path": f"/api/v1/hives/{encoded_hive}/events".encode(),
            "query_string": (f"subscription=hive%3A{encoded_hive}&cursor={old_epoch}%3A0").encode(),
            "headers": [],
            "path_params": {"hive_id": HIVE},
        }
    )

    async def remove_in_handoff_gap():
        event_request = asyncio.create_task(broker.events(request))
        assert await asyncio.to_thread(captured.wait, 2)
        await broker.remove_hive(HIVE)
        removed_state = broker.retained_state()
        broker.relay._start_pump(HIVE)
        late_pump_created = HIVE in broker.relay._pumps
        release.set()
        response = await asyncio.wait_for(event_request, 2)
        await asyncio.sleep(0)
        return response, removed_state, late_pump_created

    response, removed_state, late_pump_created = asyncio.run(remove_in_handoff_gap())
    assert json.loads(response.body) == {"error": "snapshot_required", "action": "resnapshot"}
    assert removed_state["hives"] == {}
    assert removed_state["events"] == 0
    assert removed_state["clients"] == 0
    assert removed_state["inFlightFeedCalls"] == 0
    assert late_pump_created is False
    assert HIVE not in broker.feed.tracked_hive_ids()
    assert HIVE not in broker.relay.tracked_hive_ids()
    assert HIVE not in broker.relay._pumps
    assert HIVE not in broker.retained_state()["hives"]

    broker.feed.installed_snapshot = original_installed  # type: ignore[method-assign]
    replacement = broker.snapshot_with_cursor(HIVE)
    assert replacement["cursor"]["producerEpoch"] != old_epoch
    asyncio.run(broker.close())


def test_subscribe_to_pump_start_is_fenced_by_removal_generation(tmp_path: Path) -> None:
    class MultiProvider:
        def __init__(self) -> None:
            self.current = {
                HIVE: _snapshot("source-revision-one", "open"),
                OTHER_HIVE: _snapshot("source-revision-two", "open", hive=OTHER_HIVE),
            }

        def refresh(self, request):
            return self.current[str(request.hive)]

    sources = _sources(tmp_path, MultiProvider())  # type: ignore[arg-type]
    sources.cfg["managed_repos"].append(
        {
            "provider": "github",
            "org": "beadhive",
            "repo": "other",
            "prefix": "other",
            "kind": "org-native",
        }
    )
    runtime = host_daemon.DaemonRuntime()
    runtime.mark_ready()
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=runtime,
        settings=_settings(),
    )
    snapshot = broker.snapshot_with_cursor(HIVE)
    other_snapshot = broker.snapshot_with_cursor(OTHER_HIVE)
    old_epoch = str(snapshot["cursor"]["producerEpoch"])
    other_epoch = str(other_snapshot["cursor"]["producerEpoch"])
    begin_entered = threading.Event()
    begin_release = threading.Event()
    original_begin = broker.feed.begin_hive_removal

    def paused_after_pump_pop(hive_id: str) -> None:
        begin_entered.set()
        assert begin_release.wait(2)
        original_begin(hive_id)

    broker.feed.begin_hive_removal = paused_after_pump_pop  # type: ignore[method-assign]
    encoded_hive = HIVE.replace("/", "%2F")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/v1/hives/{HIVE}/events",
            "raw_path": f"/api/v1/hives/{encoded_hive}/events".encode(),
            "query_string": (f"subscription=hive%3A{encoded_hive}&cursor={old_epoch}%3A0").encode(),
            "headers": [],
            "path_params": {"hive_id": HIVE},
        }
    )

    async def exercise_removal_race():
        initial_client = broker.relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(old_epoch, 0),
            loop=asyncio.get_running_loop(),
        )
        broker.relay._start_pump(HIVE)
        assert HIVE in broker.relay._pumps
        removal = asyncio.create_task(broker.remove_hive(HIVE))
        assert await asyncio.to_thread(begin_entered.wait, 2)
        response = await broker.events(request)
        late_pump_created = HIVE in broker.relay._pumps
        removal.cancel()
        begin_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(removal, 2)
        return (
            response,
            initial_client,
            late_pump_created,
            broker.retained_state(),
            broker.relay.tracked_hive_ids(),
        )

    response, initial_client, late_pump_created, removed_state, tracked_hives = asyncio.run(
        exercise_removal_race()
    )
    assert response.status_code == 409
    assert initial_client.closed and initial_client.close_reason == "hive_removed"
    assert late_pump_created is False
    assert HIVE not in tracked_hives
    assert HIVE not in removed_state["hives"]
    assert removed_state["clients"] == 0
    assert removed_state["events"] == 0
    assert removed_state["inFlightFeedCalls"] == 0
    assert OTHER_HIVE in removed_state["hives"]
    assert removed_state["hives"][OTHER_HIVE]["producerEpoch"] == other_epoch
    assert HIVE not in broker.feed.tracked_hive_ids()
    assert not broker.relay._pumps
    assert not broker.relay._removal_admissions

    broker.feed.begin_hive_removal = original_begin  # type: ignore[method-assign]
    replacement = broker.snapshot_with_cursor(HIVE)
    assert replacement["cursor"]["producerEpoch"] != old_epoch
    unchanged = broker.snapshot_with_cursor(OTHER_HIVE)
    assert unchanged["cursor"]["producerEpoch"] == other_epoch
    asyncio.run(broker.close())


def test_stale_cached_owner_cannot_escape_new_owner_removal(tmp_path: Path) -> None:
    sources = _sources(tmp_path, MutableProvider())
    sources.cfg["managed_repos"].append(
        {
            "provider": "github",
            "org": "beadhive",
            "repo": "other",
            "prefix": "other",
            "kind": "org-native",
        }
    )
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    _write_journal(tmp_path, "other-stable", hive=OTHER_HIVE)
    old_path = _write_journal(tmp_path, "moved-run")
    old_activity = broker.activity_with_cursor("moved-run")
    old_snapshot = broker.snapshot_with_cursor(HIVE)
    _write_journal(tmp_path, "stable-run")
    stable_activity = broker.activity_with_cursor("stable-run")
    old_path.unlink()
    _write_journal(tmp_path, "moved-run", hive=OTHER_HIVE)

    captured = threading.Event()
    release = threading.Event()
    original_locate = sources.locate_run
    responses: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def delayed_locate(run_id: str):
        located = original_locate(run_id)
        captured.set()
        assert release.wait(2)
        return located

    def read_activity() -> None:
        try:
            responses.append(broker.activity_with_cursor("moved-run"))
        except BaseException as exc:
            failures.append(exc)

    sources.locate_run = delayed_locate  # type: ignore[method-assign]
    worker = threading.Thread(target=read_activity)
    worker.start()
    assert captured.wait(2)

    async def remove_new_owner_while_stale_owner_is_pinned() -> None:
        removal = asyncio.create_task(broker.remove_hive(OTHER_HIVE))
        await asyncio.sleep(0.01)
        assert not removal.done()
        removal.cancel()
        await asyncio.sleep(0.01)
        assert not removal.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(removal, 2)

    asyncio.run(remove_new_owner_while_stale_owner_is_pinned())
    worker.join(2)
    assert not worker.is_alive()
    assert not responses
    assert len(failures) == 1
    assert isinstance(failures[0], operator_sources.OperatorSourceError)
    assert failures[0].code == "hive_generation_expired"
    assert all(hive_id != OTHER_HIVE for hive_id, _run_id in broker.feed._activities)
    assert OTHER_HIVE not in broker.retained_state()["hives"]
    assert broker.retained_state()["inFlightFeedCalls"] == 0
    assert OTHER_HIVE not in broker.feed._admissions
    assert not broker.feed._removal_markers
    assert not broker.feed._activity_discoveries

    sources.locate_run = original_locate  # type: ignore[method-assign]
    assert broker.snapshot_with_cursor(HIVE)["cursor"]["producerEpoch"] == str(
        old_snapshot["cursor"]["producerEpoch"]
    )
    stable_after = broker.activity_with_cursor(
        "stable-run",
        after=(str(stable_activity["producerEpoch"]), int(stable_activity["sequence"])),
    )
    assert stable_after["producerEpoch"] == stable_activity["producerEpoch"]
    replacement = broker.activity_with_cursor("moved-run")
    assert replacement["hiveId"] == OTHER_HIVE
    assert replacement["producerEpoch"] != old_activity["producerEpoch"]
    asyncio.run(broker.close())


def test_unresolved_activity_discoveries_are_globally_bounded(tmp_path: Path) -> None:
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(max_records_per_read=1),
    )
    _write_journal(tmp_path, "run-1")
    _write_journal(tmp_path, "run-2")
    captured = threading.Event()
    release = threading.Event()
    original_locate = sources.locate_run
    responses: list[dict[str, object]] = []

    def delayed_locate(run_id: str):
        located = original_locate(run_id)
        captured.set()
        assert release.wait(2)
        return located

    def read_activity() -> None:
        responses.append(broker.activity_with_cursor("run-1"))

    sources.locate_run = delayed_locate  # type: ignore[method-assign]
    worker = threading.Thread(target=read_activity)
    worker.start()
    assert captured.wait(2)
    assert len(broker.feed._activity_discoveries) == broker.feed.max_activity_discoveries == 1

    with pytest.raises(operator_sources.OperatorSourceError) as exc_info:
        broker.activity_with_cursor("run-2")
    assert exc_info.value.code == "activity_discovery_capacity"
    assert exc_info.value.retryable is True
    assert len(broker.feed._activity_discoveries) == 1

    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert len(responses) == 1
    assert not broker.feed._activity_discoveries
    asyncio.run(broker.close())


def test_active_pump_detects_registry_removal_and_cleans_only_that_hive(tmp_path: Path) -> None:
    runtime = host_daemon.DaemonRuntime()
    runtime.mark_ready()
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=runtime,
        settings=_settings(),
    )
    broker.relay.poll_interval = 0.001
    snapshot = broker.snapshot_with_cursor(HIVE)

    async def exercise():
        client = broker.relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(
                str(snapshot["cursor"]["producerEpoch"]),
                int(snapshot["cursor"]["sequence"]),
            ),
            loop=asyncio.get_running_loop(),
        )
        broker.relay._start_pump(HIVE)
        sources.cfg["managed_repos"] = []
        for _ in range(1_000):
            if HIVE not in broker.retained_state()["hives"]:
                break
            await asyncio.sleep(0.001)
        return client

    client = asyncio.run(exercise())
    assert client.closed and client.close_reason == "hive_removed"
    assert HIVE not in broker.retained_state()["hives"]
    assert HIVE not in broker.feed._hives
    asyncio.run(broker.close())


def test_registry_reconciler_removes_cached_hive_without_clients_or_pumps(tmp_path: Path) -> None:
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
        registry_poll_interval=0.001,
    )
    snapshot = broker.snapshot_with_cursor(HIVE)
    _write_journal(tmp_path)
    broker.activity_with_cursor("run-1")
    broker.feed.allocate_events(HIVE, broker.relay._heartbeat)
    assert broker.retained_state()["events"] == 1
    assert broker.feed.cached_activity_runs == 1
    assert not broker.relay._pumps

    async def exercise() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        component = broker.component()
        async with component.lifespan(None):
            before = broker.retained_state()
            sources.cfg["managed_repos"] = []
            for _ in range(1_000):
                current = broker.retained_state()
                if HIVE not in current["hives"]:
                    break
                await asyncio.sleep(0.001)
            after = broker.retained_state()
        return before, after, broker.retained_state()

    before, after, stopped = asyncio.run(exercise())
    assert before["registryReconciler"]["running"] is True
    assert before["hives"][HIVE]["producerEpoch"] == snapshot["cursor"]["producerEpoch"]
    assert HIVE not in after["hives"]
    assert after["events"] == after["bytes"] == after["clients"] == 0
    assert after["cachedActivityRuns"] == after["cachedActivityBytes"] == 0
    assert after["cachedRunOwnerships"] == 0
    assert after["registryReconciler"]["removedHives"] == 1
    assert after["registryReconciler"]["running"] is True
    assert stopped["registryReconciler"]["running"] is False
    assert after["inFlightFeedCalls"] == 0
    assert HIVE not in broker.feed._hives
    assert not broker.relay._pumps


def test_activity_disappearance_recovers_with_reset_without_rotating_other_run(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    run_path = _write_journal(tmp_path, "run-1")
    _write_journal(tmp_path, "run-2")
    first = broker.activity_with_cursor("run-1")
    other = broker.activity_with_cursor("run-2")
    held_path = tmp_path / "held-run-1.jsonl"
    run_path.rename(held_path)

    with pytest.raises(operator_sources.OperatorSourceError) as missing:
        broker.activity_with_cursor("run-1", after=(first["producerEpoch"], first["sequence"]))
    assert missing.value.code == "run_not_found"

    held_path.rename(run_path)
    recovered = broker.activity_with_cursor(
        "run-1", after=(first["producerEpoch"], first["sequence"])
    )
    unchanged = broker.activity_with_cursor(
        "run-2", after=(other["producerEpoch"], other["sequence"])
    )
    assert recovered["kind"] == "reset"
    assert recovered["resetReason"] == "run_not_found"
    assert recovered["producerEpoch"] != first["producerEpoch"]
    assert recovered["baseSequence"] == 0
    assert len(recovered["activities"]) == 1
    assert unchanged["kind"] == "delta"
    assert unchanged["producerEpoch"] == other["producerEpoch"]
    with pytest.raises(operator_sources.OperatorSourceError) as expired:
        broker.activity_with_cursor("run-1", after=(first["producerEpoch"], first["sequence"]))
    assert expired.value.code == "activity_cursor_expired"
    asyncio.run(broker.close())


def test_activity_source_identity_change_resets_identical_records_and_only_that_run(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, MutableProvider())
    sources.cfg["managed_repos"].append(
        {
            "provider": "github",
            "org": "beadhive",
            "repo": "other",
            "prefix": "other",
            "kind": "org-native",
        }
    )
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    _write_journal(tmp_path, "other-stable", hive=OTHER_HIVE)
    path = _write_journal(tmp_path, "run-1")
    stable_path = _write_journal(tmp_path, "stable-run")
    first = broker.activity_with_cursor("run-1")
    stable = broker.activity_with_cursor("stable-run")
    original_bytes = path.read_bytes()
    old_identity = (path.stat().st_dev, path.stat().st_ino)

    replacement_path = path.with_suffix(".replacement")
    replacement_path.write_bytes(original_bytes)
    replacement_path.replace(path)
    assert (path.stat().st_dev, path.stat().st_ino) != old_identity
    replaced = broker.activity_with_cursor(
        "run-1", after=(str(first["producerEpoch"]), int(first["sequence"]))
    )
    assert replaced["kind"] == "reset"
    assert replaced["resetReason"] == "activity_source_changed"
    assert replaced["producerEpoch"] != first["producerEpoch"]
    replaced_identity = (path.stat().st_dev, path.stat().st_ino)
    with pytest.raises(operator_sources.OperatorSourceError) as expired:
        broker.activity_with_cursor(
            "run-1", after=(str(first["producerEpoch"]), int(first["sequence"]))
        )
    assert expired.value.code == "activity_cursor_expired"

    held_path = tmp_path / "held-original-run-1.jsonl"
    path.rename(held_path)
    moved = _write_journal(tmp_path, "run-1", hive=OTHER_HIVE)
    moved_activity = broker.activity_with_cursor("run-1")
    assert moved_activity["hiveId"] == OTHER_HIVE
    moved.unlink()
    held_path.rename(path)
    assert (path.stat().st_dev, path.stat().st_ino) == replaced_identity
    round_tripped = broker.activity_with_cursor(
        "run-1", after=(str(replaced["producerEpoch"]), int(replaced["sequence"]))
    )
    assert round_tripped["kind"] == "reset"
    assert round_tripped["resetReason"] == "activity_owner_changed"
    assert round_tripped["producerEpoch"] != replaced["producerEpoch"]
    with pytest.raises(operator_sources.OperatorSourceError) as round_trip_expired:
        broker.activity_with_cursor(
            "run-1", after=(str(replaced["producerEpoch"]), int(replaced["sequence"]))
        )
    assert round_trip_expired.value.code == "activity_cursor_expired"

    collision_path = _write_journal(tmp_path, "run-1", hive=OTHER_HIVE)
    with pytest.raises(operator_sources.OperatorSourceError) as collision:
        broker.activity_with_cursor(
            "run-1",
            after=(str(round_tripped["producerEpoch"]), int(round_tripped["sequence"])),
        )
    assert collision.value.code == "ambiguous_run_id"
    collision_path.unlink()
    collision_recovered = broker.activity_with_cursor(
        "run-1",
        after=(str(round_tripped["producerEpoch"]), int(round_tripped["sequence"])),
    )
    assert collision_recovered["kind"] == "reset"
    assert collision_recovered["resetReason"] == "ambiguous_run_id"
    assert collision_recovered["producerEpoch"] != round_tripped["producerEpoch"]

    stable_after = broker.activity_with_cursor(
        "stable-run", after=(str(stable["producerEpoch"]), int(stable["sequence"]))
    )
    assert stable_path.exists()
    assert stable_after["producerEpoch"] == stable["producerEpoch"]
    assert stable_after["kind"] == "delta"
    asyncio.run(broker.close())


def test_concurrent_owner_return_cannot_preserve_the_pre_move_epoch(tmp_path: Path) -> None:
    sources = _sources(tmp_path, MutableProvider())
    sources.cfg["managed_repos"].append(
        {
            "provider": "github",
            "org": "beadhive",
            "repo": "other",
            "prefix": "other",
            "kind": "org-native",
        }
    )
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    _write_journal(tmp_path, "other-stable", hive=OTHER_HIVE)
    _write_journal(tmp_path, "hive-stable")
    path = _write_journal(tmp_path, "run-1")
    first = broker.activity_with_cursor("run-1")
    read_captured = threading.Event()
    release_read = threading.Event()
    original_read = sources.read_run

    def delayed_original_owner_read(hive, source, run_id):
        journal = original_read(hive, source, run_id)
        if hive.identity == HIVE:
            read_captured.set()
            assert release_read.wait(2)
        return journal

    responses: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def read_original_owner() -> None:
        try:
            responses.append(
                broker.activity_with_cursor(
                    "run-1", after=(str(first["producerEpoch"]), int(first["sequence"]))
                )
            )
        except BaseException as exc:
            failures.append(exc)

    sources.read_run = delayed_original_owner_read  # type: ignore[method-assign]
    worker = threading.Thread(target=read_original_owner)
    worker.start()
    assert read_captured.wait(2)

    held_path = tmp_path / "held-concurrent-run-1.jsonl"
    path.rename(held_path)
    moved_path = _write_journal(tmp_path, "run-1", hive=OTHER_HIVE)
    moved = broker.activity_with_cursor("run-1")
    assert moved["hiveId"] == OTHER_HIVE
    moved_path.unlink()
    held_path.rename(path)
    release_read.set()
    worker.join(2)

    assert not worker.is_alive()
    assert not failures
    assert len(responses) == 1
    returned = responses[0]
    assert returned["kind"] == "reset"
    assert returned["resetReason"] == "activity_owner_changed"
    assert returned["producerEpoch"] != first["producerEpoch"]
    assert broker.feed.cached_run_ownerships <= broker.feed.max_run_ownerships
    asyncio.run(broker.close())


@pytest.mark.parametrize("failure_code", ["activity_source_unavailable", "activity_read_cancelled"])
def test_activity_read_interruption_recovers_with_fresh_epoch_and_reset(
    tmp_path: Path,
    failure_code: str,
) -> None:
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    _write_journal(tmp_path)
    first = broker.activity_with_cursor("run-1")
    original_read = sources.read_run

    def interrupted_read(_hive, _source, _run_id):
        raise operator_sources.OperatorSourceError(
            failure_code,
            "The activity read was interrupted.",
            status_code=503,
            retryable=True,
        )

    sources.read_run = interrupted_read  # type: ignore[method-assign]
    with pytest.raises(operator_sources.OperatorSourceError) as interrupted:
        broker.activity_with_cursor("run-1", after=(first["producerEpoch"], first["sequence"]))
    assert interrupted.value.code == failure_code

    sources.read_run = original_read  # type: ignore[method-assign]
    recovered = broker.activity_with_cursor(
        "run-1", after=(first["producerEpoch"], first["sequence"])
    )
    assert recovered["kind"] == "reset"
    assert recovered["resetReason"] == failure_code
    assert recovered["producerEpoch"] != first["producerEpoch"]
    assert [activity["runId"] for activity in recovered["activities"]] == ["run-1"]
    asyncio.run(broker.close())


def test_activity_cancelled_error_is_a_discontinuity_even_though_it_is_base_exception(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, MutableProvider())
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(),
        settings=_settings(),
    )
    _write_journal(tmp_path)
    first = broker.activity_with_cursor("run-1")
    original_read = sources.read_run

    def cancelled_read(_hive, _source, _run_id):
        raise asyncio.CancelledError

    sources.read_run = cancelled_read  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        broker.activity_with_cursor("run-1")

    sources.read_run = original_read  # type: ignore[method-assign]
    recovered = broker.activity_with_cursor("run-1")
    assert recovered["kind"] == "reset"
    assert recovered["resetReason"] == "activity_read_cancelled"
    assert recovered["producerEpoch"] != first["producerEpoch"]
    asyncio.run(broker.close())


def test_cancelled_activity_request_drains_reader_then_fences_exact_run(tmp_path: Path) -> None:
    sources = _sources(tmp_path, MutableProvider())
    runtime = host_daemon.DaemonRuntime()
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=runtime,
        settings=_settings(),
    )
    _write_journal(tmp_path)
    first = broker.activity_with_cursor("run-1")
    entered = threading.Event()
    release = threading.Event()
    original_read = sources.read_run

    def blocked_read(hive, source, run_id):
        entered.set()
        assert release.wait(2)
        return original_read(hive, source, run_id)

    sources.read_run = blocked_read  # type: ignore[method-assign]
    api = operator_api.OperatorAPI(
        sources=sources,
        feed=broker.feed,
        host_id="host-1",
        instance_id="instance-1",
        ready=lambda: runtime.ready,
        snapshot_reader=broker.read_snapshot,
        activity_reader=broker.read_activity,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/runs/run-1/activity",
            "raw_path": b"/api/v1/runs/run-1/activity",
            "query_string": b"",
            "headers": [],
            "path_params": {"run_id": "run-1"},
        }
    )

    async def exercise() -> None:
        task = asyncio.create_task(api.activity(request))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    sources.read_run = original_read  # type: ignore[method-assign]
    recovered = broker.activity_with_cursor("run-1")
    assert recovered["kind"] == "reset"
    assert recovered["resetReason"] == "activity_read_cancelled"
    assert recovered["producerEpoch"] != first["producerEpoch"]
    asyncio.run(broker.close())


def test_cancelled_snapshot_is_drained_and_cleared_by_concurrent_broker_close(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    class BlockedProvider(MutableProvider):
        def refresh(self, request):
            entered.set()
            try:
                assert release.wait(2)
                return super().refresh(request)
            finally:
                exited.set()

    sources = _sources(tmp_path, BlockedProvider())
    original_close = sources.close

    def close_sources() -> None:
        release.set()
        original_close()

    sources.close = close_sources  # type: ignore[method-assign]
    runtime = host_daemon.DaemonRuntime(shutdown_budget=2)
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=runtime,
        settings=_settings(),
    )
    api = operator_api.OperatorAPI(
        sources=sources,
        feed=broker.feed,
        host_id="host-1",
        instance_id="instance-1",
        ready=lambda: runtime.ready,
        snapshot_reader=broker.read_snapshot,
        activity_reader=broker.read_activity,
    )
    encoded_hive = HIVE.replace("/", "%2F")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/v1/hives/{HIVE}/snapshot",
            "raw_path": f"/api/v1/hives/{encoded_hive}/snapshot".encode(),
            "query_string": b"",
            "headers": [],
            "path_params": {"hive_id": HIVE},
        }
    )

    async def exercise() -> None:
        request_task = asyncio.create_task(api.snapshot(request))
        assert await asyncio.to_thread(entered.wait, 2)
        request_task.cancel()
        await asyncio.wait_for(broker.close(), 2)
        with pytest.raises(asyncio.CancelledError):
            await request_task
        assert await asyncio.to_thread(exited.wait, 2)
        await asyncio.sleep(0)
        with pytest.raises(operator_sources.OperatorSourceError) as closing:
            await broker.read_snapshot(HIVE)
        assert closing.value.code == "state_broker_closing"
        rejected = await api.snapshot(request)
        assert rejected.status_code == 503
        assert json.loads(rejected.body)["error"]["code"] == "state_broker_closing"

    asyncio.run(exercise())
    retained = broker.retained_state()
    assert retained["ownedFeedCalls"] == 0
    assert retained["brokerClosing"] is retained["brokerClosed"] is True
    assert HIVE not in broker.feed.tracked_hive_ids()
    assert HIVE not in retained["hives"]
    assert broker.feed.hive_admissions == 0


def test_sync_source_read_is_owned_across_close_and_all_post_close_reads_are_rejected(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    close_called = threading.Event()
    responses: list[dict[str, object]] = []
    failures: list[BaseException] = []

    class BlockedProvider(MutableProvider):
        def refresh(self, request):
            entered.set()
            assert release.wait(2)
            return super().refresh(request)

    sources = _sources(tmp_path, BlockedProvider())
    original_close = sources.close

    def close_sources() -> None:
        close_called.set()
        original_close()

    sources.close = close_sources  # type: ignore[method-assign]
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(shutdown_budget=2),
        settings=_settings(),
    )

    def read_snapshot() -> None:
        try:
            responses.append(broker.snapshot_with_cursor(HIVE))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=read_snapshot)
    worker.start()
    assert entered.wait(2)

    async def close_while_sync_read_is_owned() -> None:
        assert broker.retained_state()["ownedFeedCalls"] == 1
        close_task = asyncio.create_task(broker.close())
        assert await asyncio.to_thread(close_called.wait, 2)
        await asyncio.sleep(0.01)
        assert not close_task.done()
        release.set()
        await asyncio.wait_for(close_task, 2)

        for read in (
            lambda: broker.snapshot_with_cursor(HIVE),
            lambda: broker.activity_with_cursor("run-1"),
        ):
            with pytest.raises(operator_sources.OperatorSourceError) as closing:
                read()
            assert closing.value.code == "state_broker_closing"

        for read in (
            broker.read_snapshot(HIVE),
            broker.read_activity("run-1"),
        ):
            with pytest.raises(operator_sources.OperatorSourceError) as closing:
                await read
            assert closing.value.code == "state_broker_closing"

    asyncio.run(close_while_sync_read_is_owned())
    worker.join(2)
    assert not worker.is_alive()
    assert len(responses) == 1
    assert not failures
    retained = broker.retained_state()
    assert retained["ownedFeedCalls"] == 0
    assert retained["brokerClosing"] is retained["brokerClosed"] is True
    assert HIVE not in broker.feed.tracked_hive_ids()
    assert HIVE not in retained["hives"]
    assert broker.feed.hive_admissions == 0


def test_blocked_registry_reader_is_cancelled_and_drained_before_broker_close(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    close_called = threading.Event()
    sources = _sources(tmp_path, MutableProvider())
    original_close = sources.close

    def blocked_registered_hives():
        entered.set()
        try:
            assert release.wait(2)
            return ()
        finally:
            exited.set()

    def close_sources() -> None:
        close_called.set()
        original_close()

    sources.registered_hives = blocked_registered_hives  # type: ignore[method-assign]
    sources.close = close_sources  # type: ignore[method-assign]
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(shutdown_budget=2),
        settings=_settings(),
        registry_poll_interval=0.001,
    )

    async def close_while_registry_worker_is_owned() -> None:
        reconciler = asyncio.create_task(
            broker._reconcile_registry_loop(),
            name="test:blocked-registry-reconciler",
        )
        broker._registry_task = reconciler
        assert await asyncio.to_thread(entered.wait, 2)
        close_task = asyncio.create_task(broker.close())
        assert await asyncio.to_thread(close_called.wait, 2)
        await asyncio.sleep(0.01)
        assert not close_task.done()
        assert not exited.is_set()
        release.set()
        await asyncio.wait_for(close_task, 2)
        assert exited.is_set()
        assert reconciler.done()

    asyncio.run(close_while_registry_worker_is_owned())
    retained = broker.retained_state()
    assert retained["ownedFeedCalls"] == 0
    assert retained["brokerClosing"] is retained["brokerClosed"] is True
    assert retained["registryReconciler"]["running"] is False
    assert not broker.feed.tracked_hive_ids()
    assert not broker.relay.tracked_hive_ids()


def test_registry_reader_shutdown_timeout_is_typed_and_never_marks_broker_closed(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    sources = _sources(tmp_path, MutableProvider())

    def blocked_registered_hives():
        entered.set()
        try:
            assert release.wait(2)
            return ()
        finally:
            exited.set()

    sources.registered_hives = blocked_registered_hives  # type: ignore[method-assign]
    broker = daemon_state_broker.DaemonStateBroker(
        sources=sources,
        runtime=host_daemon.DaemonRuntime(shutdown_budget=0.01),
        settings=_settings(),
        registry_poll_interval=0.001,
    )

    async def time_out_then_finish_shutdown() -> None:
        reconciler = asyncio.create_task(broker._reconcile_registry_loop())
        broker._registry_task = reconciler
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(operator_sources.OperatorSourceError) as timeout:
            await broker.close()
        assert timeout.value.code == "state_broker_drain_timeout"
        assert timeout.value.retryable is True
        retained = broker.retained_state()
        assert retained["brokerClosing"] is True
        assert retained["brokerClosed"] is False
        assert retained["ownedFeedCalls"] > 0

        release.set()
        await broker.close()
        assert exited.is_set()
        assert reconciler.done()

    asyncio.run(time_out_then_finish_shutdown())
    retained = broker.retained_state()
    assert retained["ownedFeedCalls"] == 0
    assert retained["brokerClosed"] is True
    assert retained["registryReconciler"]["running"] is False


def test_product_application_owns_one_configured_state_broker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = host_daemon.DaemonRuntime()
    app = host_daemon.build_product_application(
        runtime=runtime,
        control_record=host_daemon.ControlRecord(
            contract=host_daemon.CONTRACT_VERSION,
            account_id="uid:test",
            bh_home=str(tmp_path),
            host_id="host-1",
            instance_id="instance-1",
            pid=1,
            process_start="test:1",
            listener_host="127.0.0.1",
            listener_port=8737,
            started_at="2026-09-03T00:00:00+00:00",
        ),
        cfg={"managed_repos": []},
        settings=settings,
    )

    assert isinstance(app.state.state_broker, daemon_state_broker.DaemonStateBroker)
    assert isinstance(app.state.state_broker, daemon_state_broker.StateBroker)
    assert app.state.state_broker.sources is app.state.operator_sources
    assert app.state.state_broker.feed is app.state.operator_feed
    assert app.state.state_broker.relay is app.state.operator_sse
    assert app.state.operator_sse.replay_event_limit == 3
    assert app.state.operator_sse.global_byte_limit == 65_536
    assert app.state.operator_sse.client_event_limit == 2
    assert app.state.operator_sse.heartbeat_interval == 7
    assert isinstance(app.state.operator_sources.provider, PollingStateStreamProvider)
    assert app.state.operator_sources._process_scope is not None

    asyncio.run(app.state.state_broker.close())
