"""Conformance coverage for the authenticated Development gateway profile."""

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

from beadhive import remote_gateway

ISSUER = "https://rapid-snail-6758.clerk.accounts.dev"
AUDIENCE = "beadhive-gateway-dev"
APP_ORIGIN = "https://app.dev.beadhive.cloud"
GATEWAY_ORIGIN = "https://gateway.dev.beadhive.cloud"
INSTANCE_ID = "dev/demo"
SUBJECT = "user_dev_demo"
CORRELATION_ID = "123e4567-e89b-42d3-a456-426614174000"


def _keys() -> tuple[RSAKey, RSAKey]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return RSAKey.import_key(private), RSAKey.import_key(private.public_key())


def _token(private_key: RSAKey, **overrides: object) -> str:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    return jwt.encode({"alg": "RS256", "kid": "development-test"}, claims, private_key)


def _snapshot() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "revision": "sha256:" + "a" * 64,
        "generatedAt": 1724716800000,
        "workItems": [
            {
                "ref": {"hiveId": "github/beadhive/beadhive", "kind": "work-item", "id": "bh-1"},
                "record": {
                    "id": "bh-1",
                    "title": "Development demo",
                    "status": "open",
                    "issueType": "task",
                    "priority": 1,
                    "labels": ["component:gateway"],
                    "assignee": "dev/codex",
                    "description": "must not cross the remote boundary",
                },
                "updatedAt": 1724716800000,
                "revision": "private-revision",
            }
        ],
        "agents": [
            {
                "ref": {"hiveId": "github/beadhive/beadhive", "kind": "agent-run", "id": "run-1"},
                "state": "running",
                "ownerSeat": "dev",
                "startedAt": 1724716700000,
                "updatedAt": 1724716800000,
                "endedAt": None,
                "runtime": "private-runtime",
            }
        ],
        "workspaceRoot": "/Users/private/repository",
        "secret": "must-not-leak",
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


def _app(public_key: RSAKey, *, revoked: frozenset[str] = frozenset()):
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    verifier = remote_gateway.ClerkTokenVerifier(
        config=config,
        key=public_key,
        revoked_subjects=revoked,
    )
    registry = remote_gateway.DevelopmentInstanceRegistry(
        instances={
            INSTANCE_ID: remote_gateway.RemoteInstance(
                display_name="Development demo",
                authorized_subjects=frozenset({SUBJECT}),
                snapshot=_read_snapshot,
                online=_online,
            )
        }
    )
    return remote_gateway.build_development_gateway_application(
        config=config,
        verifier=verifier,
        registry=registry,
    )


def _exercise(app, action):
    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url=GATEWAY_ORIGIN) as client:
            return await action(client)

    return asyncio.run(run())


def _headers(token: str, *, origin: str = APP_ORIGIN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Origin": origin}


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
            "workItems": [
                {
                    "id": "bh-1",
                    "title": "Development demo",
                    "status": "open",
                    "issueType": "task",
                    "priority": 1,
                    "labels": ["component:gateway"],
                    "assignee": "dev/codex",
                    "updatedAt": 1724716800000,
                }
            ],
            "agents": [
                {
                    "id": "run-1",
                    "state": "running",
                    "ownerSeat": "dev",
                    "startedAt": 1724716700000,
                    "updatedAt": 1724716800000,
                    "endedAt": None,
                }
            ],
        },
    }
    assert discovery.headers["access-control-allow-origin"] == APP_ORIGIN
    assert snapshot.headers["cache-control"] == "no-store"


