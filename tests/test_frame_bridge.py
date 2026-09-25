"""Conformance coverage for the authenticated Development Frame Bridge profile."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt
from joserfc.jwk import RSAKey
from joserfc.jws import JWSRegistry

from beadhive import daemon_auth, frame_bridge, frame_bridge_runtime
from beadhive.kernel.telemetry import (
    EventIdentity,
    Outcome,
    RecordingTelemetrySink,
    SemanticTelemetry,
)

ISSUER = "https://rapid-snail-6758.clerk.accounts.dev"
AUDIENCE = "beadhive-gateway-dev"
APP_ORIGIN = "https://app-dev.beadhive.cloud"
GATEWAY_ORIGIN = "https://gateway-dev.beadhive.cloud"
LOCAL_APP_ORIGIN = "tauri://localhost"
LOCAL_GATEWAY_ORIGIN = "http://127.0.0.1:8787"
INSTANCE_ID = "dev/demo"
SUBJECT = "user_dev_demo"
CORRELATION_ID = "123e4567-e89b-42d3-a456-426614174000"
EVENT_EPOCH = "123e4567-e89b-42d3-a456-426614174000"


def _retrieval(revision: str) -> dict[str, object]:
    return {
        "contract": "beadhive.work-items/v1",
        "revision": revision,
        "views": ["ready", "active", "blocked", "recent"],
        "maxPageItems": 200,
        "maxPageBytes": 917_504,
        "maxDetailBytes": 917_504,
    }


def _keys() -> tuple[RSAKey, RSAKey]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return RSAKey.import_key(private), RSAKey.import_key(private.public_key())


def _token(
    private_key: RSAKey,
    *,
    header_overrides: dict[str, object] | None = None,
    **overrides: object,
) -> str:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    header: dict[str, object] = {"alg": "RS256", "kid": "development-test"}
    header.update(header_overrides or {})
    return jwt.encode(
        header,
        claims,
        private_key,
        registry=JWSRegistry(algorithms=["RS256"], strict_check_header=False),
    )


def _snapshot() -> dict[str, object]:
    limits = {"maxBytes": 917504, "maxWorkItems": 4096}
    return {
        "schemaVersion": 1,
        "revision": "sha256:" + "a" * 64,
        "generatedAt": 1724716800000,
        "projectionPolicy": "beadhive.snapshot-summary/v1",
        "limits": limits,
        "coverage": {
            "state": "complete",
            "generatedAt": 1724716800000,
            "eligible": 1,
            "returned": 1,
            "reason": None,
            "policy": "beadhive.snapshot-summary/v1",
            "sourceRevision": "sha256:" + "a" * 64,
            "limits": limits,
            "workItemRetrieval": _retrieval("sha256:" + "a" * 64),
            "sources": {"private": "must-not-cross"},
        },
        "workItems": [
            {
                "id": "bh-1",
                "title": "Development demo",
                "status": "open",
                "readiness": "ready",
                "issueType": "task",
                "priority": 1,
                "labels": ["component:gateway"],
                "remainingLabelCount": 0,
                "assignee": "dev/codex",
                "owner": None,
                "updatedAt": 1724716800000,
                "blockerCount": 0,
                "openGateCount": 0,
                "liveAgentCount": 0,
                "description": "must not cross the remote boundary",
            }
        ],
        "workspaceRoot": "/Users/private/repository",
        "secret": "must-not-leak",
    }


def _public_snapshot_envelope() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "contractVersion": "gateway.v1",
        "instanceId": INSTANCE_ID,
        "snapshot": frame_bridge._public_snapshot(_snapshot(), with_events=False),
    }


async def _read_snapshot() -> dict[str, object]:
    return _snapshot()


async def _online() -> bool:
    return True


def _snapshot_reader(value: dict[str, object]):
    async def read() -> dict[str, object]:
        return value

    return read


def _refresh_reader(value: dict[str, object]):
    async def refresh(expected_revision: str, correlation_id: str) -> dict[str, object]:
        assert expected_revision == "sha256:" + "a" * 64
        assert correlation_id == CORRELATION_ID
        return value

    return refresh


def _app(
    public_key: RSAKey,
    *,
    revoked: frozenset[str] = frozenset(),
    telemetry=None,
):
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    verifier = frame_bridge.ClerkTokenVerifier(
        config=config,
        key=public_key,
        revoked_subjects=revoked,
    )
    registry = frame_bridge.DevelopmentInstanceRegistry(
        instances={
            INSTANCE_ID: frame_bridge.RemoteInstance(
                display_name="Development demo",
                authorized_subjects=frozenset({SUBJECT}),
                snapshot=_read_snapshot,
                online=_online,
            )
        }
    )
    return frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=verifier,
        registry=registry,
        telemetry=telemetry,
    )


def _exercise(app, action):
    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url=GATEWAY_ORIGIN) as client:
            return await action(client)

    return asyncio.run(run())


def _headers(token: str, *, origin: str = APP_ORIGIN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Origin": origin}


def test_local_desktop_network_profile_is_exact_and_browser_origins_stay_refused() -> None:
    local = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=LOCAL_APP_ORIGIN,
        gateway_origin=LOCAL_GATEWAY_ORIGIN,
    )
    assert local.app_origin == LOCAL_APP_ORIGIN
    assert local.gateway_origin == LOCAL_GATEWAY_ORIGIN

    for app_origin, gateway_origin in (
        (APP_ORIGIN, LOCAL_GATEWAY_ORIGIN),
        (LOCAL_APP_ORIGIN, GATEWAY_ORIGIN),
        ("http://127.0.0.1", LOCAL_GATEWAY_ORIGIN),
        ("http://localhost", LOCAL_GATEWAY_ORIGIN),
    ):
        with pytest.raises(ValueError, match="approved network profile"):
            frame_bridge.DevelopmentFrameBridgeConfig(
                issuer=ISSUER,
                audience=AUDIENCE,
                app_origin=app_origin,
                gateway_origin=gateway_origin,
            )


def test_runtime_network_profile_defaults_cloud_and_requires_exact_local_opt_in() -> None:
    assert frame_bridge_runtime.network_origins(None) == (APP_ORIGIN, GATEWAY_ORIGIN)
    assert frame_bridge_runtime.network_origins("cloud") == (APP_ORIGIN, GATEWAY_ORIGIN)
    assert frame_bridge_runtime.network_origins("local-desktop") == (
        LOCAL_APP_ORIGIN,
        LOCAL_GATEWAY_ORIGIN,
    )
    with pytest.raises(RuntimeError, match="BEADHIVE_FRAME_BRIDGE_NETWORK_PROFILE"):
        frame_bridge_runtime.network_origins("local")


def test_local_desktop_profile_admits_tauri_without_clerk_and_rejects_browser_origin() -> None:
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=LOCAL_APP_ORIGIN,
        gateway_origin=LOCAL_GATEWAY_ORIGIN,
    )
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=None,
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
                    snapshot=_read_snapshot,
                    online=_online,
                )
            }
        ),
    )

    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url=LOCAL_GATEWAY_ORIGIN) as client:
            admitted = await client.get(
                "/v1/instances",
                params={"limit": "50"},
                headers={"Origin": LOCAL_APP_ORIGIN},
            )
            snapshot = await client.get(
                "/v1/instances/dev/demo/snapshot",
                headers={"Origin": LOCAL_APP_ORIGIN},
            )
            rejected = await client.get(
                "/v1/instances",
                params={"limit": "50"},
                headers={"Origin": "http://127.0.0.1:3000"},
            )
            wrong_host = await client.get(
                "/v1/instances",
                params={"limit": "50"},
                headers={"Host": "localhost:8787", "Origin": LOCAL_APP_ORIGIN},
            )
            preflight = await client.options(
                "/v1/instances",
                headers={
                    "Origin": LOCAL_APP_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                },
            )
            return admitted, snapshot, rejected, wrong_host, preflight

    admitted, snapshot, rejected, wrong_host, preflight = asyncio.run(run())
    assert admitted.status_code == snapshot.status_code == 200
    assert admitted.headers["access-control-allow-origin"] == LOCAL_APP_ORIGIN
    assert rejected.status_code == 403
    assert wrong_host.status_code == 403
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == LOCAL_APP_ORIGIN
    assert "access-control-allow-headers" not in preflight.headers


def test_local_desktop_profile_rejects_authorization_and_cloud_still_requires_it() -> None:
    _, public_key = _keys()
    local_config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=LOCAL_APP_ORIGIN,
        gateway_origin=LOCAL_GATEWAY_ORIGIN,
    )
    local_app = frame_bridge.build_development_frame_bridge_application(
        config=local_config,
        verifier=None,
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
                    snapshot=_read_snapshot,
                    online=_online,
                )
            }
        ),
    )

    async def local_action():
        transport = httpx.ASGITransport(app=local_app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url=LOCAL_GATEWAY_ORIGIN) as client:
            request = await client.get(
                "/v1/instances",
                params={"limit": "50"},
                headers={"Origin": LOCAL_APP_ORIGIN, "Authorization": "Bearer not-local"},
            )
            preflight = await client.options(
                "/v1/instances",
                headers={
                    "Origin": LOCAL_APP_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )
            return request, preflight

    local_request, local_preflight = asyncio.run(local_action())
    assert (local_request.status_code, local_request.json()["error"]["code"]) == (
        401,
        "authentication_failed",
    )
    assert (local_preflight.status_code, local_preflight.json()["error"]["code"]) == (
        403,
        "request_denied",
    )

    cloud_app = _app(public_key)

    async def cloud_action(client):
        return await client.get(
            "/v1/instances", params={"limit": "50"}, headers={"Origin": APP_ORIGIN}
        )

    cloud_request = _exercise(cloud_app, cloud_action)
    assert (cloud_request.status_code, cloud_request.json()["error"]["code"]) == (
        401,
        "authentication_failed",
    )


def test_local_desktop_refresh_uses_content_type_only_preflight_and_no_bearer() -> None:
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=LOCAL_APP_ORIGIN,
        gateway_origin=LOCAL_GATEWAY_ORIGIN,
    )
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=None,
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({frame_bridge.LOCAL_DESKTOP_SUBJECT}),
                    snapshot=_read_snapshot,
                    online=_online,
                    refresh=_refresh_reader(
                        {"status": "completed", "revision": "sha256:" + "a" * 64}
                    ),
                )
            }
        ),
    )

    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url=LOCAL_GATEWAY_ORIGIN) as client:
            preflight = await client.options(
                "/v1/instances/dev/demo/commands/refresh",
                headers={
                    "Origin": LOCAL_APP_ORIGIN,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )
            command = await client.post(
                "/v1/instances/dev/demo/commands/refresh",
                headers={"Origin": LOCAL_APP_ORIGIN, "Content-Type": "application/json"},
                json={
                    "schemaVersion": 1,
                    "correlationId": CORRELATION_ID,
                    "expectedRevision": "sha256:" + "a" * 64,
                },
            )
            return preflight, command

    preflight, command = asyncio.run(run())
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-headers"] == "Content-Type"
    assert command.status_code == 200


def test_authorized_subject_discovers_only_dev_demo_and_reads_redacted_snapshot() -> None:
    private_key, public_key = _keys()
    app = _app(public_key)

    async def action(client):
        discovery = await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(_token(private_key))
        )
        snapshot = await client.get(
            "/v1/instances/dev/demo/snapshot", headers=_headers(_token(private_key))
        )
        return discovery, snapshot

    discovery, snapshot = _exercise(app, action)
    assert discovery.status_code == snapshot.status_code == 200
    assert discovery.json() == {
        "schemaVersion": 1,
        "items": [
            {
                "id": "dev/demo",
                "displayName": "Development demo",
                "availability": "online",
                "capabilities": ["snapshot"],
            }
        ],
        "nextCursor": None,
    }
    assert snapshot.json() == {
        "schemaVersion": 1,
        "contractVersion": "gateway.v1",
        "instanceId": "dev/demo",
        "snapshot": {
            "schemaVersion": 1,
            "revision": "sha256:" + "a" * 64,
            "generatedAt": 1724716800000,
            "projectionPolicy": "beadhive.snapshot-summary/v1",
            "limits": {"maxBytes": 917504, "maxWorkItems": 4096},
            "coverage": {
                "state": "complete",
                "generatedAt": 1724716800000,
                "eligible": 1,
                "returned": 1,
                "reason": None,
                "policy": "beadhive.snapshot-summary/v1",
                "sourceRevision": "sha256:" + "a" * 64,
                "limits": {"maxBytes": 917504, "maxWorkItems": 4096},
                "workItemRetrieval": _retrieval("sha256:" + "a" * 64),
            },
            "workItems": [
                {
                    "id": "bh-1",
                    "title": "Development demo",
                    "status": "open",
                    "readiness": "ready",
                    "issueType": "task",
                    "priority": 1,
                    "labels": ["component:gateway"],
                    "remainingLabelCount": 0,
                    "assignee": "dev/codex",
                    "owner": None,
                    "updatedAt": 1724716800000,
                    "blockerCount": 0,
                    "openGateCount": 0,
                    "liveAgentCount": 0,
                }
            ],
        },
    }
    assert discovery.headers["access-control-allow-origin"] == APP_ORIGIN
    assert snapshot.headers["cache-control"] == "no-store"


def test_public_caller_bearer_is_never_forwarded_to_the_host_daemon() -> None:
    private_key, public_key = _keys()
    caller_bearer = _token(private_key)
    daemon_bearer = "bh1.frame-bridge." + "d" * 43
    seen: list[str | None] = []

    async def daemon(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "live", "ready": True})
        seen.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != f"Bearer {daemon_bearer}":
            return httpx.Response(401, json={"error": {"code": "auth_missing"}})
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "hive": {
                    "prefix": "github/beadhive/beadhive",
                    "provider": "github",
                    "org": "beadhive",
                    "repo": "beadhive",
                    "kind": "org-native",
                },
                "revision": "sha256:" + "a" * 64,
                "generatedAt": 1724716800000,
                "cursor": {
                    "subscriptionId": frame_bridge_runtime.HIVE_SUBSCRIPTION_ID,
                    "producerEpoch": EVENT_EPOCH.replace("-", ""),
                    "sequence": 0,
                    "observedAt": 1724716800000,
                },
                "projectionPolicy": "beadhive.snapshot-summary/v1",
                "limits": {"maxBytes": 917504, "maxWorkItems": 4096},
                "coverage": {
                    "state": "complete",
                    "generatedAt": 1724716800000,
                    "eligible": 0,
                    "returned": 0,
                    "reason": None,
                    "policy": "beadhive.snapshot-summary/v1",
                    "sourceRevision": "sha256:" + "a" * 64,
                    "limits": {"maxBytes": 917504, "maxWorkItems": 4096},
                    "workItemRetrieval": _retrieval("sha256:" + "a" * 64),
                    "sources": {},
                },
                "workItems": [],
            },
        )

    daemon_client = httpx.AsyncClient(
        transport=httpx.MockTransport(daemon),
        base_url=frame_bridge_runtime.LOOPBACK_ORIGIN,
    )
    runtime = frame_bridge_runtime.LoopbackDemoRuntime(
        daemon_bearer=daemon_auth.SecretBearer(daemon_bearer),
        client=daemon_client,
    )
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=runtime.snapshot,
                    online=runtime.online,
                    close=runtime.close,
                )
            }
        ),
    )

    async def action(client):
        try:
            return await client.get(
                "/v1/instances/dev/demo/snapshot",
                headers=_headers(caller_bearer),
            )
        finally:
            await runtime.close()

    response = _exercise(app, action)

    assert response.status_code == 200
    assert seen == [f"Bearer {daemon_bearer}"]
    assert caller_bearer not in "".join(value or "" for value in seen)


def test_authorized_subject_invokes_advertised_refresh_and_receives_correlated_result() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    verifier = frame_bridge.ClerkTokenVerifier(config=config, key=public_key)
    instance = frame_bridge.RemoteInstance(
        display_name="Development demo",
        authorized_subjects=frozenset({SUBJECT}),
        snapshot=_read_snapshot,
        online=_online,
        refresh=_refresh_reader(
            {
                "status": "completed",
                "revision": "sha256:" + "b" * 64,
                "privatePath": "/Users/private/repository",
                "transcript": "must not cross the remote boundary",
            }
        ),
    )
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=verifier,
        registry=frame_bridge.DevelopmentInstanceRegistry(instances={INSTANCE_ID: instance}),
    )

    async def action(client):
        discovery = await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(_token(private_key))
        )
        result = await client.post(
            "/v1/instances/dev/demo/commands/refresh",
            headers=_headers(_token(private_key)),
            json={
                "schemaVersion": 1,
                "correlationId": CORRELATION_ID,
                "expectedRevision": "sha256:" + "a" * 64,
            },
        )
        return discovery, result

    discovery, result = _exercise(app, action)
    assert discovery.json()["items"][0]["capabilities"] == ["snapshot", "refresh"]
    assert result.status_code == 200
    assert result.json() == {
        "schemaVersion": 1,
        "contractVersion": "gateway.v1",
        "instanceId": "dev/demo",
        "command": "refresh",
        "correlationId": CORRELATION_ID,
        "result": {"status": "completed", "revision": "sha256:" + "b" * 64},
    }
    assert frame_bridge.frame_bridge_payload_is_allowlisted("commandResult", result.json())
    assert "/Users/" not in str(result.json())
    assert "transcript" not in str(result.json())
    assert "prod" not in str(result.json()).lower()


def test_refresh_reauthorizes_scope_and_fails_closed_for_hidden_stale_and_revoked_access() -> None:
    private_key, public_key = _keys()
    calls = 0

    async def stale_refresh(_expected_revision: str, _correlation_id: str):
        nonlocal calls
        calls += 1
        raise frame_bridge.StaleCommandScope

    def app_for(*, subjects=frozenset({SUBJECT}), revoked=frozenset(), refresh=stale_refresh):
        config = frame_bridge.DevelopmentFrameBridgeConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            app_origin=APP_ORIGIN,
            gateway_origin=GATEWAY_ORIGIN,
        )
        return frame_bridge.build_development_frame_bridge_application(
            config=config,
            verifier=frame_bridge.ClerkTokenVerifier(
                config=config, key=public_key, revoked_subjects=revoked
            ),
            registry=frame_bridge.DevelopmentInstanceRegistry(
                instances={
                    INSTANCE_ID: frame_bridge.RemoteInstance(
                        display_name="Development demo",
                        authorized_subjects=subjects,
                        snapshot=_read_snapshot,
                        online=_online,
                        refresh=refresh,
                    )
                }
            ),
        )

    body = {
        "schemaVersion": 1,
        "correlationId": CORRELATION_ID,
        "expectedRevision": "sha256:" + "a" * 64,
    }

    async def post(client, *, path="/v1/instances/dev/demo/commands/refresh"):
        return await client.post(path, headers=_headers(_token(private_key)), json=body)

    stale = _exercise(app_for(), post)
    hidden = _exercise(app_for(refresh=None), post)
    missing = _exercise(
        app_for(),
        lambda client: post(client, path="/v1/instances/dev/demo/commands/missing"),
    )
    unauthorized = _exercise(app_for(subjects=frozenset()), post)
    revoked = _exercise(app_for(revoked=frozenset({SUBJECT})), post)

    assert stale.status_code == 409
    assert stale.json() == {
        "error": {
            "code": "scope_conflict",
            "message": "The command scope is stale.",
            "retryable": False,
        }
    }
    assert hidden.status_code == missing.status_code == unauthorized.status_code == 404
    assert hidden.json() == missing.json() == unauthorized.json()
    assert revoked.status_code == 401
    assert calls == 1


def test_refresh_rechecks_changed_instance_policy_after_discovery() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    instances = {
        INSTANCE_ID: frame_bridge.RemoteInstance(
            display_name="Development demo",
            authorized_subjects=frozenset({SUBJECT}),
            snapshot=_read_snapshot,
            online=_online,
            refresh=_refresh_reader({"status": "completed", "revision": "sha256:" + "b" * 64}),
        )
    }
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(instances=instances),
    )

    async def action(client):
        token = _token(private_key)
        discovery = await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(token)
        )
        instances[INSTANCE_ID] = frame_bridge.RemoteInstance(
            display_name="Development demo",
            authorized_subjects=frozenset(),
            snapshot=_read_snapshot,
            online=_online,
            refresh=None,
        )
        command = await client.post(
            "/v1/instances/dev/demo/commands/refresh",
            headers=_headers(token),
            json={
                "schemaVersion": 1,
                "correlationId": CORRELATION_ID,
                "expectedRevision": "sha256:" + "a" * 64,
            },
        )
        return discovery, command

    discovery, command = _exercise(app, action)
    assert discovery.json()["items"][0]["capabilities"] == ["snapshot", "refresh"]
    assert command.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"schemaVersion": 1, "correlationId": "bad/id", "expectedRevision": "sha256:" + "a" * 64},
        {"schemaVersion": 1, "correlationId": CORRELATION_ID, "expectedRevision": "main"},
        {
            "schemaVersion": 1,
            "correlationId": CORRELATION_ID,
            "expectedRevision": "sha256:" + "a" * 64,
            "secret": "forbidden",
        },
    ],
)
def test_refresh_input_is_an_exact_non_disclosing_wire_shape(body) -> None:
    private_key, public_key = _keys()
    app = _app(public_key)

    async def action(client):
        return await client.post(
            "/v1/instances/dev/demo/commands/refresh",
            headers=_headers(_token(private_key)),
            json=body,
        )

    response = _exercise(app, action)
    assert response.status_code in {400, 404}
    assert set(response.json()) == {"error"}


@pytest.mark.parametrize(
    ("token_factory", "revoked"),
    [
        (lambda key: "", frozenset()),
        (lambda key: _token(key, iss="https://attacker.clerk.accounts.dev"), frozenset()),
        (lambda key: _token(key, aud="another-gateway"), frozenset()),
        (lambda key: _token(key, exp=int(time.time()) - 1), frozenset()),
        (lambda key: _token(key, sub=""), frozenset()),
        (lambda key: _token(key), frozenset({SUBJECT})),
    ],
    ids=["signed-out", "wrong-issuer", "wrong-audience", "expired", "empty-subject", "revoked"],
)
def test_invalid_identities_share_one_non_disclosing_failure(token_factory, revoked) -> None:
    private_key, public_key = _keys()
    app = _app(public_key, revoked=revoked)
    token = token_factory(private_key)

    async def action(client):
        headers = {"Origin": APP_ORIGIN}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await client.get("/v1/instances", params={"limit": "50"}, headers=headers)

    response = _exercise(app, action)
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "authentication_failed",
            "message": "Authentication failed.",
            "retryable": False,
        }
    }
    assert frame_bridge.frame_bridge_payload_is_allowlisted("error", response.json())


def test_clerk_token_category_header_is_supported_and_strictly_validated() -> None:
    private_key, public_key = _keys()
    app = _app(public_key)

    async def action(client):
        valid = await client.get(
            "/v1/instances",
            params={"limit": "50"},
            headers=_headers(_token(private_key, header_overrides={"cat": "cl_test"})),
        )
        invalid = []
        for category in (42, "session", "cl_", "cl_" + "a" * 129):
            invalid.append(
                await client.get(
                    "/v1/instances",
                    params={"limit": "50"},
                    headers=_headers(_token(private_key, header_overrides={"cat": category})),
                )
            )
        return valid, invalid

    valid, invalid = _exercise(app, action)
    assert valid.status_code == 200
    for response in invalid:
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_failed"


def test_wrong_signature_origin_and_instance_fail_before_runtime_access() -> None:
    private_key, public_key = _keys()
    attacker_key, _ = _keys()
    calls = 0

    async def guarded_snapshot():
        nonlocal calls
        calls += 1
        return _snapshot()

    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=guarded_snapshot,
                    online=_online,
                )
            }
        ),
    )

    async def action(client):
        wrong_signature = await client.get(
            "/v1/instances/dev/demo/snapshot", headers=_headers(_token(attacker_key))
        )
        wrong_origin = await client.get(
            "/v1/instances/dev/demo/snapshot",
            headers=_headers(_token(private_key), origin="https://attacker.example"),
        )
        wrong_instance = await client.get(
            "/v1/instances/dev/private/snapshot", headers=_headers(_token(private_key))
        )
        other_stage_instance = await client.get(
            "/v1/instances/other/demo/snapshot", headers=_headers(_token(private_key))
        )
        return wrong_signature, wrong_origin, wrong_instance, other_stage_instance

    wrong_signature, wrong_origin, wrong_instance, other_stage_instance = _exercise(app, action)
    assert (wrong_signature.status_code, wrong_signature.json()["error"]["code"]) == (
        401,
        "authentication_failed",
    )
    assert (wrong_origin.status_code, wrong_origin.json()["error"]["code"]) == (
        403,
        "request_denied",
    )
    for response in (wrong_instance, other_stage_instance):
        assert response.status_code == 404
        assert response.json()["error"] == {
            "code": "resource_not_found",
            "message": "The resource was not found.",
            "retryable": False,
        }
    assert calls == 0


def test_exact_cors_preflight_and_response_allowlists_are_closed() -> None:
    private_key, public_key = _keys()
    app = _app(public_key)

    async def action(client):
        allowed = await client.options(
            "/v1/instances",
            headers={
                "Origin": APP_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        widened = await client.options(
            "/v1/instances",
            headers={
                "Origin": APP_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, X-Private",
            },
        )
        discovery = await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(_token(private_key))
        )
        snapshot = await client.get(
            "/v1/instances/dev/demo/snapshot", headers=_headers(_token(private_key))
        )
        return allowed, widened, discovery, snapshot

    allowed, widened, discovery, snapshot = _exercise(app, action)
    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == APP_ORIGIN
    assert allowed.headers["access-control-allow-headers"] == "Authorization"
    assert (widened.status_code, widened.json()["error"]["code"]) == (403, "request_denied")
    assert frame_bridge.frame_bridge_payload_is_allowlisted("instances", discovery.json())
    assert frame_bridge.frame_bridge_payload_is_allowlisted("snapshot", snapshot.json())
    leaked = str(snapshot.json())
    assert "/Users/" not in leaked
    assert "must-not-leak" not in leaked
    assert "private-runtime" not in leaked


def test_component_rename_preserves_read_only_gateway_wire_error() -> None:
    _private_key, public_key = _keys()
    app = _app(public_key)

    async def action(client):
        return await client.delete("/v1/instances")

    response = _exercise(app, action)
    assert response.status_code == 405
    assert response.json() == {
        "error": {
            "code": "read_only_profile",
            "message": "The gateway is read-only.",
            "retryable": False,
        }
    }


def test_refresh_cors_preflight_allows_only_post_authorization_and_json() -> None:
    _, public_key = _keys()
    app = _app(public_key)

    async def action(client):
        allowed = await client.options(
            "/v1/instances/dev/demo/commands/refresh",
            headers={
                "Origin": APP_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization",
            },
        )
        widened = await client.options(
            "/v1/instances/dev/demo/commands/refresh",
            headers={
                "Origin": APP_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization, X-Private",
            },
        )
        return allowed, widened

    allowed, widened = _exercise(app, action)
    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-methods"] == "POST"
    assert allowed.headers["access-control-allow-headers"] == "Authorization, Content-Type"
    assert widened.status_code == 403


def test_stream_starts_from_snapshot_cursor_and_delivers_monotonic_redacted_events() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    raw_snapshot = _snapshot()
    raw_snapshot["eventCursor"] = f"{EVENT_EPOCH}:1"

    async def open_events(cursor: str):
        assert cursor == f"{EVENT_EPOCH}:1"

        async def events():
            for sequence in (2, 3):
                yield {
                    "cursor": f"{EVENT_EPOCH}:{sequence}",
                    "revision": "sha256:" + str(sequence) * 64,
                    "workspaceRoot": "/Users/private/repository",
                    "transcript": "must not cross the remote boundary",
                }

        return events()

    instance = frame_bridge.RemoteInstance(
        display_name="Development demo",
        authorized_subjects=frozenset({SUBJECT}),
        snapshot=_snapshot_reader(raw_snapshot),
        online=_online,
        events=open_events,
    )
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(instances={INSTANCE_ID: instance}),
    )

    async def action(client):
        token = _token(private_key)
        snapshot = await client.get("/v1/instances/dev/demo/snapshot", headers=_headers(token))
        stream = await client.get(
            "/v1/instances/dev/demo/events",
            params={"cursor": snapshot.json()["snapshot"]["eventCursor"]},
            headers=_headers(token),
        )
        return snapshot, stream

    snapshot, stream = _exercise(app, action)
    assert snapshot.json()["snapshot"]["eventCursor"] == f"{EVENT_EPOCH}:1"
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.text.count("event: snapshot-invalidated") == 2
    assert f"id: {EVENT_EPOCH}:2" in stream.text
    assert f"id: {EVENT_EPOCH}:3" in stream.text
    assert stream.text.index(f"id: {EVENT_EPOCH}:2") < stream.text.index(f"id: {EVENT_EPOCH}:3")
    assert "/Users/" not in stream.text
    assert "transcript" not in stream.text
    assert "prod" not in stream.text.lower()


def test_stream_replay_has_no_duplicates_and_stale_cursor_requires_resnapshot() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )

    async def open_events(cursor: str):
        if cursor.endswith(":0"):
            raise frame_bridge.StaleEventCursor
        sequence = int(cursor.rsplit(":", 1)[1]) + 1

        async def events():
            yield {
                "cursor": f"{EVENT_EPOCH}:{sequence}",
                "revision": "sha256:" + "c" * 64,
            }

        return events()

    raw_snapshot = _snapshot()
    raw_snapshot["eventCursor"] = f"{EVENT_EPOCH}:2"
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_snapshot_reader(raw_snapshot),
                    online=_online,
                    events=open_events,
                )
            }
        ),
    )

    async def action(client):
        token = _token(private_key)
        before_disconnect = await client.get(
            "/v1/instances/dev/demo/events",
            params={"cursor": f"{EVENT_EPOCH}:1"},
            headers=_headers(token),
        )
        replay = await client.get(
            "/v1/instances/dev/demo/events",
            params={"cursor": f"{EVENT_EPOCH}:2"},
            headers=_headers(token),
        )
        stale = await client.get(
            "/v1/instances/dev/demo/events",
            params={"cursor": f"{EVENT_EPOCH}:0"},
            headers=_headers(token),
        )
        return before_disconnect, replay, stale

    before_disconnect, replay, stale = _exercise(app, action)
    assert before_disconnect.text.count(f"id: {EVENT_EPOCH}:2") == 1
    assert f"id: {EVENT_EPOCH}:3" not in before_disconnect.text
    assert replay.text.count(f"id: {EVENT_EPOCH}:3") == 1
    assert f"id: {EVENT_EPOCH}:2" not in replay.text
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "resnapshot_required",
        "message": "A fresh snapshot is required.",
        "retryable": False,
    }


@pytest.mark.parametrize("cursor", [f"{EVENT_EPOCH}:4", "223e4567-e89b-42d3-a456-426614174000:3"])
def test_stream_gap_or_epoch_change_emits_one_resnapshot_control(cursor) -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )

    async def open_events(_cursor: str):
        async def events():
            yield {
                "cursor": cursor,
                "revision": "sha256:" + "d" * 64,
                "secret": "must not cross",
            }

        return events()

    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_read_snapshot,
                    online=_online,
                    events=open_events,
                )
            }
        ),
    )

    async def action(client):
        return await client.get(
            "/v1/instances/dev/demo/events",
            params={"cursor": f"{EVENT_EPOCH}:2"},
            headers=_headers(_token(private_key)),
        )

    response = _exercise(app, action)
    assert response.text == 'event: resnapshot-required\ndata: {"schemaVersion":1}\n\n'
    assert "must not cross" not in response.text


@pytest.mark.parametrize(
    "failure",
    [frame_bridge.EventRetentionGap, frame_bridge.ProducerEpochChanged],
    ids=["retention-gap", "producer-restart"],
)
def test_stream_retention_gap_and_restart_require_resnapshot(failure) -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )

    async def open_events(_cursor: str):
        raise failure

    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_read_snapshot,
                    online=_online,
                    events=open_events,
                )
            }
        ),
    )

    async def action(client):
        return await client.get(
            "/v1/instances/dev/demo/events",
            params={"cursor": f"{EVENT_EPOCH}:2"},
            headers=_headers(_token(private_key)),
        )

    response = _exercise(app, action)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "resnapshot_required"


@pytest.mark.parametrize("revocation", ["scope", "identity"])
def test_idle_stream_closes_promptly_when_authorization_changes(revocation) -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    entered = asyncio.Event()

    async def open_events(_cursor: str):
        async def idle_events():
            entered.set()
            await asyncio.Event().wait()
            if False:
                yield {}

        return idle_events()

    instance = frame_bridge.RemoteInstance(
        display_name="Development demo",
        authorized_subjects=frozenset({SUBJECT}),
        snapshot=_read_snapshot,
        online=_online,
        events=open_events,
    )
    instances = {INSTANCE_ID: instance}
    revoked = False

    def subject_is_revoked(_subject: str) -> bool:
        return revoked

    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(
            config=config, key=public_key, subject_is_revoked=subject_is_revoked
        ),
        registry=frame_bridge.DevelopmentInstanceRegistry(instances=instances),
        runtime_calls=frame_bridge.RuntimeCallPolicy(stream_reauthorize_seconds=0.05),
    )

    async def action(client):
        nonlocal revoked
        request = asyncio.create_task(
            client.get(
                "/v1/instances/dev/demo/events",
                params={"cursor": f"{EVENT_EPOCH}:2"},
                headers=_headers(_token(private_key)),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        if revocation == "scope":
            instances[INSTANCE_ID] = frame_bridge.RemoteInstance(
                display_name="Development demo",
                authorized_subjects=frozenset(),
                snapshot=_read_snapshot,
                online=_online,
            )
        else:
            revoked = True
        return await asyncio.wait_for(request, timeout=0.5)

    response = _exercise(app, action)
    assert response.status_code == 200
    assert response.text == ""


def test_development_profile_refuses_a_different_clerk_development_issuer() -> None:
    with pytest.raises(ValueError, match="exact Clerk Development issuer"):
        frame_bridge.DevelopmentFrameBridgeConfig(
            issuer="https://attacker.clerk.accounts.dev",
            audience=AUDIENCE,
            app_origin=APP_ORIGIN,
            gateway_origin=GATEWAY_ORIGIN,
        )


def test_unadvertised_capability_uses_the_stable_allowlisted_not_found_shape() -> None:
    private_key, public_key = _keys()
    app = _app(public_key)

    async def action(client):
        return await client.get(
            "/v1/instances/dev/demo/commands",
            headers=_headers(_token(private_key)),
        )

    response = _exercise(app, action)
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "resource_not_found",
            "message": "The resource was not found.",
            "retryable": False,
        }
    }
    assert frame_bridge.frame_bridge_payload_is_allowlisted("error", response.json())


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", -1, 2])
def test_incompatible_runtime_snapshot_fails_without_reflecting_internal_content(
    schema_version: object,
) -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    incompatible = _snapshot()
    incompatible["schemaVersion"] = schema_version
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_snapshot_reader(incompatible),
                    online=_online,
                )
            }
        ),
    )

    async def action(client):
        return await client.get(
            "/v1/instances/dev/demo/snapshot", headers=_headers(_token(private_key))
        )

    response = _exercise(app, action)
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "runtime_unavailable",
        "message": "The runtime is unavailable.",
        "retryable": True,
    }
    assert "must-not-leak" not in response.text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["workItems"][0].__setitem__(
            "title", {"secret": "nested-must-not-leak"}
        ),
        lambda value: value["workItems"][0].__setitem__(
            "labels", [{"secret": "nested-must-not-leak"}]
        ),
        lambda value: value["workItems"][0].__setitem__("priority", True),
        lambda value: value["workItems"][0].__setitem__(
            "owner", {"secret": "nested-must-not-leak"}
        ),
        lambda value: value["workItems"][0].__setitem__("updatedAt", -1),
    ],
    ids=["title-object", "label-object", "boolean-priority", "seat-object", "negative-time"],
)
def test_nested_private_values_fail_the_recursive_disclosure_allowlist(mutate) -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    malformed = _snapshot()
    mutate(malformed)
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_snapshot_reader(malformed),
                    online=_online,
                )
            }
        ),
    )

    async def action(client):
        return await client.get(
            "/v1/instances/dev/demo/snapshot", headers=_headers(_token(private_key))
        )

    response = _exercise(app, action)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"
    assert "nested-must-not-leak" not in response.text


def test_discovery_rejects_non_scalar_registry_metadata() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name={"secret": "registry-must-not-leak"},
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_read_snapshot,
                    online=_online,
                )
            }
        ),
    )

    async def action(client):
        return await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(_token(private_key))
        )

    response = _exercise(app, action)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"
    assert "registry-must-not-leak" not in response.text


def test_snapshot_collection_bounds_fail_closed_before_serialization() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    oversized = _snapshot()
    oversized["workItems"] = oversized["workItems"] * 4_097
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_snapshot_reader(oversized),
                    online=_online,
                )
            }
        ),
    )

    async def action(client):
        return await client.get(
            "/v1/instances/dev/demo/snapshot", headers=_headers(_token(private_key))
        )

    response = _exercise(app, action)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"


def test_snapshot_timestamp_outside_json_safe_integer_range_fails_closed() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    unsafe = _snapshot()
    unsafe["generatedAt"] = 2**53
    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_snapshot_reader(unsafe),
                    online=_online,
                )
            }
        ),
    )

    async def action(client):
        return await client.get(
            "/v1/instances/dev/demo/snapshot", headers=_headers(_token(private_key))
        )

    response = _exercise(app, action)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"


def test_snapshot_coverage_allowlist_enforces_strict_truthful_counts() -> None:
    unsafe_eligible = _public_snapshot_envelope()
    unsafe_eligible["snapshot"]["coverage"]["eligible"] = 2**53
    assert not frame_bridge.frame_bridge_payload_is_allowlisted("snapshot", unsafe_eligible)

    boolean_returned = _public_snapshot_envelope()
    boolean_returned["snapshot"]["workItems"] = []
    boolean_returned["snapshot"]["coverage"].update(
        {"state": "complete", "eligible": 0, "returned": False, "reason": None}
    )
    assert not frame_bridge.frame_bridge_payload_is_allowlisted("snapshot", boolean_returned)

    false_structural_cap = _public_snapshot_envelope()
    false_structural_cap["snapshot"]["coverage"].update(
        {"state": "partial", "eligible": 2, "returned": 1, "reason": "structural_cap"}
    )
    assert not frame_bridge.frame_bridge_payload_is_allowlisted("snapshot", false_structural_cap)


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", -1, 2])
def test_schema_versions_require_the_exact_supported_integer(schema_version: object) -> None:
    instance_page = {"schemaVersion": schema_version, "items": [], "nextCursor": None}
    assert not frame_bridge.frame_bridge_payload_is_allowlisted("instances", instance_page)

    envelope = {
        "schemaVersion": schema_version,
        "contractVersion": "gateway.v1",
        "instanceId": "dev/demo",
        "snapshot": {
            "schemaVersion": schema_version,
            "revision": "revision",
            "generatedAt": 0,
            "workItems": [],
            "agents": [],
        },
    }
    assert not frame_bridge.frame_bridge_payload_is_allowlisted("snapshot", envelope)


def test_slow_snapshot_source_does_not_block_other_gateway_requests() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    release = asyncio.Event()

    async def slow_snapshot():
        await release.wait()
        return _snapshot()

    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=slow_snapshot,
                    online=_online,
                )
            }
        ),
    )

    async def action(client):
        token = _token(private_key)
        started_at = time.monotonic()
        snapshot_task = asyncio.create_task(
            client.get("/v1/instances/dev/demo/snapshot", headers=_headers(token))
        )
        await asyncio.sleep(0)
        discovery = await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(token)
        )
        discovery_elapsed = time.monotonic() - started_at
        release.set()
        snapshot = await snapshot_task
        return discovery, snapshot, discovery_elapsed

    discovery, snapshot, discovery_elapsed = _exercise(app, action)
    assert discovery.status_code == snapshot.status_code == 200
    assert discovery_elapsed < 0.25


def test_hung_snapshot_saturation_times_out_without_starving_discovery() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hung_snapshot():
        entered.set()
        await release.wait()
        return _snapshot()

    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=hung_snapshot,
                    online=_online,
                )
            }
        ),
        runtime_calls=frame_bridge.RuntimeCallPolicy(
            deadline_seconds=0.1,
            snapshot_concurrency=1,
            availability_concurrency=1,
        ),
    )

    async def action(client):
        token = _token(private_key)
        first = asyncio.create_task(
            client.get("/v1/instances/dev/demo/snapshot", headers=_headers(token))
        )
        for _ in range(50):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        second = asyncio.create_task(
            client.get("/v1/instances/dev/demo/snapshot", headers=_headers(token))
        )
        discovery = await asyncio.wait_for(
            client.get("/v1/instances", params={"limit": "50"}, headers=_headers(token)),
            timeout=0.25,
        )
        timed_out = await asyncio.gather(first, second)
        return discovery, timed_out

    discovery, timed_out = _exercise(app, action)

    assert discovery.status_code == 200
    assert [response.status_code for response in timed_out] == [503, 503]
    assert all(response.json()["error"]["code"] == "runtime_unavailable" for response in timed_out)


def test_snapshot_availability_saturation_does_not_starve_discovery() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def online() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return True

    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_read_snapshot,
                    online=online,
                )
            }
        ),
        runtime_calls=frame_bridge.RuntimeCallPolicy(
            deadline_seconds=0.1,
            snapshot_concurrency=1,
            availability_concurrency=1,
        ),
    )

    async def action(client):
        token = _token(private_key)
        snapshot = asyncio.create_task(
            client.get("/v1/instances/dev/demo/snapshot", headers=_headers(token))
        )
        for _ in range(50):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        discovery = await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(token)
        )
        return await snapshot, discovery

    snapshot, discovery = _exercise(app, action)

    assert snapshot.status_code == 503
    assert discovery.status_code == 200


def test_discovery_rejects_non_boolean_availability() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )

    async def invalid_online():
        return 1

    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_read_snapshot,
                    online=invalid_online,
                )
            }
        ),
    )

    async def action(client):
        return await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(_token(private_key))
        )

    response = _exercise(app, action)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"


def test_offline_runtime_is_disclosed_but_snapshot_fails_bounded() -> None:
    private_key, public_key = _keys()
    config = frame_bridge.DevelopmentFrameBridgeConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )

    async def offline() -> bool:
        return False

    app = frame_bridge.build_development_frame_bridge_application(
        config=config,
        verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public_key),
        registry=frame_bridge.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: frame_bridge.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_read_snapshot,
                    online=offline,
                )
            }
        ),
    )

    async def action(client):
        token = _token(private_key)
        discovery = await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(token)
        )
        snapshot = await client.get("/v1/instances/dev/demo/snapshot", headers=_headers(token))
        return discovery, snapshot

    discovery, snapshot = _exercise(app, action)
    assert discovery.json()["items"][0]["availability"] == "offline"
    assert snapshot.status_code == 503
    assert snapshot.json()["error"]["code"] == "runtime_unavailable"


def test_runtime_port_rejects_blocking_callbacks() -> None:
    with pytest.raises(TypeError, match="snapshot operation must be async"):
        frame_bridge.RemoteInstance(
            display_name="Development demo",
            authorized_subjects=frozenset({SUBJECT}),
            snapshot=_snapshot,
            online=_online,
        )


def test_lifespan_cancels_runtime_work_and_allows_clean_process_restart() -> None:
    program = textwrap.dedent(
        f"""
        import asyncio
        import time

        import httpx
        from cryptography.hazmat.primitives.asymmetric import rsa
        from joserfc import jwt
        from joserfc.jwk import RSAKey

        from beadhive import frame_bridge

        async def run_once():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private = RSAKey.import_key(key)
            public = RSAKey.import_key(key.public_key())
            config = frame_bridge.DevelopmentFrameBridgeConfig(
                issuer={ISSUER!r},
                audience={AUDIENCE!r},
                app_origin={APP_ORIGIN!r},
                gateway_origin={GATEWAY_ORIGIN!r},
            )
            entered = asyncio.Event()

            async def online():
                return True

            async def never_returns():
                entered.set()
                await asyncio.Event().wait()

            app = frame_bridge.build_development_frame_bridge_application(
                config=config,
                verifier=frame_bridge.ClerkTokenVerifier(config=config, key=public),
                registry=frame_bridge.DevelopmentInstanceRegistry(
                    instances={{
                        {INSTANCE_ID!r}: frame_bridge.RemoteInstance(
                            display_name="Development demo",
                            authorized_subjects=frozenset({{{SUBJECT!r}}}),
                            snapshot=never_returns,
                            online=online,
                        )
                    }}
                ),
                runtime_calls=frame_bridge.RuntimeCallPolicy(deadline_seconds=5),
            )
            token = jwt.encode(
                {{"alg": "RS256", "kid": "test"}},
                {{"iss": {ISSUER!r}, "aud": {AUDIENCE!r}, "sub": {SUBJECT!r},
                  "exp": int(time.time()) + 60}},
                private,
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url={GATEWAY_ORIGIN!r}
            ) as client:
                request = asyncio.create_task(client.get(
                    "/v1/instances/dev/demo/snapshot",
                    headers={{"Authorization": f"Bearer {{token}}", "Origin": {APP_ORIGIN!r}}},
                ))
                await asyncio.wait_for(entered.wait(), timeout=1)
                await asyncio.wait_for(lifespan.__aexit__(None, None, None), timeout=1)
                await asyncio.gather(request, return_exceptions=True)
                assert request.done()
                assert not [
                    task for task in asyncio.all_tasks()
                    if task.get_name().startswith("beadhive-frame-bridge")
                ]

        async def main():
            await run_once()
            await run_once()

        asyncio.run(main())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr


def test_gateway_exchange_uses_semantic_port_without_request_or_identity_labels() -> None:
    _private_key, public_key = _keys()
    sink = RecordingTelemetrySink()
    semantic = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(service="bh-frame-bridge", instance_id="frame-bridge-one"),
    )
    app = _app(public_key, telemetry=semantic)

    async def action(client):
        return await client.get(
            "/healthz?token=secret",
            headers={"Host": "gateway-dev.beadhive.cloud"},
        )

    response = _exercise(app, action)

    assert response.status_code == 200
    assert len(sink.events) == 2
    started, completed = sink.events
    assert started.correlation_id == completed.correlation_id
    assert completed.causation_id == started.event_id
    assert completed.outcome is Outcome.SUCCEEDED
    assert {attribute.key.value: attribute.value for attribute in started.attributes} == {
        "http.method": "GET",
        "operation.kind": "route",
        "surface": "gateway",
        "transport": "http",
    }
    assert "token=secret" not in repr(sink.events)
    assert "gateway-dev.beadhive.cloud" not in repr(sink.events)
