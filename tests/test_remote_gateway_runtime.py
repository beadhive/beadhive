"""Deployable Development gateway runtime profile conformance."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from beadhive import remote_gateway, remote_gateway_runtime

EPOCH = "123e4567e89b42d3a456426614174000"
REVISION = "sha256:" + "a" * 64


def _operator_app() -> Starlette:
    async def health(_request):
        return JSONResponse({"live": True, "ready": True})

    async def snapshot(_request):
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


def _runtime() -> remote_gateway_runtime.LoopbackDemoRuntime:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_operator_app()),
        base_url=remote_gateway_runtime.LOOPBACK_ORIGIN,
    )
    return remote_gateway_runtime.LoopbackDemoRuntime(client)


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


def test_loopback_profile_rejects_stale_refresh_and_event_cursor() -> None:
    async def exercise():
        runtime = _runtime()
        try:
            with pytest.raises(remote_gateway.StaleCommandScope):
                await runtime.refresh("sha256:" + "f" * 64, "ignored-correlation")
            with pytest.raises(remote_gateway.StaleEventCursor):
                await runtime.events("123e4567-e89b-42d3-a456-426614174000:0")
        finally:
            await runtime.close()

    asyncio.run(exercise())


def test_subject_policy_file_is_private_bounded_and_exact(tmp_path: Path) -> None:
    policy = tmp_path / "subjects.json"
    policy.write_text('["user_development"]', encoding="utf-8")
    policy.chmod(0o600)
    assert remote_gateway_runtime._authorized_subjects(policy) == {"user_development"}

    policy.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        remote_gateway_runtime._authorized_subjects(policy)


def test_public_health_is_exact_host_only_and_origin_free() -> None:
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=remote_gateway.DEVELOPMENT_ISSUER,
        audience=remote_gateway_runtime.AUDIENCE,
        app_origin=remote_gateway_runtime.APP_ORIGIN,
        gateway_origin=remote_gateway_runtime.GATEWAY_ORIGIN,
    )
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=object()),
        registry=remote_gateway.DevelopmentInstanceRegistry(instances={}),
    )

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=remote_gateway_runtime.GATEWAY_ORIGIN
        ) as client:
            healthy = await client.get("/healthz")
            browser = await client.get(
                "/healthz", headers={"Origin": "https://app-dev.beadhive.cloud"}
            )
            return healthy, browser

    healthy, browser = asyncio.run(exercise())
    assert healthy.json() == {"live": True, "contractVersion": "gateway.v1"}
    assert browser.status_code == 403