def test_authorized_subject_invokes_advertised_refresh_and_receives_correlated_result() -> None:
    private_key, public_key = _keys()
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    verifier = remote_gateway.ClerkTokenVerifier(config=config, key=public_key)
    instance = remote_gateway.RemoteInstance(
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
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=verifier,
        registry=remote_gateway.DevelopmentInstanceRegistry(instances={INSTANCE_ID: instance}),
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
    assert remote_gateway.remote_payload_is_allowlisted("commandResult", result.json())
    assert "/Users/" not in str(result.json())
    assert "transcript" not in str(result.json())
    assert "prod" not in str(result.json()).lower()


def test_refresh_reauthorizes_scope_and_fails_closed_for_hidden_stale_and_revoked_access() -> None:
    private_key, public_key = _keys()
    calls = 0

    async def stale_refresh(_expected_revision: str, _correlation_id: str):
        nonlocal calls
        calls += 1
        raise remote_gateway.StaleCommandScope

    def app_for(*, subjects=frozenset({SUBJECT}), revoked=frozenset(), refresh=stale_refresh):
        config = remote_gateway.DevelopmentGatewayConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            app_origin=APP_ORIGIN,
            gateway_origin=GATEWAY_ORIGIN,
        )
        return remote_gateway.build_development_gateway_application(
            config=config,
            verifier=remote_gateway.ClerkTokenVerifier(
                config=config, key=public_key, revoked_subjects=revoked
            ),
            registry=remote_gateway.DevelopmentInstanceRegistry(
                instances={
                    INSTANCE_ID: remote_gateway.RemoteInstance(
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
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    instances = {
        INSTANCE_ID: remote_gateway.RemoteInstance(
            display_name="Development demo",
            authorized_subjects=frozenset({SUBJECT}),
            snapshot=_read_snapshot,
            online=_online,
            refresh=_refresh_reader({"status": "completed", "revision": "sha256:" + "b" * 64}),
        )
    }
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(instances=instances),
    )

    async def action(client):
        token = _token(private_key)
        discovery = await client.get(
            "/v1/instances", params={"limit": "50"}, headers=_headers(token)
        )
        instances[INSTANCE_ID] = remote_gateway.RemoteInstance(
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
    assert remote_gateway.remote_payload_is_allowlisted("error", response.json())


def test_wrong_signature_origin_and_instance_fail_before_runtime_access() -> None:
    private_key, public_key = _keys()
    attacker_key, _ = _keys()
    calls = 0

    async def guarded_snapshot():
        nonlocal calls
        calls += 1
        return _snapshot()

    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
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
    assert remote_gateway.remote_payload_is_allowlisted("instances", discovery.json())
    assert remote_gateway.remote_payload_is_allowlisted("snapshot", snapshot.json())
    leaked = str(snapshot.json())
    assert "/Users/" not in leaked
    assert "must-not-leak" not in leaked
    assert "private-runtime" not in leaked


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


def test_development_profile_refuses_a_different_clerk_development_issuer() -> None:
    with pytest.raises(ValueError, match="exact Clerk Development issuer"):
        remote_gateway.DevelopmentGatewayConfig(
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
    assert remote_gateway.remote_payload_is_allowlisted("error", response.json())


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", -1, 2])
def test_incompatible_runtime_snapshot_fails_without_reflecting_internal_content(
    schema_version: object,
) -> None:
    private_key, public_key = _keys()
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    incompatible = _snapshot()
    incompatible["schemaVersion"] = schema_version
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
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
        lambda value: value["workItems"][0]["record"].__setitem__(
            "title", {"secret": "nested-must-not-leak"}
        ),
        lambda value: value["workItems"][0]["record"].__setitem__(
            "labels", [{"secret": "nested-must-not-leak"}]
        ),
        lambda value: value["workItems"][0]["record"].__setitem__("priority", True),
        lambda value: value["agents"][0].__setitem__(
            "ownerSeat", {"secret": "nested-must-not-leak"}
        ),
        lambda value: value["agents"][0].__setitem__("updatedAt", -1),
    ],
    ids=["title-object", "label-object", "boolean-priority", "seat-object", "negative-time"],
)
def test_nested_private_values_fail_the_recursive_disclosure_allowlist(mutate) -> None:
    private_key, public_key = _keys()
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    malformed = _snapshot()
    mutate(malformed)
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
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
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
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
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    oversized = _snapshot()
    oversized["workItems"] = oversized["workItems"] * 1_001
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
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
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    unsafe = _snapshot()
    unsafe["generatedAt"] = 2**53
    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
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


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", -1, 2])
def test_schema_versions_require_the_exact_supported_integer(schema_version: object) -> None:
    instance_page = {"schemaVersion": schema_version, "items": [], "nextCursor": None}
    assert not remote_gateway.remote_payload_is_allowlisted("instances", instance_page)

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
    assert not remote_gateway.remote_payload_is_allowlisted("snapshot", envelope)


def test_slow_snapshot_source_does_not_block_other_gateway_requests() -> None:
    private_key, public_key = _keys()
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    release = asyncio.Event()

    async def slow_snapshot():
        await release.wait()
        return _snapshot()

    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
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
    config = remote_gateway.DevelopmentGatewayConfig(
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

    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=hung_snapshot,
                    online=_online,
                )
            }
        ),
        runtime_calls=remote_gateway.RuntimeCallPolicy(
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
    config = remote_gateway.DevelopmentGatewayConfig(
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

    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
                    display_name="Development demo",
                    authorized_subjects=frozenset({SUBJECT}),
                    snapshot=_read_snapshot,
                    online=online,
                )
            }
        ),
        runtime_calls=remote_gateway.RuntimeCallPolicy(
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
    config = remote_gateway.DevelopmentGatewayConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )

    async def invalid_online():
        return 1

    app = remote_gateway.build_development_gateway_application(
        config=config,
        verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public_key),
        registry=remote_gateway.DevelopmentInstanceRegistry(
            instances={
                INSTANCE_ID: remote_gateway.RemoteInstance(
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


def test_runtime_port_rejects_blocking_callbacks() -> None:
    with pytest.raises(TypeError, match="snapshot operation must be async"):
        remote_gateway.RemoteInstance(
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

        from beadhive import remote_gateway

        async def run_once():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private = RSAKey.import_key(key)
            public = RSAKey.import_key(key.public_key())
            config = remote_gateway.DevelopmentGatewayConfig(
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

            app = remote_gateway.build_development_gateway_application(
                config=config,
                verifier=remote_gateway.ClerkTokenVerifier(config=config, key=public),
                registry=remote_gateway.DevelopmentInstanceRegistry(
                    instances={{
                        {INSTANCE_ID!r}: remote_gateway.RemoteInstance(
                            display_name="Development demo",
                            authorized_subjects=frozenset({{{SUBJECT!r}}}),
                            snapshot=never_returns,
                            online=online,
                        )
                    }}
                ),
                runtime_calls=remote_gateway.RuntimeCallPolicy(deadline_seconds=5),
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
                    if task.get_name().startswith("beadhive-gateway")
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
