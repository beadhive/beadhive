"""Deployable Development Frame Bridge runtime conformance."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from beadhive import (
    daemon_auth,
    frame_bridge,
    frame_bridge_runtime,
    gateway_read,
    operator_contract,
)

EPOCH = "123e4567e89b42d3a456426614174000"
REVISION = "sha256:" + "a" * 64
DAEMON_BEARER = "bh1.frame-bridge." + "d" * 43
HIVE = "github/beadhive/beadhive-app"
HIVE_SUBSCRIPTION = operator_contract.hive_subscription_id(HIVE)


def _live_snapshot() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "hive": {
            "prefix": "github/beadhive/beadhive-app",
            "provider": "github",
            "org": "beadhive",
            "repo": "beadhive-app",
            "kind": "org-native",
        },
        "revision": REVISION,
        "generatedAt": 1_787_811_221_000,
        "cursor": {
            "subscriptionId": HIVE_SUBSCRIPTION,
            "producerEpoch": EPOCH,
            "sequence": 7,
            "observedAt": 1_787_811_221_001,
        },
        "coverage": {
            "state": "partial",
            "generatedAt": 1_787_811_221_000,
            "sources": {
                "runtime": {
                    "state": "unavailable",
                    "requested": None,
                    "returned": 0,
                    "fromCache": 0,
                    "detail": "source_missing",
                    "generatedAt": 1_787_811_221_000,
                    "provenance": {
                        "system": "beadhive.dispatch-summary",
                        "instance": None,
                        "runId": None,
                        "documentRef": None,
                    },
                }
            },
        },
        "workItems": [{"sentinel": "work-item"}],
        "dependencies": [{"sentinel": "dependency"}],
        "epics": [{"sentinel": "epic"}],
        "gates": [{"sentinel": "gate"}],
        "agents": [],
        "assignments": [{"sentinel": "assignment"}],
        "schedules": [{"sentinel": "schedule"}],
        "evidence": [],
        "advertisedActions": [],
    }


def _operator_app(seen_authorizations: list[str | None] | None = None) -> Starlette:
    async def health(_request):
        return JSONResponse({"status": "live", "ready": True})

    async def snapshot(request):
        authorization = request.headers.get("authorization")
        if seen_authorizations is not None:
            seen_authorizations.append(authorization)
        if authorization != f"Bearer {DAEMON_BEARER}":
            return JSONResponse({"error": {"code": "auth_missing"}}, status_code=401)
        return JSONResponse(
            {
                "schemaVersion": 1,
                "revision": REVISION,
                "generatedAt": 1_787_811_221_000,
                "cursor": {"producerEpoch": EPOCH, "sequence": 1},
                "workItems": [],
                "agents": [],
                "workspaceRoot": "/private/must-not-cross",
            }
        )

    async def events(request):
        authorization = request.headers.get("authorization")
        if seen_authorizations is not None:
            seen_authorizations.append(authorization)
        if authorization != f"Bearer {DAEMON_BEARER}":
            return JSONResponse({"error": {"code": "auth_missing"}}, status_code=401)
        if request.query_params["cursor"].endswith(":0"):
            return JSONResponse({"action": "resnapshot"}, status_code=409)

        async def stream():
            payload = json.dumps({"revision": "beads-local-2", "private": "hidden"})
            yield f"event: operator-event\nid: {EPOCH}:2\ndata: {payload}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/api/v1/hives/github/beadhive/beadhive/snapshot", snapshot),
            Route("/api/v1/hives/github/beadhive/beadhive/events", events),
        ]
    )


def _runtime() -> frame_bridge_runtime.LoopbackDemoRuntime:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_operator_app()),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    return frame_bridge_runtime.LoopbackDemoRuntime(
        daemon_bearer=daemon_auth.SecretBearer(DAEMON_BEARER),
        client=client,
    )


def test_live_gateway_read_source_maps_the_daemon_hive_directory() -> None:
    seen_authorizations: list[str | None] = []

    async def hives(request):
        seen_authorizations.append(request.headers.get("authorization"))
        return JSONResponse(
            {
                "schemaVersion": 1,
                "revision": REVISION,
                "generatedAt": 1_787_811_221_000,
                "items": [
                    {
                        "id": "github/beadhive/beadhive-app",
                        "displayLabel": "beadhive-app",
                        "availability": {"state": "available", "reason": None},
                        "revision": REVISION,
                        "asOf": 1_787_811_221_000,
                        "coverage": {"state": "partial", "reason": "runtime_source_missing"},
                    }
                ],
                "returnedCount": 1,
                "limit": 50,
                "truncated": False,
                "nextCursor": None,
                "warnings": [],
            }
        )

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=Starlette(routes=[Route("/api/v1/factory/hives", hives)])
        ),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    source = frame_bridge_runtime.LoopbackGatewayReadSource(
        daemon_bearer=daemon_auth.SecretBearer(DAEMON_BEARER),
        authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
        client=client,
    )

    async def exercise():
        try:
            return await source.list_hives(
                frame_bridge.LOCAL_DESKTOP_SUBJECT,
                limit=50,
                after=None,
            )
        finally:
            await client.aclose()

    page = asyncio.run(exercise())

    assert page == {
        "schemaVersion": 1,
        "contractVersion": "gateway.read.v1",
        "instanceId": "dev/demo",
        "factoryId": "development",
        "detailLevel": "summary",
        "items": [
            {
                "factoryId": "development",
                "hiveId": "github/beadhive/beadhive-app",
                "displayName": "beadhive-app",
                "sourceMode": "live",
                "scenarioId": None,
                "availability": "online",
                "freshness": {
                    "state": "fresh",
                    "asOf": 1_787_811_221_000,
                    "expiresAt": None,
                    "detail": "runtime_source_missing",
                },
                "capabilities": ["snapshot", "events"],
            }
        ],
        "nextCursor": None,
    }
    assert seen_authorizations == [f"Bearer {DAEMON_BEARER}"]


def test_live_gateway_read_source_rejects_a_torn_daemon_directory_page() -> None:
    async def hives(_request):
        return JSONResponse(
            {
                "schemaVersion": 1,
                "revision": REVISION,
                "generatedAt": 1_787_811_221_000,
                "items": [],
                "returnedCount": 1,
                "limit": 50,
                "truncated": False,
                "nextCursor": None,
                "warnings": [],
            }
        )

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=Starlette(routes=[Route("/api/v1/factory/hives", hives)])
        ),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    source = frame_bridge_runtime.LoopbackGatewayReadSource(
        daemon_bearer=daemon_auth.SecretBearer(DAEMON_BEARER),
        authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
        client=client,
    )

    async def exercise() -> None:
        try:
            with pytest.raises(RuntimeError, match="directory is incompatible"):
                await source.list_hives(
                    frame_bridge.LOCAL_DESKTOP_SUBJECT,
                    limit=50,
                    after=None,
                )
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_live_gateway_read_source_wraps_the_exact_daemon_snapshot() -> None:
    async def snapshot(request):
        assert request.path_params["hive_id"] == "github/beadhive/beadhive-app"
        assert request.headers["authorization"] == f"Bearer {DAEMON_BEARER}"
        return JSONResponse(_live_snapshot())

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=Starlette(routes=[Route("/api/v1/hives/{hive_id:path}/snapshot", snapshot)])
        ),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    source = frame_bridge_runtime.LoopbackGatewayReadSource(
        daemon_bearer=daemon_auth.SecretBearer(DAEMON_BEARER),
        authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
        client=client,
    )

    async def exercise():
        try:
            return await source.snapshot(
                frame_bridge.LOCAL_DESKTOP_SUBJECT,
                factory_id="development",
                hive_id="github/beadhive/beadhive-app",
                detail="live",
            )
        finally:
            await client.aclose()

    envelope = asyncio.run(exercise())

    assert envelope == {
        "schemaVersion": 1,
        "contractVersion": "gateway.read.v1",
        "instanceId": "dev/demo",
        "factoryId": "development",
        "hiveId": "github/beadhive/beadhive-app",
        "detailLevel": "live",
        "source": {
            "mode": "live",
            "revision": REVISION,
            "generatedAt": 1_787_811_221_000,
            "artifactVersion": None,
            "provenance": {
                "system": "beadhive.host-daemon",
                "version": "1",
                "scenario": None,
            },
        },
        "snapshot": _live_snapshot(),
    }


def test_live_gateway_read_source_rejects_snapshot_fields_outside_the_public_contract() -> None:
    async def snapshot(_request):
        payload = _live_snapshot()
        payload["workspaceRoot"] = "/private/must-not-cross"
        return JSONResponse(payload)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=Starlette(routes=[Route("/api/v1/hives/{hive_id:path}/snapshot", snapshot)])
        ),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    source = frame_bridge_runtime.LoopbackGatewayReadSource(
        daemon_bearer=daemon_auth.SecretBearer(DAEMON_BEARER),
        authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
        client=client,
    )

    async def exercise() -> None:
        try:
            with pytest.raises(RuntimeError, match="snapshot is incompatible"):
                await source.snapshot(
                    frame_bridge.LOCAL_DESKTOP_SUBJECT,
                    factory_id="development",
                    hive_id="github/beadhive/beadhive-app",
                    detail="live",
                )
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_live_gateway_read_source_streams_contiguous_daemon_events() -> None:
    seen_query: dict[str, str] = {}

    async def snapshot(_request):
        return JSONResponse(_live_snapshot())

    async def events(request):
        seen_query.update(request.query_params)
        assert request.headers["authorization"] == f"Bearer {DAEMON_BEARER}"
        event = {
            "schemaVersion": 1,
            "hiveId": "github/beadhive/beadhive-app",
            "subscriptionId": HIVE_SUBSCRIPTION,
            "producerEpoch": EPOCH,
            "sequence": 8,
            "baseSequence": 7,
            "observedAt": 1_787_811_221_002,
            "generatedAt": 1_787_811_221_002,
            "source": "beads",
            "revision": REVISION,
            "entity": None,
            "payload": {"kind": "heartbeat"},
        }

        async def stream():
            yield (
                f"id: {EPOCH}:8\nevent: operator-event\n"
                f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            )

        return StreamingResponse(stream(), media_type="text/event-stream")

    app = Starlette(
        routes=[
            Route("/api/v1/hives/{hive_id:path}/snapshot", snapshot),
            Route("/api/v1/hives/{hive_id:path}/events", events),
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    source = frame_bridge_runtime.LoopbackGatewayReadSource(
        daemon_bearer=daemon_auth.SecretBearer(DAEMON_BEARER),
        authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
        client=client,
    )

    async def exercise():
        try:
            await source.snapshot(
                frame_bridge.LOCAL_DESKTOP_SUBJECT,
                factory_id="development",
                hive_id="github/beadhive/beadhive-app",
                detail="live",
            )
            stream = await source.events(
                frame_bridge.LOCAL_DESKTOP_SUBJECT,
                factory_id="development",
                hive_id="github/beadhive/beadhive-app",
                subscription=HIVE_SUBSCRIPTION,
                after=f"{EPOCH}:7",
            )
            return [event async for event in stream]
        finally:
            await client.aclose()

    envelopes = asyncio.run(exercise())

    assert seen_query == {
        "cursor": f"{EPOCH}:7",
        "subscription": HIVE_SUBSCRIPTION,
    }
    assert envelopes == [
        {
            "schemaVersion": 1,
            "contractVersion": "gateway.read.v1",
            "instanceId": "dev/demo",
            "factoryId": "development",
            "hiveId": "github/beadhive/beadhive-app",
            "detailLevel": "live",
            "event": {
                "schemaVersion": 1,
                "hiveId": "github/beadhive/beadhive-app",
                "subscriptionId": HIVE_SUBSCRIPTION,
                "producerEpoch": EPOCH,
                "sequence": 8,
                "baseSequence": 7,
                "observedAt": 1_787_811_221_002,
                "generatedAt": 1_787_811_221_002,
                "source": "beads",
                "revision": REVISION,
                "entity": None,
                "payload": {"kind": "heartbeat"},
            },
        }
    ]


def test_live_gateway_read_source_fences_a_stream_when_a_new_snapshot_is_installed() -> None:
    snapshots = 0

    async def snapshot(_request):
        nonlocal snapshots
        snapshots += 1
        payload = _live_snapshot()
        if snapshots == 2:
            payload["revision"] = "sha256:" + "b" * 64
            payload["cursor"] = {
                "subscriptionId": HIVE_SUBSCRIPTION,
                "producerEpoch": "223e4567e89b42d3a456426614174000",
                "sequence": 0,
                "observedAt": 1_787_811_221_003,
            }
        return JSONResponse(payload)

    async def events(_request):
        event = {
            "schemaVersion": 1,
            "hiveId": "github/beadhive/beadhive-app",
            "subscriptionId": HIVE_SUBSCRIPTION,
            "producerEpoch": EPOCH,
            "sequence": 8,
            "baseSequence": 7,
            "observedAt": 1_787_811_221_002,
            "generatedAt": 1_787_811_221_002,
            "source": "beads",
            "revision": REVISION,
            "entity": None,
            "payload": {"kind": "heartbeat"},
        }

        async def stream():
            yield (
                f"id: {EPOCH}:8\nevent: operator-event\n"
                f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            )

        return StreamingResponse(stream(), media_type="text/event-stream")

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=Starlette(
                routes=[
                    Route("/api/v1/hives/{hive_id:path}/snapshot", snapshot),
                    Route("/api/v1/hives/{hive_id:path}/events", events),
                ]
            )
        ),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    source = frame_bridge_runtime.LoopbackGatewayReadSource(
        daemon_bearer=daemon_auth.SecretBearer(DAEMON_BEARER),
        authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
        client=client,
    )

    async def exercise() -> None:
        try:
            await source.snapshot(
                frame_bridge.LOCAL_DESKTOP_SUBJECT,
                factory_id="development",
                hive_id="github/beadhive/beadhive-app",
                detail="live",
            )
            stream = await source.events(
                frame_bridge.LOCAL_DESKTOP_SUBJECT,
                factory_id="development",
                hive_id="github/beadhive/beadhive-app",
                subscription=HIVE_SUBSCRIPTION,
                after=f"{EPOCH}:7",
            )
            await source.snapshot(
                frame_bridge.LOCAL_DESKTOP_SUBJECT,
                factory_id="development",
                hive_id="github/beadhive/beadhive-app",
                detail="live",
            )
            with pytest.raises(gateway_read.ReadSourceResnapshotRequired):
                _ = [event async for event in stream]
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_real_loopback_profile_maps_snapshot_refresh_and_retained_events() -> None:
    async def exercise():
        runtime = _runtime()
        try:
            assert await runtime.online()
            snapshot = await runtime.snapshot()
            receipt = await runtime.refresh(REVISION, "ignored-correlation")
            source = await runtime.events("123e4567-e89b-42d3-a456-426614174000:1")
            events = [event async for event in source]
            return snapshot, receipt, events
        finally:
            await runtime.close()

    snapshot, receipt, events = asyncio.run(exercise())
    assert snapshot == {
        "schemaVersion": 1,
        "revision": REVISION,
        "generatedAt": 1_787_811_221_000,
        "workItems": [],
        "agents": [],
        "eventCursor": "123e4567-e89b-42d3-a456-426614174000:1",
    }
    assert receipt == {"status": "completed", "revision": REVISION}
    assert events == [
        {
            "cursor": "123e4567-e89b-42d3-a456-426614174000:2",
            "revision": REVISION,
        }
    ]
    assert "private" not in str(events)


def test_loopback_profile_sends_only_its_private_daemon_bearer() -> None:
    seen: list[str | None] = []
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_operator_app(seen)),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    runtime = frame_bridge_runtime.LoopbackDemoRuntime(
        daemon_bearer=daemon_auth.SecretBearer(DAEMON_BEARER),
        client=client,
    )

    async def exercise() -> None:
        try:
            assert await runtime.online()
            await runtime.snapshot()
            source = await runtime.events("123e4567-e89b-42d3-a456-426614174000:1")
            assert [event async for event in source]
        finally:
            await runtime.close()

    asyncio.run(exercise())

    assert seen == [f"Bearer {DAEMON_BEARER}"] * 3
    assert all("caller" not in value for value in seen if value is not None)


def test_development_projection_selects_only_current_non_operational_work() -> None:
    items = [
        {"record": {"id": "active", "status": "open", "issueType": "task"}},
        {"record": {"id": "running", "status": "in_progress", "issueType": "feature"}},
        {"record": {"id": "blocked", "status": "blocked", "issueType": "bug"}},
        {"record": {"id": "closed", "status": "closed", "issueType": "task"}},
        {"record": {"id": "deferred", "status": "deferred", "issueType": "task"}},
        {"record": {"id": "event", "status": "open", "issueType": "event"}},
        {"record": {"id": "gate", "status": "open", "issueType": "gate"}},
    ]

    selected = frame_bridge_runtime._development_work_items(items)

    assert [item["record"]["id"] for item in selected] == ["active", "running", "blocked"]


def test_development_projection_rejects_malformed_work_items() -> None:
    with pytest.raises(RuntimeError, match="work item is incompatible"):
        frame_bridge_runtime._development_work_items([{"record": {"status": "open"}}])


def test_loopback_profile_rejects_stale_refresh_and_event_cursor() -> None:
    async def exercise():
        runtime = _runtime()
        try:
            with pytest.raises(frame_bridge.StaleCommandScope):
                await runtime.refresh("sha256:" + "f" * 64, "ignored-correlation")
            with pytest.raises(frame_bridge.StaleEventCursor):
                await runtime.events("123e4567-e89b-42d3-a456-426614174000:0")
        finally:
            await runtime.close()

    asyncio.run(exercise())


def test_subject_policy_file_is_private_bounded_and_exact(tmp_path: Path) -> None:
    policy = tmp_path / "subjects.json"
    policy.write_text('["user_development"]', encoding="utf-8")
    policy.chmod(0o600)
    assert frame_bridge_runtime._authorized_subjects(policy) == {"user_development"}

    policy.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        frame_bridge_runtime._authorized_subjects(policy)


def test_local_desktop_factory_does_not_load_clerk_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon_bearer = tmp_path / "daemon-bearer"
    daemon_bearer.write_text(DAEMON_BEARER, encoding="ascii")
    daemon_bearer.chmod(0o600)
    monkeypatch.setenv(frame_bridge_runtime.NETWORK_PROFILE_ENV, "local-desktop")
    monkeypatch.setenv(frame_bridge_runtime.SOURCE_MODE_ENV, "generated")
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_DAEMON_CREDENTIAL_FILE", str(daemon_bearer))
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_JWKS_FILE", str(tmp_path / "absent-jwks"))
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_SUBJECTS_FILE", str(tmp_path / "absent-subjects"))

    def forbidden_subject_policy(_path: Path) -> frozenset[str]:
        raise AssertionError("local-desktop must not read Clerk subject policy")

    def forbidden_jwks(_value):
        raise AssertionError("local-desktop must not import Clerk JWKS")

    monkeypatch.setattr(frame_bridge_runtime, "_authorized_subjects", forbidden_subject_policy)
    monkeypatch.setattr(frame_bridge_runtime.KeySet, "import_key_set", forbidden_jwks)
    monkeypatch.setattr(
        frame_bridge_runtime.bh_config,
        "load",
        lambda: (_ for _ in ()).throw(RuntimeError("telemetry unavailable")),
    )

    app = frame_bridge_runtime.create_application()

    assert app.state.source_mode == "generated"

    async def exercise() -> httpx.Response:
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=frame_bridge.LOCAL_DESKTOP_GATEWAY_ORIGIN,
            ) as client:
                return await client.get(
                    "/v1/instances/dev/demo/experience",
                    headers={"Origin": frame_bridge.LOCAL_DESKTOP_APP_ORIGIN},
                )
        finally:
            await lifespan.__aexit__(None, None, None)

    response = asyncio.run(exercise())
    assert response.status_code == 200


def test_local_desktop_live_factory_wires_the_loopback_gateway_read_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon_bearer = tmp_path / "daemon-bearer"
    daemon_bearer.write_text(DAEMON_BEARER, encoding="ascii")
    daemon_bearer.chmod(0o600)
    monkeypatch.setenv(frame_bridge_runtime.NETWORK_PROFILE_ENV, "local-desktop")
    monkeypatch.setenv(frame_bridge_runtime.SOURCE_MODE_ENV, "live")
    monkeypatch.setenv("BEADHIVE_FRAME_BRIDGE_DAEMON_CREDENTIAL_FILE", str(daemon_bearer))
    monkeypatch.setattr(
        frame_bridge_runtime.bh_config,
        "load",
        lambda: (_ for _ in ()).throw(RuntimeError("telemetry unavailable")),
    )

    class Runtime:
        client = object()

        async def online(self):
            return True

        async def snapshot(self):
            return {
                "schemaVersion": 1,
                "revision": REVISION,
                "generatedAt": 1_787_811_221_000,
                "workItems": [],
                "agents": [],
                "eventCursor": "123e4567-e89b-42d3-a456-426614174000:1",
            }

        async def refresh(self, _revision, _correlation_id):
            return {"status": "completed", "revision": REVISION}

        async def events(self, _cursor):
            async def stream():
                if False:
                    yield {}

            return stream()

        async def close(self):
            return None

    class Source:
        cache_boundary = "live-test"

        async def list_hives(self, subject, *, limit, after):
            assert subject == frame_bridge.LOCAL_DESKTOP_SUBJECT
            assert limit == 50
            assert after is None
            return {
                "schemaVersion": 1,
                "contractVersion": "gateway.read.v1",
                "instanceId": "dev/demo",
                "factoryId": "development",
                "detailLevel": "summary",
                "items": [],
                "nextCursor": None,
            }

    constructed: list[tuple[object, frozenset[str]]] = []
    monkeypatch.setattr(frame_bridge_runtime, "LoopbackDemoRuntime", lambda **_kwargs: Runtime())
    monkeypatch.setattr(
        frame_bridge_runtime,
        "LoopbackGatewayReadSource",
        lambda *, daemon_bearer, authorized_subjects, **_kwargs: (
            constructed.append((daemon_bearer, authorized_subjects)) or Source()
        ),
    )

    app = frame_bridge_runtime.create_application()

    async def exercise() -> httpx.Response:
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=frame_bridge.LOCAL_DESKTOP_GATEWAY_ORIGIN,
            ) as client:
                return await client.get(
                    "/v1/instances/dev/demo/hives?limit=50",
                    headers={"Origin": frame_bridge.LOCAL_DESKTOP_APP_ORIGIN},
                )
        finally:
            await lifespan.__aexit__(None, None, None)

    response = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert len(constructed) == 1
    assert constructed[0][1] == {frame_bridge.LOCAL_DESKTOP_SUBJECT}


def test_public_health_is_exact_host_only_and_origin_free() -> None:
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=frame_bridge.DEVELOPMENT_ISSUER,
        audience=frame_bridge_runtime.AUDIENCE,
        app_origin=frame_bridge_runtime.APP_ORIGIN,
        gateway_origin=frame_bridge_runtime.GATEWAY_ORIGIN,
    )
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=object()),
        registry=frame_bridge.DevelopmentInstanceRegistry(instances={}),
    )

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=frame_bridge_runtime.GATEWAY_ORIGIN
        ) as client:
            healthy = await client.get("/healthz")
            browser = await client.get(
                "/healthz", headers={"Origin": "https://app-dev.beadhive.cloud"}
            )
            return healthy, browser

    healthy, browser = asyncio.run(exercise())
    assert healthy.json() == {"live": True, "contractVersion": "gateway.v1"}
    assert browser.status_code == 403
