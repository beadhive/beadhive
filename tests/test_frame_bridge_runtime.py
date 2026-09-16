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

from beadhive import daemon_auth, frame_bridge, frame_bridge_runtime

EPOCH = "123e4567e89b42d3a456426614174000"
REVISION = "sha256:" + "a" * 64
DAEMON_BEARER = "bh1.frame-bridge." + "d" * 43


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
