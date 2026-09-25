"""ASGI and checked-contract coverage for the local operator read profile."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jsonschema
import pytest
from starlette.middleware import Middleware

from beadhive import (
    daemon_auth,
    daemon_contract,
    daemon_state_broker,
    frame_bridge,
    frame_bridge_runtime,
    frame_bridge_upstream,
    host_daemon,
    operator_api,
    operator_contract,
    operator_feed,
    operator_sources,
    operator_sse,
    run_journal,
    state_stream,
)
from beadhive.agent_run_summary import Freshness
from beadhive.public_readers import AgentRunSnapshot, Coverage

NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
HIVE = "github/beadhive/beadhive"
DIGEST = "sha256:" + "a" * 64


class Provider:
    def refresh(self, _request):
        return state_stream.ProviderSnapshot(
            scope="hive",
            revision="beads-1",
            as_of=NOW,
            issues=(
                state_stream.StreamIssue(
                    id="bh-1",
                    hive=HIVE,
                    issue_type="task",
                    status="open",
                    priority="P1",
                    title="Operator API",
                    updated_at=NOW,
                ),
            ),
        )


def _app(tmp_path: Path, *, cfg=None, provider=None, now_millis=None):
    now_millis = now_millis or (lambda: 1000)
    cfg = cfg or {
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

    sources = operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-1",
        provider=provider or Provider(),
        summary_reader=lambda _path, host, source: runtime(host, source),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )
    feed = operator_feed.OperatorFeed(sources, now_millis=now_millis)
    daemon_runtime = host_daemon.DaemonRuntime()
    relay = operator_sse.OperatorEventRelay(feed, daemon_runtime, now_millis=now_millis)

    async def read_snapshot(identity: str):
        return feed.snapshot_with_cursor(identity)

    async def read_activity(run_id: str, after: tuple[str, int] | None):
        return feed.activity_with_cursor(run_id, after=after)

    api = operator_api.OperatorAPI(
        sources=sources,
        feed=feed,
        host_id="host-1",
        instance_id="instance-1",
        ready=lambda: daemon_runtime.ready,
        events=relay.events,
        snapshot_reader=read_snapshot,
        activity_reader=read_activity,
    )
    app = host_daemon.build_application(
        runtime=daemon_runtime,
        routes=api.routes(),
        components=[relay.component()],
        middleware=[
            Middleware(
                operator_api.LocalReadPolicyMiddleware,
                listener_host="127.0.0.1",
                listener_port=8420,
                allowed_origin="http://127.0.0.1:3000",
            )
        ],
    )
    app.state.operator_feed = feed
    app.state.operator_relay = relay
    return app


def _exercise(tmp_path: Path, action, **app_kwargs):
    app = _app(tmp_path, **app_kwargs)

    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420"
            ) as client:
                return await action(client, app)

    return asyncio.run(run())


def test_phase_one_gets_are_unauthenticated_direct_and_path_free(tmp_path: Path) -> None:
    async def action(client, _app):
        factory = await client.get("/api/v1/factory")
        snapshot = await client.get("/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot")
        health = await client.get("/health")
        return factory, snapshot, health

    factory, snapshot, health = _exercise(tmp_path, action)
    assert factory.status_code == snapshot.status_code == health.status_code == 200
    assert factory.json()["host"] == {
        "hostId": "host-1",
        "serviceInstanceId": "instance-1",
    }
    assert factory.json()["hives"][0]["hiveId"] == HIVE
    assert not ({"workspaceRoot", "worktrees", "edges"} & factory.json().keys())
    assert snapshot.json()["hive"]["prefix"] == HIVE
    snapshot_payload = snapshot.json()
    assert snapshot_payload["cursor"]["subscriptionId"] == (
        operator_contract.hive_subscription_id(HIVE)
    )
    assert daemon_contract.HiveSnapshotResponse.model_validate(snapshot_payload)
    assert health.json() == {
        "schemaVersion": 1,
        "status": "live",
        "ready": True,
        "contract": host_daemon.CONTRACT_VERSION,
    }
    assert snapshot.headers["cache-control"] == "no-store"


class OverloadedSnapshotProvider:
    def refresh(self, _request):
        return state_stream.ProviderSnapshot(
            scope="hive",
            revision="beads-overloaded",
            as_of=NOW,
            issues=tuple(
                state_stream.StreamIssue(
                    id=f"bh-overload-{index}",
                    hive=HIVE,
                    issue_type="task",
                    status="open",
                    priority="P1",
                    title=f"Overloaded {index}",
                    updated_at=NOW,
                )
                for index in range(operator_contract.DEVELOPMENT_WORK_ITEM_LIMIT + 1)
            ),
        )


class NearLimitSnapshotProvider:
    def refresh(self, _request):
        return state_stream.ProviderSnapshot(
            scope="hive",
            revision="beads-near-limit",
            as_of=NOW,
            issues=tuple(
                state_stream.StreamIssue(
                    id=f"bh-{index}",
                    hive=HIVE,
                    issue_type="task",
                    status="open",
                    priority="P1",
                    title="x" * (1_050 if index == 0 else 534),
                    updated_at=NOW,
                )
                for index in range(operator_contract.DEVELOPMENT_WORK_ITEM_LIMIT)
            ),
        )


def test_snapshot_overload_returns_bounded_partial_while_health_stays_ready(tmp_path: Path) -> None:
    async def action(client, _app):
        snapshot = await client.get("/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot")
        health = await client.get("/health")
        return snapshot, health

    snapshot, health = _exercise(tmp_path, action, provider=OverloadedSnapshotProvider())

    assert snapshot.status_code == 200
    assert len(snapshot.content) <= operator_contract.DEVELOPMENT_SNAPSHOT_MAX_BYTES
    payload = snapshot.json()
    assert payload["coverage"]["state"] == "partial"
    assert payload["coverage"]["eligible"] == 4_097
    assert payload["coverage"]["reason"] in {"byte_budget", "structural_cap"}
    assert len(payload["workItems"]) <= 4_096
    daemon_contract.HiveSnapshotResponse.model_validate(payload)
    assert health.status_code == 200
    assert health.json()["ready"] is True


def test_near_limit_host_snapshot_survives_cursor_growth_to_json_safe_maximum(
    tmp_path: Path,
) -> None:
    clock = [1_000]

    async def action(client, app):
        initial = await client.get("/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot")
        feed = app.state.operator_feed
        relay = app.state.operator_relay
        state = feed._hives[HIVE]

        state.sequence = 10**15 - 1
        state.snapshot["cursor"]["sequence"] = state.sequence
        relay._hives[HIVE].sequence = state.sequence
        clock[0] = operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE
        assert feed.allocate_events(HIVE, relay._heartbeat) == 10**15
        crossed = await client.get("/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot")

        clock[0] = operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE + 1
        with pytest.raises(RuntimeError, match="timestamp is outside the wire bound"):
            feed.allocate_events(HIVE, relay._heartbeat)
        assert relay._hives[HIVE].sequence == state.sequence == 10**15

        state.sequence = operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE - 1
        state.snapshot["cursor"]["sequence"] = state.sequence
        relay._hives[HIVE].sequence = state.sequence
        clock[0] = operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE
        assert (
            feed.allocate_events(HIVE, relay._heartbeat)
            == operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE
        )
        maximum = await client.get("/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot")
        with pytest.raises(RuntimeError, match="fit the cursor contract"):
            feed.allocate_events(HIVE, relay._heartbeat)
        return initial, crossed, maximum, relay._hives[HIVE].history[-1].frame

    initial, crossed, maximum, last_frame = _exercise(
        tmp_path,
        action,
        provider=NearLimitSnapshotProvider(),
        now_millis=lambda: clock[0],
    )

    for response in (initial, crossed, maximum):
        assert response.status_code == 200
        assert len(response.content) <= operator_contract.DEVELOPMENT_SNAPSHOT_MAX_BYTES
        payload = frame_bridge_upstream._strict_json_object(response.content)
        validated = daemon_contract.HiveSnapshotResponse.model_validate(payload)
        assert frame_bridge_runtime._safe_timestamp(validated.cursor.sequence) >= 0
        assert frame_bridge_runtime._safe_timestamp(validated.cursor.observed_at) >= 0
    assert operator_contract.DEVELOPMENT_SNAPSHOT_MAX_BYTES - len(maximum.content) < 2_048
    assert maximum.json()["cursor"]["sequence"] == (
        operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE
    )
    assert maximum.json()["cursor"]["observedAt"] == (
        operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE
    )
    data = next(line for line in last_frame.splitlines() if line.startswith(b"data: "))
    event = json.loads(data.removeprefix(b"data: "))
    assert event["sequence"] == operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE
    assert event["observedAt"] == operator_contract.DEVELOPMENT_MAX_JSON_SAFE_INTEGER
    assert event["generatedAt"] == operator_contract.DEVELOPMENT_MAX_JSON_SAFE_INTEGER
    assert frame_bridge_runtime._safe_timestamp(event["observedAt"]) >= 0
    assert frame_bridge_runtime._safe_timestamp(event["generatedAt"]) >= 0
    assert daemon_contract.OperatorEvent.model_validate(event)


class FactoryProvider:
    def __init__(self) -> None:
        self.revisions = {
            "github/beadhive/alpha": "alpha-1",
            "github/beadhive/beadhive": "beadhive-1",
        }

    def refresh(self, request):
        hive = request.hive
        if hive == "github/beadhive/zebra":
            raise OSError("unavailable")
        issues = ()
        if hive == HIVE:
            issues = (
                state_stream.StreamIssue(
                    id="bh-ready",
                    hive=hive,
                    issue_type="task",
                    status="open",
                    priority="P1",
                    title="Ready",
                    updated_at=NOW,
                ),
                state_stream.StreamIssue(
                    id="bh-waiting",
                    hive=hive,
                    issue_type="task",
                    status="open",
                    priority="P1",
                    title="Waiting",
                    updated_at=NOW,
                    dependencies=(
                        state_stream.StreamDependency(
                            issue_id="bh-waiting",
                            depends_on_id="bh-active",
                            type="blocks",
                        ),
                    ),
                ),
                state_stream.StreamIssue(
                    id="bh-active",
                    hive=hive,
                    issue_type="task",
                    status="in_progress",
                    priority="P1",
                    title="Active",
                    updated_at=NOW,
                ),
                state_stream.StreamIssue(
                    id="bh-blocked",
                    hive=hive,
                    issue_type="task",
                    status="blocked",
                    priority="P1",
                    title="Blocked",
                    updated_at=NOW,
                ),
                state_stream.StreamIssue(
                    id="bh-closed-with-edge",
                    hive=hive,
                    issue_type="task",
                    status="closed",
                    priority="P1",
                    title="Closed with retained dependency",
                    updated_at=NOW,
                    dependencies=(
                        state_stream.StreamDependency(
                            issue_id="bh-closed-with-edge",
                            depends_on_id="bh-active",
                            type="blocks",
                        ),
                    ),
                ),
            )
        return state_stream.ProviderSnapshot(
            scope="hive",
            revision=self.revisions[hive],
            as_of=NOW,
            issues=issues,
        )


def _factory_cfg():
    return {
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": repo,
                "prefix": repo,
                "kind": "org-native",
            }
            for repo in ("zebra", "beadhive", "alpha")
        ]
    }


async def _warm_directory(client, app) -> httpx.Response:
    """Read the directory cold, then wait for the background refreshes it scheduled."""

    cold = await client.get("/api/v1/factory/hives")
    summaries = app.state.operator_feed.sources.hive_summaries
    assert await asyncio.to_thread(summaries.wait_idle, 10)
    return cold


def test_factory_hives_are_bounded_deterministic_and_distinguish_unavailable(
    tmp_path: Path,
) -> None:
    provider = FactoryProvider()

    async def action(client, app):
        await _warm_directory(client, app)
        first = await client.get("/api/v1/factory/hives", params={"limit": 2})
        second = await client.get(
            "/api/v1/factory/hives", params={"limit": 2, "cursor": first.json()["nextCursor"]}
        )
        unavailable = await client.get(
            "/api/v1/factory/hives", params={"availability": "unavailable"}
        )
        unchanged = await client.get(
            "/api/v1/factory/hives",
            params={"limit": 2},
            headers={"If-None-Match": first.headers["etag"]},
        )
        return first, second, unavailable, unchanged

    first, second, unavailable, unchanged = _exercise(
        tmp_path, action, cfg=_factory_cfg(), provider=provider
    )
    assert first.status_code == second.status_code == unavailable.status_code == 200
    assert [item["id"] for item in first.json()["items"]] == [
        "github/beadhive/alpha",
        "github/beadhive/beadhive",
    ]
    assert first.json()["returnedCount"] == 2
    assert first.json()["truncated"] is True
    assert second.json()["items"][0]["id"] == "github/beadhive/zebra"
    assert second.json()["items"][0]["counts"] == {
        "open": None,
        "ready": None,
        "active": None,
        "blocked": None,
    }
    assert unavailable.json()["items"][0]["availability"] == {
        "state": "unavailable",
        "reason": "snapshot_source_unavailable",
    }
    beadhive = first.json()["items"][1]
    assert beadhive["counts"] == {"open": 2, "ready": 1, "active": 1, "blocked": 2}
    assert beadhive["freshness"]["state"] == "fresh"
    assert beadhive["opaqueRef"].startswith("hive-sha256-")
    assert [action["id"] for action in beadhive["advertisedActions"]] == [
        "hive.inspect",
        "hive.refresh",
    ]
    assert all(
        action["target"] == {"hiveId": HIVE, "kind": "hive", "id": HIVE}
        for action in beadhive["advertisedActions"]
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert first.headers["cache-control"] == "no-cache"
    assert unchanged.headers["cache-control"] == "no-cache"

    schema = operator_api.openapi_document()["components"]["schemas"]
    jsonschema.Draft202012Validator(
        {
            "components": {"schemas": schema},
            "$ref": "#/components/schemas/FactoryHivePage",
        }
    ).validate(first.json())


def test_factory_hive_cursor_limits_filters_and_revisions_are_checked(tmp_path: Path) -> None:
    provider = FactoryProvider()

    async def action(client, app):
        await _warm_directory(client, app)
        bad_limits = [
            await client.get("/api/v1/factory/hives", params={"limit": value})
            for value in (0, 201, "many")
        ]
        malformed = await client.get("/api/v1/factory/hives", params={"cursor": "%%%"})
        first = await client.get("/api/v1/factory/hives", params={"limit": 1})
        wrong_filter = await client.get(
            "/api/v1/factory/hives",
            params={"limit": 1, "availability": "available", "cursor": first.json()["nextCursor"]},
        )
        # A background summary refresh changes content, never the cursor's membership scope.
        sources = app.state.operator_feed.sources
        provider.revisions["github/beadhive/alpha"] = "alpha-2"
        sources.hive_summaries.mark_dirty("github/beadhive/alpha")
        await client.get("/api/v1/factory/hives")
        assert await asyncio.to_thread(sources.hive_summaries.wait_idle, 10)
        refreshed = await client.get(
            "/api/v1/factory/hives",
            params={"limit": 1, "cursor": first.json()["nextCursor"]},
        )
        # A registry membership change still invalidates the open cursor.
        sources.cfg["managed_repos"].append(
            {
                "provider": "github",
                "org": "beadhive",
                "repo": "newcomer",
                "prefix": "newcomer",
                "kind": "org-native",
            }
        )
        stale = await client.get(
            "/api/v1/factory/hives",
            params={"limit": 1, "cursor": first.json()["nextCursor"]},
        )
        return bad_limits, malformed, wrong_filter, (first, refreshed), stale

    bad_limits, malformed, wrong_filter, (first, refreshed), stale = _exercise(
        tmp_path, action, cfg=_factory_cfg(), provider=provider
    )
    assert refreshed.status_code == 200
    assert [item["id"] for item in refreshed.json()["items"]] == ["github/beadhive/beadhive"]
    assert refreshed.json()["revision"] != first.json()["revision"]
    assert [item.status_code for item in bad_limits] == [400, 400, 400]
    assert malformed.json()["error"]["code"] == "invalid_hive_cursor"
    assert (wrong_filter.status_code, wrong_filter.json()["error"]["code"]) == (
        409,
        "hive_cursor_scope_mismatch",
    )
    assert (stale.status_code, stale.json()["error"]["code"]) == (
        409,
        "hive_cursor_revision_mismatch",
    )


def test_unsafe_hive_representations_are_refused_before_source_read(tmp_path: Path) -> None:
    async def action(client, _app):
        return [
            await client.get("/api/v1/hives/github/beadhive/beadhive/snapshot"),
            await client.get("/api/v1/hives/github%252Fbeadhive%252Fbeadhive/snapshot"),
            await client.get("/api/v1/hives/bh/snapshot"),
            await client.get("/api/v1/hives/github%2Fbeadhive%2F..%2Fsnapshot"),
        ]

    responses = _exercise(tmp_path, action)
    assert [response.status_code for response in responses] == [400, 400, 400, 400]
    assert all("error" in response.json() for response in responses)


def test_host_origin_peer_and_read_only_profile_fail_closed(tmp_path: Path) -> None:
    async def action(client, app):
        bad_host = await client.get("/api/v1/factory", headers={"host": "attacker.test"})
        bad_origin = await client.get(
            "/api/v1/factory", headers={"origin": "https://attacker.test"}
        )
        allowed_origin = await client.get(
            "/api/v1/factory", headers={"origin": "http://127.0.0.1:3000"}
        )
        write = await client.post("/api/v1/factory")
        preflight = await client.options(
            "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/events",
            headers={
                "origin": "http://127.0.0.1:3000",
                "access-control-request-method": "GET",
                "access-control-request-headers": "Last-Event-ID",
            },
        )
        unsafe_preflight = await client.options(
            "/api/v1/factory",
            headers={
                "origin": "http://127.0.0.1:3000",
                "access-control-request-method": "GET",
                "access-control-request-headers": "Authorization",
            },
        )
        absent = [
            await client.get("/mcp"),
            await client.get("/api/v1/terminal/attach-token"),
            await client.get("/ws/terminal"),
        ]
        remote_transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 5000))
        async with httpx.AsyncClient(
            transport=remote_transport, base_url="http://127.0.0.1:8420"
        ) as remote:
            nonloopback = await remote.get("/api/v1/factory")
        return (
            bad_host,
            bad_origin,
            allowed_origin,
            write,
            preflight,
            unsafe_preflight,
            absent,
            nonloopback,
        )

    (
        bad_host,
        bad_origin,
        allowed_origin,
        write,
        preflight,
        unsafe_preflight,
        absent,
        nonloopback,
    ) = _exercise(tmp_path, action)
    assert (bad_host.status_code, bad_host.json()["error"]["code"]) == (400, "invalid_host")
    assert (bad_origin.status_code, bad_origin.json()["error"]["code"]) == (
        403,
        "invalid_origin",
    )
    assert allowed_origin.status_code == 200
    assert allowed_origin.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    assert (write.status_code, write.json()["error"]["code"]) == (405, "read_only_profile")
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    assert preflight.headers["access-control-allow-methods"] == "GET"
    assert preflight.headers["access-control-allow-headers"] == "Last-Event-ID"
    assert (unsafe_preflight.status_code, unsafe_preflight.json()["error"]["code"]) == (
        403,
        "invalid_preflight",
    )
    # The local read-only compatibility profile sees the reserved POST route but cannot invoke it.
    assert [response.status_code for response in absent] == [404, 405, 404]
    assert (nonloopback.status_code, nonloopback.json()["error"]["code"]) == (
        403,
        "non_loopback_client",
    )


def test_exact_activity_get_returns_direct_frame_and_accepts_after_cursor(
    tmp_path: Path,
) -> None:
    path = run_journal.journal_path_for_hive(HIVE, "run-1", base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "version": run_journal.VERSION,
        "source_revision": "opaque:first",
        "timestamp_ms": 1,
        "run_id": "run-1",
        "hive": HIVE,
        "bead": "bh-1",
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": DIGEST,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created", "phase": "planned"},
    }
    path.write_text(json.dumps(record) + "\n")

    async def action(client, _app):
        snapshot = await client.get("/api/v1/runs/run-1/activity")
        body = snapshot.json()
        cursor = f"{body['producerEpoch']}:{body['sequence']}"
        delta = await client.get("/api/v1/runs/run-1/activity", params={"after": cursor})
        return snapshot, delta

    snapshot, delta = _exercise(tmp_path, action)
    assert snapshot.status_code == delta.status_code == 200
    assert snapshot.json()["kind"] == "snapshot"
    assert snapshot.json()["activities"][0]["runId"] == "run-1"
    assert delta.json()["kind"] == "delta"
    assert delta.json()["activities"] == []
    assert delta.json()["baseSequence"] == delta.json()["sequence"] == 1


def test_openapi_artifact_matches_running_route_table_and_omits_mcp(tmp_path: Path) -> None:
    async def action(client, app):
        return await client.get("/openapi.json"), app

    response, app = _exercise(tmp_path, action)
    checked = operator_api.openapi_document()
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert checked["openapi"] == "3.1.0"
    assert checked["security"] == [{"BearerAuth": []}]
    assert checked["paths"]["/health"]["get"]["security"] == []
    assert (
        checked["components"]["securitySchemes"]["BearerAuth"]["x-beadhive-required-scope"]
        == "operator:read"
    )
    assert "BearerAuth" in checked["components"]["securitySchemes"]
    assert "/mcp" not in checked["paths"]

    running = {
        route.path.replace("{hive_id:path}", "{hive_id}")
        for route in app.routes
        if hasattr(route, "path")
    }
    assert running == set(checked["paths"])
    artifact = Path(operator_api.__file__).parent / "schemas" / operator_api.OPENAPI_CONTRACT
    assert json.loads(artifact.read_text()) == checked


def test_product_factory_composes_operator_state_into_daemon_core(tmp_path: Path) -> None:
    runtime = host_daemon.DaemonRuntime()
    record = host_daemon.ControlRecord(
        contract=host_daemon.CONTRACT_VERSION,
        account_id="uid:1234",
        bh_home=str(tmp_path),
        host_id="host-1",
        instance_id="instance-1",
        pid=1234,
        process_start="test:1",
        listener_host="127.0.0.1",
        listener_port=8420,
        started_at=NOW,
    )
    app = host_daemon.build_product_application(
        runtime=runtime,
        state_broker_factory=daemon_state_broker.DaemonStateBroker.for_host,
        control_record=record,
        listener_host="127.0.0.1",
        listener_port=8420,
        cfg={"managed_repos": []},
    )
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert paths == {
        "/health",
        "/api/v1/factory",
        "/api/v1/factory/hives",
        "/api/v1/hives/{hive_id:path}/snapshot",
        "/api/v1/hives/{hive_id:path}/snapshot-with-work-items",
        "/api/v1/hives/{hive_id:path}/work-item-pages",
        "/api/v1/hives/{hive_id:path}/work-item-details/{bead_id}",
        "/api/v1/hives/{hive_id:path}/work-items",
        "/api/v1/hives/{hive_id:path}/work-items/{bead_id}",
        "/api/v1/hives/{hive_id:path}/events",
        "/api/v1/runs/{run_id}/activity",
        "/api/v1/terminal/attach-token",
        "/openapi.json",
        "/ws/terminal",
    }
    assert app.state.operator_feed.sources is app.state.operator_sources
    assert app.state.operator_api.feed is app.state.operator_feed
    assert app.state.operator_sse.feed is app.state.operator_feed
    process_scope = app.state.operator_sources._process_scope
    assert process_scope is not None
    assert process_scope.timeout < runtime.shutdown_budget
    app.state.operator_sources.close()


class SlowHiveProvider:
    """A per-hive source slower than the private directory deadline until released."""

    def __init__(self, *, seconds: float = 30.0) -> None:
        self.seconds = seconds
        self.release = threading.Event()
        self.calls: dict[str, int] = {}
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def refresh(self, request):
        with self._lock:
            self.calls[request.hive] = self.calls.get(request.hive, 0) + 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.release.wait(self.seconds)
        finally:
            with self._lock:
                self.active -= 1
        return state_stream.ProviderSnapshot(
            scope="hive",
            revision=f"{request.hive}-1",
            as_of=NOW,
            issues=(
                state_stream.StreamIssue(
                    id="bh-1",
                    hive=request.hive,
                    issue_type="task",
                    status="open",
                    priority="P1",
                    title="Slow",
                    updated_at=NOW,
                ),
            ),
        )


def _fleet_cfg(count: int) -> dict:
    return {
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": f"hive-{index:02d}",
                "prefix": f"h{index:02d}",
                "kind": "org-native",
            }
            for index in range(count)
        ]
    }


def test_factory_directory_never_waits_on_slow_hive_sources(tmp_path: Path) -> None:
    provider = SlowHiveProvider()
    fleet = operator_sources.HIVE_REFRESH_CONCURRENCY + 4

    async def action(client, app):
        sources = app.state.operator_feed.sources
        try:
            started = time.monotonic()
            reads = await asyncio.gather(
                *(client.get("/api/v1/factory/hives", params={"limit": 200}) for _ in range(5))
            )
            elapsed = time.monotonic() - started
            # Let the capped pool pick up everything it can while sources stay slow.
            await asyncio.sleep(0.2)
            in_flight = dict(provider.calls)
            max_active = provider.max_active
        finally:
            provider.release.set()
        assert await asyncio.to_thread(sources.hive_summaries.wait_idle, 10)
        # Queued hives were refreshed once each after the release, never twice.
        warm = await client.get("/api/v1/factory/hives", params={"limit": 200})
        return reads, elapsed, in_flight, max_active, warm

    reads, elapsed, in_flight, max_active, warm = _exercise(
        tmp_path, action, cfg=_fleet_cfg(fleet), provider=provider
    )

    assert elapsed < 2.0
    assert all(read.status_code == 200 for read in reads)
    cold = reads[0].json()
    assert cold["returnedCount"] == fleet
    for item in cold["items"]:
        assert item["freshness"] == {"state": "refreshing", "asOf": None, "expiresAt": None}
        assert item["availability"] == {"state": "available", "reason": "summary_pending"}
        assert item["coverage"] == {"state": "partial", "reason": "summary_pending"}
        assert item["counts"] == {"open": None, "ready": None, "active": None, "blocked": None}
    # Concurrent reads dedupe to one in-flight refresh per hive under a global cap.
    assert all(count == 1 for count in in_flight.values())
    assert max_active <= operator_sources.HIVE_REFRESH_CONCURRENCY
    assert set(provider.calls) == {item["id"] for item in cold["items"]}
    assert all(count == 1 for count in provider.calls.values())

    assert warm.status_code == 200
    assert {item["freshness"]["state"] for item in warm.json()["items"]} == {"fresh"}
    assert all(item["counts"]["open"] == 1 for item in warm.json()["items"])
    schema = operator_api.openapi_document()["components"]["schemas"]
    validator = jsonschema.Draft202012Validator(
        {"components": {"schemas": schema}, "$ref": "#/components/schemas/FactoryHivePage"}
    )
    validator.validate(cold)
    validator.validate(warm.json())


def test_single_hive_reads_warm_the_directory_cache(tmp_path: Path) -> None:
    calls: list[str] = []

    class CountingProvider(Provider):
        def refresh(self, request):
            calls.append(request.hive)
            return super().refresh(request)

    async def action(client, app):
        snapshot = await client.get("/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot")
        directory = await client.get("/api/v1/factory/hives")
        return snapshot, directory

    snapshot, directory = _exercise(tmp_path, action, provider=CountingProvider())

    assert snapshot.status_code == directory.status_code == 200
    [item] = directory.json()["items"]
    assert item["freshness"]["state"] == "fresh"
    assert item["revision"] == "beads-1"
    assert item["counts"]["open"] == 1
    # The directory served the warmed summary without scheduling another source read.
    assert calls == [HIVE]


def test_hive_summary_cache_expires_and_tracks_registry_membership() -> None:
    now = [0.0]
    refreshed: list[str] = []
    cache: operator_sources.HiveSummaryCache

    def refresh(hive):
        refreshed.append(hive.identity)
        cache.record(hive, {"id": hive.identity, "revision": "r"}, started=now[0])

    cache = operator_sources.HiveSummaryCache(refresh, ttl=60.0, clock=lambda: now[0])
    entry = {"provider": "github", "org": "o", "repo": "r", "prefix": "r"}
    hive = operator_sources.ExactHive("github/o/r", entry)
    try:
        assert cache.read((hive,))[0]["freshness"]["state"] in {"refreshing", "unknown"}
        assert cache.wait_idle(5)
        assert cache.read((hive,))[0]["freshness"]["state"] == "fresh"
        now[0] = 61.0
        assert cache.read((hive,))[0]["freshness"]["state"] in {"refreshing", "fresh"}
        assert cache.wait_idle(5)
        assert refreshed == ["github/o/r", "github/o/r"]
        # A newer observation is never replaced by an older-started one.
        cache.record(hive, {"id": hive.identity, "revision": "old"}, started=0.0)
        assert cache.read((hive,))[0]["revision"] == "r"
        # Dropping the hive from the registry drops its cached summary.
        assert cache.read(()) == []
        cache.close()
        [pending] = cache.read((hive,))
        assert pending["freshness"]["state"] == "unknown"
    finally:
        cache.close()


def test_gateway_and_frame_bridge_directory_relays_return_promptly_with_slow_sources(
    tmp_path: Path,
) -> None:
    provider = SlowHiveProvider()
    cfg = _fleet_cfg(operator_sources.HIVE_REFRESH_CONCURRENCY + 2)
    cfg["managed_repos"].append(
        {
            "provider": "github",
            "org": "beadhive",
            "repo": "beadhive",
            "prefix": "bh",
            "kind": "org-native",
        }
    )
    app = _app(tmp_path, cfg=cfg, provider=provider)

    async def run():
        async with app.router.lifespan_context(app):
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
                base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
            )
            gateway = frame_bridge_runtime.LoopbackGatewayReadSource(
                daemon_bearer=daemon_auth.SecretBearer("bh1.frame-bridge." + "d" * 43),
                authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
                client=client,
            )
            bridge = frame_bridge_upstream.HostDaemonFrameBridgeSource(
                daemon_bearer=daemon_auth.SecretBearer("bh1.frame-bridge." + "d" * 43),
                instance=frame_bridge_upstream.RegisteredInstance(),
                client=client,
            )
            try:
                started = time.monotonic()
                listed = await asyncio.wait_for(
                    gateway.list_hives(frame_bridge.LOCAL_DESKTOP_SUBJECT, limit=50, after=None),
                    timeout=5.0,
                )
                directory = await asyncio.wait_for(
                    bridge.directory(limit=50, cursor=None), timeout=5.0
                )
                return time.monotonic() - started, listed, directory
            finally:
                provider.release.set()
                await asyncio.to_thread(
                    app.state.operator_feed.sources.hive_summaries.wait_idle, 10
                )
                await client.aclose()

    elapsed, listed, directory = asyncio.run(run())

    assert elapsed < 2.0
    assert len(listed["items"]) == len(cfg["managed_repos"])
    assert all(
        item["availability"] == "online" and item["freshness"]["state"] == "unknown"
        for item in listed["items"]
    )
    assert [item["hiveId"] for item in directory["items"]] == [HIVE]
    assert directory["items"][0]["availability"] == "available"
