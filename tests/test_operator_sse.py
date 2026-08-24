"""Authoritative OperatorEvent sequencing, replay, and transport tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jsonschema
import pytest
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from beadhive import (
    host_daemon,
    operator_api,
    operator_feed,
    operator_sources,
    operator_sse,
    state_stream,
    state_stream_polling,
)
from beadhive.agent_run_summary import Freshness
from beadhive.public_readers import AgentRunSnapshot, Coverage

NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
HIVE = "github/beadhive/beadhive"
HIVE_TWO = "github/beadhive/second"
UI_OPERATOR_EVENT_FIELDS = {
    "schemaVersion",
    "hiveId",
    "subscriptionId",
    "producerEpoch",
    "sequence",
    "baseSequence",
    "observedAt",
    "generatedAt",
    "source",
    "revision",
    "entity",
    "payload",
}
UI_CONFORMANCE_SHA256 = "0dd82547b2539bdf54deba70e1b45c4516bb848db8eff0dd5354f8fa22d813cc"


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
                priority="P1",
                title="SSE relay",
                updated_at=NOW,
            ),
        ),
    )


class Provider:
    def __init__(self) -> None:
        self.current = _snapshot("beads-1", "open")
        self.fail = False
        self.block = False
        self.refresh_started = threading.Event()
        self.refresh_release = threading.Event()
        self.refresh_finished = threading.Event()
        self.close_called = False

    def refresh(self, _request):
        if self.fail:
            raise RuntimeError("source unavailable")
        if self.block:
            self.refresh_started.set()
            assert self.refresh_release.wait(2)
            self.refresh_finished.set()
        return self.current

    def close(self) -> None:
        self.close_called = True


class MultiProvider:
    def __init__(self) -> None:
        self.current = {
            HIVE: _snapshot("beads-one", "open"),
            HIVE_TWO: _snapshot("beads-two", "open", hive=HIVE_TWO),
        }

    def refresh(self, request):
        return self.current[str(request.hive)]


class ExportBackend:
    """Exercise the production polling adapter with a deterministic engine-shaped read."""

    def __init__(self, records: list[dict]) -> None:
        self.records = records

    def export_jsonl(self, _cwd, out_path, *, env=None):
        del env
        out_path.write_text("".join(f"{json.dumps(row)}\n" for row in self.records))
        return subprocess.CompletedProcess([], 0, "", "")

    def list_gates(self, _cwd):
        return subprocess.CompletedProcess([], 0, "[]", "")


class BlockingCommandBackend:
    """A real process-scope backend command which can outlive the daemon budget."""

    name = "controlled-blocking"

    def __init__(self, started_path: Path) -> None:
        self.started_path = started_path
        self.block = False

    def stream_export_command(self, _cwd, out_path):
        if self.block:
            source = (
                "import os,pathlib,time; "
                f"pathlib.Path({str(self.started_path)!r}).write_text(str(os.getpid())); "
                "time.sleep(300)"
            )
        else:
            record = json.dumps(_raw_issue("open"))
            source = (
                f"import pathlib; pathlib.Path({str(out_path)!r}).write_text({record!r} + '\\n')"
            )
        return [sys.executable, "-c", source]

    def stream_gate_list_command(self, _cwd):
        return [sys.executable, "-c", "print('[]')"]


def _raw_issue(status: str) -> dict:
    return {
        "_type": "issue",
        "id": "bh-1",
        "title": "SSE relay",
        "issue_type": "task",
        "status": status,
        "priority": 1,
        "updated_at": NOW,
        "labels": [],
        "dependencies": [],
        "assignee": None,
    }


def _sources(tmp_path: Path, provider: Provider) -> operator_sources.OperatorSources:
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

    def runtime(host: str, source: str) -> AgentRunSnapshot:
        return AgentRunSnapshot(
            host_id=host,
            source_id=source,
            revision="runtime-1",
            summaries=(),
            coverage=Coverage.UNKNOWN,
            coverage_reason="source_missing",
            freshness=Freshness(),
        )

    return operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-1",
        provider=provider,
        summary_reader=lambda _path, host, source: runtime(host, source),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )


def _multi_sources(tmp_path: Path) -> operator_sources.OperatorSources:
    cfg = {
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": repo,
                "prefix": "bh" if repo == "beadhive" else "second",
                "kind": "org-native",
            }
            for repo in ("beadhive", "second")
        ]
    }
    return operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-1",
        provider=MultiProvider(),
        summary_reader=lambda _path, host, source: AgentRunSnapshot(
            host_id=host,
            source_id=source,
            revision="runtime-1",
            summaries=(),
            coverage=Coverage.UNKNOWN,
            coverage_reason="source_missing",
            freshness=Freshness(),
        ),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, entry: tmp_path / f"{entry['repo']}.jsonl",
    )


def _relay(tmp_path: Path, **limits):
    provider = Provider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider), now_millis=lambda: 1000)
    runtime = host_daemon.DaemonRuntime()
    relay = operator_sse.OperatorEventRelay(
        feed,
        runtime,
        now_millis=lambda: 2000,
        **limits,
    )
    return provider, feed, runtime, relay


def _event(frame: bytes) -> tuple[str, dict]:
    lines = frame.decode().splitlines()
    assert lines[0] == "event: operator-event"
    assert lines[1].startswith("id: ")
    assert lines[2].startswith("data: ")
    return lines[1].removeprefix("id: "), json.loads(lines[2].removeprefix("data: "))


def test_snapshot_boundary_replays_strictly_later_entity_event(tmp_path: Path) -> None:
    provider, feed, _runtime, relay = _relay(tmp_path)
    first = feed.snapshot_with_cursor(HIVE)
    epoch = first["cursor"]["producerEpoch"]
    provider.current = _snapshot("beads-2", "closed")
    second = feed.snapshot_with_cursor(HIVE)

    async def exercise():
        client = relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(str(epoch), 0),
            loop=asyncio.get_running_loop(),
        )
        frame, closed = relay._take(client)
        client.close()
        return frame, closed

    frame, closed = asyncio.run(exercise())
    assert closed is False and frame is not None
    event_id, event = _event(frame)
    assert event_id == f"{epoch}:1"
    assert (event["sequence"], event["baseSequence"]) == (1, 0)
    assert event["payload"]["kind"] == "entity-upsert"
    assert event["entity"] == event["payload"]["entity"]["ref"]
    assert second["cursor"]["sequence"] == 1


def test_concurrent_snapshot_handoff_never_exposes_new_state_with_old_cursor(
    tmp_path: Path,
) -> None:
    provider, feed, _runtime, relay = _relay(tmp_path)
    first = feed.snapshot_with_cursor(HIVE)
    provider.current = _snapshot("beads-2", "closed")
    provider.block = True
    refreshed: list[dict] = []
    installed: list[dict] = []
    refresh_thread = threading.Thread(
        target=lambda: refreshed.append(feed.snapshot_with_cursor(HIVE))
    )
    refresh_thread.start()
    assert provider.refresh_started.wait(1)
    read_thread = threading.Thread(
        target=lambda: installed.append(dict(feed.installed_snapshot(HIVE) or {}))
    )
    read_thread.start()
    read_thread.join(0.02)
    assert read_thread.is_alive(), "installed snapshot read must share the refresh lock"

    provider.refresh_release.set()
    refresh_thread.join(1)
    read_thread.join(1)
    assert not refresh_thread.is_alive() and not read_thread.is_alive()
    assert refreshed[0]["workItems"][0]["record"]["status"] == "closed"
    assert installed[0]["workItems"][0]["record"]["status"] == "closed"
    assert refreshed[0]["cursor"] == installed[0]["cursor"]
    assert refreshed[0]["cursor"]["sequence"] == 1
    event = relay._hives[HIVE].history[0]
    assert event.base_sequence == first["cursor"]["sequence"] == 0
    assert event.sequence == refreshed[0]["cursor"]["sequence"]


def test_production_polling_source_projects_a_real_change_into_sse(
    tmp_path: Path, monkeypatch
) -> None:
    backend = ExportBackend([_raw_issue("open")])
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()
    monkeypatch.setattr(state_stream_polling.registry, "hive_dir", lambda _entry: hive_dir)
    cfg = {
        "git_workspace": {"hive_match": "triplet"},
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": "beadhive",
                "prefix": "bh",
                "kind": "org-native",
            }
        ],
    }
    provider = state_stream_polling.PollingStateStreamProvider(
        cfg,
        backend=backend,
        poll_interval=0,
        sleeper=lambda _seconds: None,
        now=lambda: datetime(2026, 8, 24, tzinfo=UTC),
    )
    sources = operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-1",
        provider=provider,
        summary_reader=lambda _path, host, source: AgentRunSnapshot(
            host_id=host,
            source_id=source,
            revision="runtime-1",
            summaries=(),
            coverage=Coverage.UNKNOWN,
            coverage_reason="source_missing",
            freshness=Freshness(),
        ),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )
    feed = operator_feed.OperatorFeed(sources, now_millis=lambda: 1000)
    relay = operator_sse.OperatorEventRelay(
        feed, host_daemon.DaemonRuntime(), now_millis=lambda: 2000
    )
    first = feed.snapshot_with_cursor(HIVE)
    backend.records = [_raw_issue("closed")]
    second = feed.snapshot_with_cursor(HIVE)

    assert first["workItems"][0]["record"]["status"] == "open"
    assert second["workItems"][0]["record"]["status"] == "closed"
    retained = relay._hives[HIVE].history
    assert len(retained) == 1
    event_id, event = _event(retained[0].frame)
    assert event_id == f"{first['cursor']['producerEpoch']}:1"
    assert event["payload"]["entity"]["record"]["status"] == "closed"


def test_emitted_frame_matches_checked_ui_wire_schema_exactly(tmp_path: Path) -> None:
    """The UI package is cross-repo; its checked shared wire shape is verified here."""

    provider, feed, _runtime, relay = _relay(tmp_path)
    feed.snapshot_with_cursor(HIVE)
    provider.current = _snapshot("beads-2", "closed")
    feed.snapshot_with_cursor(HIVE)
    event = relay._hives[HIVE].history[0].payload
    document = operator_api.openapi_document()
    schema = document["components"]["schemas"]["OperatorEvent"]

    assert set(schema["required"]) == UI_OPERATOR_EVENT_FIELDS
    assert schema["additionalProperties"] is False
    assert set(event) == UI_OPERATOR_EVENT_FIELDS
    contract_uri = "urn:beadhive:host-openapi-v1"
    registry = Registry().with_resource(
        contract_uri,
        Resource.from_contents(document, default_specification=DRAFT202012),
    )
    jsonschema.Draft202012Validator(
        {"$ref": f"{contract_uri}#/components/schemas/OperatorEvent"},
        registry=registry,
    ).validate(event)
    assert event["source"] in {"beads", "runtime", "git", "mcp", "supervisor", "fixture"}
    assert event["baseSequence"] == event["sequence"] - 1
    assert event["entity"] == event["payload"]["entity"]["ref"]


def test_pinned_ui_conformance_frame_is_reproduced_byte_for_byte(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "operator-event-ui-conformance.sse"
    generator = Path(__file__).parent / "proof" / "generate_operator_sse_fixture.py"
    generated = tmp_path / "operator-event-ui-conformance.sse"
    subprocess.run(
        [sys.executable, str(generator), str(generated)],
        cwd=Path(__file__).parents[1],
        check=True,
    )
    frame = fixture.read_bytes()
    assert generated.read_bytes() == frame
    assert hashlib.sha256(frame).hexdigest() == UI_CONFORMANCE_SHA256
    event_id, event = _event(frame)
    assert event_id == f"{event['producerEpoch']}:{event['sequence']}"
    assert set(event) == UI_OPERATOR_EVENT_FIELDS
    assert event["payload"]["kind"] == "entity-upsert"


def test_heartbeat_advances_feed_cursor_and_is_replayable(tmp_path: Path) -> None:
    _provider, feed, _runtime, relay = _relay(tmp_path)
    snapshot = feed.snapshot_with_cursor(HIVE)
    epoch = str(snapshot["cursor"]["producerEpoch"])
    assert feed.allocate_events(HIVE, relay._heartbeat) == 1
    installed = feed.installed_snapshot(HIVE)
    assert installed is not None and installed["cursor"]["sequence"] == 1

    async def exercise():
        client = relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(epoch, 0),
            loop=asyncio.get_running_loop(),
        )
        frame, _closed = relay._take(client)
        client.close()
        return frame

    frame = asyncio.run(exercise())
    assert frame is not None
    event_id, event = _event(frame)
    assert event_id == f"{epoch}:1"
    assert event["payload"] == {"kind": "heartbeat"}
    assert event["source"] == "supervisor"


def test_relay_restart_creates_a_new_epoch_and_expires_the_old_cursor(tmp_path: Path) -> None:
    provider = Provider()
    sources = _sources(tmp_path, provider)
    first_feed = operator_feed.OperatorFeed(sources, now_millis=lambda: 1000)
    first_relay = operator_sse.OperatorEventRelay(first_feed, host_daemon.DaemonRuntime())
    first = first_feed.snapshot_with_cursor(HIVE)
    old_epoch = str(first["cursor"]["producerEpoch"])
    asyncio.run(first_relay.close())

    second_feed = operator_feed.OperatorFeed(sources, now_millis=lambda: 2000)
    second_relay = operator_sse.OperatorEventRelay(second_feed, host_daemon.DaemonRuntime())
    second = second_feed.snapshot_with_cursor(HIVE)
    assert second["cursor"]["producerEpoch"] != old_epoch
    loop = asyncio.new_event_loop()
    with pytest.raises(operator_sse.ResnapshotRequired, match="cursor_epoch_expired"):
        second_relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(old_epoch, 0),
            loop=loop,
        )
    loop.close()
    asyncio.run(second_relay.close())


def test_operator_sources_does_not_close_an_injected_provider(tmp_path: Path) -> None:
    provider = Provider()
    sources = _sources(tmp_path, provider)
    sources.close()
    assert provider.close_called is False


def test_discontinuity_rotates_epoch_and_reset_replaces_old_replay(tmp_path: Path) -> None:
    provider, feed, _runtime, relay = _relay(tmp_path)
    first = feed.snapshot_with_cursor(HIVE)
    old_epoch = str(first["cursor"]["producerEpoch"])
    loop = asyncio.new_event_loop()
    client = relay.subscribe(
        HIVE,
        subscription_id=f"hive:{HIVE}",
        cursor=operator_sse.EventCursor(old_epoch, 0),
        loop=loop,
    )
    provider.current = _snapshot("beads-2", "closed")
    feed.snapshot_with_cursor(HIVE)
    assert len(client.queue) == 1
    provider.fail = True
    with pytest.raises(operator_sources.OperatorSourceError, match="source is unavailable"):
        feed.snapshot_with_cursor(HIVE)
    provider.fail = False
    provider.current = _snapshot("beads-3", "open")
    replacement = feed.snapshot_with_cursor(HIVE)
    new_epoch = str(replacement["cursor"]["producerEpoch"])

    assert new_epoch != old_epoch
    assert replacement["cursor"]["sequence"] == 1
    state = relay.retained_state()
    assert state["hives"][HIVE]["events"] == 1
    reset_frame, _closed = relay._take(client)
    assert reset_frame is not None and len(client.queue) == 0
    event_id, event = _event(reset_frame)
    assert event_id == f"{new_epoch}:1"
    assert event["payload"]["kind"] == "reset"
    with pytest.raises(operator_sse.ResnapshotRequired, match="cursor_epoch_expired"):
        relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(old_epoch, 1),
            loop=loop,
        )
    client.close()
    loop.close()


def test_retention_and_slow_client_are_bounded_independently(tmp_path: Path) -> None:
    _provider, feed, _runtime, relay = _relay(
        tmp_path,
        replay_events=2,
        replay_bytes=100_000,
        global_replay_events=2,
        global_replay_bytes=100_000,
        client_queue_events=1,
        client_queue_bytes=100_000,
    )
    snapshot = feed.snapshot_with_cursor(HIVE)

    async def exercise():
        client = relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(str(snapshot["cursor"]["producerEpoch"]), 0),
            loop=asyncio.get_running_loop(),
        )
        feed.allocate_events(HIVE, relay._heartbeat)
        feed.allocate_events(HIVE, relay._heartbeat)
        return client

    client = asyncio.run(exercise())
    retained = relay.retained_state()
    assert client.closed is True and client.close_reason == "slow_consumer"
    assert retained["clients"] == 0
    assert retained["slowDisconnects"] == 1
    assert retained["events"] == 2


def test_slow_peer_drop_does_not_interrupt_contiguous_healthy_peer_delivery(
    tmp_path: Path,
) -> None:
    _provider, feed, _runtime, relay = _relay(
        tmp_path,
        client_queue_events=2,
        client_queue_bytes=100_000,
    )
    snapshot = feed.snapshot_with_cursor(HIVE)
    epoch = str(snapshot["cursor"]["producerEpoch"])
    loop = asyncio.new_event_loop()
    slow = relay.subscribe(
        HIVE,
        subscription_id=f"hive:{HIVE}",
        cursor=operator_sse.EventCursor(epoch, 0),
        loop=loop,
    )
    healthy = relay.subscribe(
        HIVE,
        subscription_id=f"hive:{HIVE}",
        cursor=operator_sse.EventCursor(epoch, 0),
        loop=loop,
    )
    delivered: list[tuple[int, int]] = []
    for _ in range(3):
        feed.allocate_events(HIVE, relay._heartbeat)
        frame, closed = relay._take(healthy)
        assert frame is not None and closed is False
        _event_id, event = _event(frame)
        delivered.append((event["sequence"], event["baseSequence"]))

    assert delivered == [(1, 0), (2, 1), (3, 2)]
    assert not healthy.closed
    assert slow.closed and slow.close_reason == "slow_consumer"
    healthy.close()
    loop.close()


def test_global_retention_is_shared_across_hives_and_old_cursor_expires(
    tmp_path: Path,
) -> None:
    feed = operator_feed.OperatorFeed(_multi_sources(tmp_path), now_millis=lambda: 1000)
    relay = operator_sse.OperatorEventRelay(
        feed,
        host_daemon.DaemonRuntime(),
        replay_events=10,
        replay_bytes=100_000,
        global_replay_events=2,
        global_replay_bytes=100_000,
    )
    one = feed.snapshot_with_cursor(HIVE)
    two = feed.snapshot_with_cursor(HIVE_TWO)
    feed.allocate_events(HIVE, relay._heartbeat)
    feed.allocate_events(HIVE_TWO, relay._heartbeat)
    feed.allocate_events(HIVE, relay._heartbeat)

    state = relay.retained_state()
    assert state["events"] == 2
    assert state["hives"][HIVE]["events"] == 1
    assert state["hives"][HIVE_TWO]["events"] == 1
    loop = asyncio.new_event_loop()
    with pytest.raises(operator_sse.ResnapshotRequired, match="cursor_expired"):
        relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(str(one["cursor"]["producerEpoch"]), 0),
            loop=loop,
        )
    client = relay.subscribe(
        HIVE_TWO,
        subscription_id=f"hive:{HIVE_TWO}",
        cursor=operator_sse.EventCursor(str(two["cursor"]["producerEpoch"]), 0),
        loop=loop,
    )
    assert len(client.queue) == 1
    client.close()
    loop.close()


def test_byte_bounds_disconnect_slow_client_and_emit_observable_reason(
    tmp_path: Path, caplog
) -> None:
    _provider, feed, _runtime, relay = _relay(
        tmp_path,
        replay_events=10,
        replay_bytes=1,
        global_replay_events=10,
        global_replay_bytes=1,
        client_queue_events=10,
        client_queue_bytes=1,
    )
    snapshot = feed.snapshot_with_cursor(HIVE)
    loop = asyncio.new_event_loop()
    client = relay.subscribe(
        HIVE,
        subscription_id=f"hive:{HIVE}",
        cursor=operator_sse.EventCursor(str(snapshot["cursor"]["producerEpoch"]), 0),
        loop=loop,
    )
    with caplog.at_level("WARNING", logger="beadhive.operator_sse"):
        feed.allocate_events(HIVE, relay._heartbeat)

    state = relay.retained_state()
    assert client.closed and client.close_reason == "slow_consumer"
    assert state["events"] == state["bytes"] == 0
    assert state["slowDisconnects"] == 1
    with pytest.raises(operator_sse.ResnapshotRequired, match="cursor_expired"):
        relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(str(snapshot["cursor"]["producerEpoch"]), 0),
            loop=loop,
        )
    record = next(
        record
        for record in caplog.records
        if getattr(record, "disconnect_reason", None) == "slow_consumer"
    )
    assert record.hive_id == HIVE
    loop.close()

    _provider, global_feed, _runtime, global_relay = _relay(
        tmp_path,
        replay_events=10,
        replay_bytes=100_000,
        global_replay_events=10,
        global_replay_bytes=1,
        client_queue_events=10,
        client_queue_bytes=100_000,
    )
    global_feed.snapshot_with_cursor(HIVE)
    global_feed.allocate_events(HIVE, global_relay._heartbeat)
    global_state = global_relay.retained_state()
    assert global_state["events"] == global_state["bytes"] == 0
    global_loop = asyncio.new_event_loop()
    with pytest.raises(operator_sse.ResnapshotRequired, match="cursor_expired"):
        global_relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(str(global_state["hives"][HIVE]["producerEpoch"]), 0),
            loop=global_loop,
        )
    global_loop.close()


def test_detects_retained_cursor_gap_instead_of_rewriting_continuity(tmp_path: Path) -> None:
    _provider, feed, _runtime, relay = _relay(tmp_path)
    snapshot = feed.snapshot_with_cursor(HIVE)
    for _ in range(3):
        feed.allocate_events(HIVE, relay._heartbeat)
    loop = asyncio.new_event_loop()
    with pytest.raises(operator_sse.ResnapshotRequired, match="cursor_in_future"):
        relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(str(snapshot["cursor"]["producerEpoch"]), 4),
            loop=loop,
        )
    relay._hives[HIVE].history.remove(relay._hives[HIVE].history[1])
    with pytest.raises(operator_sse.ResnapshotRequired, match="cursor_gap"):
        relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(str(snapshot["cursor"]["producerEpoch"]), 0),
            loop=loop,
        )
    loop.close()


def test_close_drains_blocking_source_worker_before_removing_feed_observers(
    tmp_path: Path,
) -> None:
    provider, feed, runtime, relay = _relay(tmp_path, poll_interval=0.001, heartbeat_interval=60)
    snapshot = feed.snapshot_with_cursor(HIVE)
    provider.current = _snapshot("beads-2", "closed")
    provider.block = True
    runtime.mark_ready()

    async def exercise() -> None:
        client = relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(
                str(snapshot["cursor"]["producerEpoch"]),
                int(snapshot["cursor"]["sequence"]),
            ),
            loop=asyncio.get_running_loop(),
        )
        relay._start_pump(HIVE)
        while not provider.refresh_started.is_set():
            await asyncio.sleep(0.001)
        close_task = asyncio.create_task(relay.close())
        await asyncio.sleep(0.02)
        assert not close_task.done()
        assert relay.retained_state()["inFlightFeedCalls"] == 1
        assert feed._transition_handler is not None
        provider.refresh_release.set()
        await asyncio.wait_for(close_task, 1)
        assert provider.refresh_finished.is_set()
        assert feed._transition_handler is None
        assert relay.retained_state()["inFlightFeedCalls"] == 0
        assert client.closed and client.close_reason == "daemon_shutdown"

    asyncio.run(exercise())


def test_production_process_owner_terminates_blocked_backend_within_shutdown_budget(
    tmp_path: Path, monkeypatch
) -> None:
    started_path = tmp_path / "blocked-backend.pid"
    backend = BlockingCommandBackend(started_path)
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()
    monkeypatch.setattr(state_stream_polling.engine, "get_engine", lambda _cfg: backend)
    monkeypatch.setattr(state_stream_polling.registry, "hive_dir", lambda _entry: hive_dir)
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
    runtime = host_daemon.DaemonRuntime(shutdown_budget=1.0)
    process_timeout, process_grace = operator_sources.process_limits_for_shutdown(
        runtime.shutdown_budget
    )
    sources = operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-1",
        summary_reader=lambda _path, host, source: AgentRunSnapshot(
            host_id=host,
            source_id=source,
            revision="runtime-1",
            summaries=(),
            coverage=Coverage.UNKNOWN,
            coverage_reason="source_missing",
            freshness=Freshness(),
        ),
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
        process_timeout=process_timeout,
        process_term_grace=process_grace,
    )
    assert sources._process_scope is not None
    assert sources._process_scope.timeout < runtime.shutdown_budget
    feed = operator_feed.OperatorFeed(sources, now_millis=lambda: 1000)
    relay = operator_sse.OperatorEventRelay(
        feed, runtime, poll_interval=0.001, heartbeat_interval=60
    )
    snapshot = feed.snapshot_with_cursor(HIVE)
    backend.block = True
    runtime.mark_ready()

    async def exercise() -> tuple[operator_sse.EventSubscription, float]:
        client = relay.subscribe(
            HIVE,
            subscription_id=f"hive:{HIVE}",
            cursor=operator_sse.EventCursor(
                str(snapshot["cursor"]["producerEpoch"]),
                int(snapshot["cursor"]["sequence"]),
            ),
            loop=asyncio.get_running_loop(),
        )
        relay._start_pump(HIVE)
        while not started_path.exists():
            await asyncio.sleep(0.001)
        started = time.monotonic()
        await asyncio.wait_for(relay.close(), runtime.shutdown_budget)
        return client, time.monotonic() - started

    client, elapsed = asyncio.run(exercise())
    pid = int(started_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert elapsed < runtime.shutdown_budget
    assert client.closed and client.close_reason == "daemon_shutdown"
    assert relay.retained_state()["inFlightFeedCalls"] == 0
    assert feed._transition_handler is None
    assert not feed._install_observers


def test_event_route_rejects_conflicts_and_checked_resnapshot(tmp_path: Path) -> None:
    _provider, feed, runtime, relay = _relay(tmp_path)
    snapshot = feed.snapshot_with_cursor(HIVE)
    epoch = snapshot["cursor"]["producerEpoch"]

    from beadhive import operator_api

    api = operator_api.OperatorAPI(
        sources=feed.sources,
        feed=feed,
        host_id="host-1",
        instance_id="instance-1",
        ready=lambda: runtime.ready,
        events=relay.events,
    )
    app = host_daemon.build_application(runtime=runtime, routes=api.routes())

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420"
            ) as client:
                conflict = await client.get(
                    "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/events",
                    params={"subscription": f"hive:{HIVE}", "after": f"{epoch}:0"},
                    headers={"Last-Event-ID": f"{epoch}:1"},
                )
                expired = await client.get(
                    "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/events",
                    params={"subscription": "hive:wrong", "after": f"{epoch}:0"},
                )
                alias = await client.get(
                    "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/events",
                    params={"subscription": "hive:wrong", "cursor": f"{epoch}:0"},
                )
                identical = await client.get(
                    "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/events",
                    params={
                        "subscription": "hive:wrong",
                        "after": f"{epoch}:0",
                        "cursor": f"{epoch}:0",
                    },
                    headers={"Last-Event-ID": f"{epoch}:0"},
                )
                alias_conflict = await client.get(
                    "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/events",
                    params={
                        "subscription": f"hive:{HIVE}",
                        "after": f"{epoch}:0",
                        "cursor": f"{epoch}:1",
                    },
                )
                return conflict, expired, alias, identical, alias_conflict

    conflict, expired, alias, identical, alias_conflict = asyncio.run(exercise())
    assert (conflict.status_code, conflict.json()["error"]["code"]) == (
        400,
        "conflicting_event_cursors",
    )
    assert expired.status_code == 409
    assert expired.json() == {"error": "wrong_subscription", "action": "resnapshot"}
    assert alias.json() == identical.json() == expired.json()
    assert (alias_conflict.status_code, alias_conflict.json()["error"]["code"]) == (
        400,
        "conflicting_event_cursors",
    )
