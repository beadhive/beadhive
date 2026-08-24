"""ASGI and checked-contract coverage for the local operator read profile."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from starlette.middleware import Middleware

from beadhive import (
    host_daemon,
    operator_api,
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


def _app(tmp_path: Path):
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

    sources = operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-1",
        provider=Provider(),
        summary_reader=lambda _path, host, source: runtime(host, source),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )
    feed = operator_feed.OperatorFeed(sources, now_millis=lambda: 1000)
    daemon_runtime = host_daemon.DaemonRuntime()
    relay = operator_sse.OperatorEventRelay(feed, daemon_runtime)
    api = operator_api.OperatorAPI(
        sources=sources,
        feed=feed,
        host_id="host-1",
        instance_id="instance-1",
        ready=lambda: daemon_runtime.ready,
        events=relay.events,
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
    return app


def _exercise(tmp_path: Path, action):
    app = _app(tmp_path)

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
    assert factory.json()["workspaceRoot"] is None
    assert factory.json()["worktrees"] == []
    assert factory.json()["hostId"] == "host-1"
    assert snapshot.json()["hive"]["prefix"] == HIVE
    assert snapshot.json()["cursor"]["subscriptionId"] == f"hive:{HIVE}"
    assert health.json() == {
        "live": True,
        "ready": True,
        "contract": host_daemon.CONTRACT_VERSION,
    }
    assert snapshot.headers["cache-control"] == "no-store"


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
    assert [response.status_code for response in absent] == [404, 404, 404]
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
    assert response.status_code == 200
    assert response.json() == checked
    assert checked["openapi"] == "3.1.0"
    assert checked["security"] == []
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
        control_record=record,
        listener_host="127.0.0.1",
        listener_port=8420,
        cfg={"managed_repos": []},
    )
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert paths == {
        "/health",
        "/api/v1/factory",
        "/api/v1/hives/{hive_id:path}/snapshot",
        "/api/v1/hives/{hive_id:path}/events",
        "/api/v1/runs/{run_id}/activity",
        "/openapi.json",
    }
    assert app.state.operator_feed.sources is app.state.operator_sources
    assert app.state.operator_api.feed is app.state.operator_feed
    assert app.state.operator_sse.feed is app.state.operator_feed
    process_scope = app.state.operator_sources._process_scope
    assert process_scope is not None
    assert process_scope.timeout < runtime.shutdown_budget
    app.state.operator_sources.close()
