"""Shared HTTP/SSE/MCP/WebSocket network-boundary attack tests."""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import jsonschema
import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import Scope
from starlette.websockets import WebSocket

from beadhive import daemon_auth, daemon_network, host_daemon, operator_api
from beadhive.daemon_config import HostDaemonConfig
from beadhive.daemon_contract import AuthScope

ORIGIN = "https://operator.example"
HOST = "daemon.example"
PROXY = "192.0.2.2"
REMOTE = "198.51.100.9"


def _assert_checked_network_error(
    response: httpx.Response,
    *,
    status: int,
    code: str,
    retryable: bool,
) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    payload = response.json()
    schemas = operator_api.openapi_document()["components"]["schemas"]
    jsonschema.Draft202012Validator(
        {"components": {"schemas": schemas}, "$ref": "#/components/schemas/Error"}
    ).validate(payload)
    assert payload == {
        "schemaVersion": 1,
        "error": {
            "code": code,
            "message": payload["error"]["message"],
            "retryable": retryable,
            "action": "retry" if retryable else None,
            "requestId": None,
        },
    }


def _scope(
    path: str,
    *,
    scope_type: str = "http",
    scheme: str,
    client: str,
    host: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Scope:
    return {
        "type": scope_type,
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": scheme,
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [(b"host", host.encode())],
        "client": (client, 5000),
        "server": (host, 443 if scheme in {"https", "wss"} else 80),
        "subprotocols": ["bh-terminal.v1"] if scope_type == "websocket" else [],
        "state": {},
    }


def _settings(**overrides: object) -> HostDaemonConfig:
    values: dict[str, object] = {
        "bind": "192.0.2.10",
        "tls": {
            "enabled": True,
            "certificate_file": "/secure/cert.pem",
            "private_key_file": "/secure/key.pem",
        },
        "cors": {"allowed_origins": [ORIGIN]},
        "http": {
            "allowed_hosts": [HOST],
            "max_connections": 4,
            "max_request_body_bytes": 1024,
        },
        "auth": {"token_rate_limit_per_minute": 20},
        "mcp": {"max_sessions": 2},
        "sse": {"max_clients": 2},
        "terminal": {"max_sessions": 2},
    }
    values.update(overrides)
    return HostDaemonConfig.model_validate(values)


def _app(settings: HostDaemonConfig):
    async def read(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def body(request: Request) -> JSONResponse:
        return JSONResponse({"size": len(await request.body())})

    async def events(_request: Request) -> StreamingResponse:
        async def frames() -> AsyncIterator[bytes]:
            yield b"event: ready\ndata: {}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    async def terminal(websocket: WebSocket) -> None:
        await websocket.accept(subprotocol="bh-terminal.v1")
        await websocket.close()

    policy = daemon_network.SecureNetworkAdmissionPolicy(settings)
    app = host_daemon.build_application(
        routes=[
            Route("/api/v1/factory", read),
            Route("/api/v1/hives/example/events", events),
            Route("/mcp", body, methods=["POST"]),
            WebSocketRoute("/ws/terminal", terminal),
        ],
        network_policy=policy,
    )
    return app, policy


def _mcp_session_app(
    settings: HostDaemonConfig,
    *,
    monotonic=None,
    handshake_entered: asyncio.Event | None = None,
    release_handshake: asyncio.Event | None = None,
):
    async def mcp(request: Request) -> Response:
        if request.method == "DELETE":
            return Response(status_code=204)
        if handshake_entered is not None and request.headers.get("x-block-handshake") == "true":
            handshake_entered.set()
            assert release_handshake is not None
            await release_handshake.wait()
        status = int(request.headers.get("x-response-status", "200"))
        session_id = request.headers.get("x-response-session")
        headers = {"Mcp-Session-Id": session_id} if session_id is not None else None
        return JSONResponse({"ok": status < 400}, status_code=status, headers=headers)

    policy = daemon_network.SecureNetworkAdmissionPolicy(
        settings,
        **({"monotonic": monotonic} if monotonic is not None else {}),
    )
    app = host_daemon.build_application(
        routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])],
        network_policy=policy,
    )
    return app, policy


@asynccontextmanager
async def _client(
    settings: HostDaemonConfig,
    *,
    scheme: str = "https",
    peer: str = REMOTE,
    headers: dict[str, str] | None = None,
):
    app, policy = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(peer, 5000))
        async with httpx.AsyncClient(
            transport=transport,
            base_url=f"{scheme}://{HOST}",
            headers=headers,
        ) as client:
            yield client, policy


def test_configuration_refuses_insecure_remote_and_nonexact_credentialed_cors() -> None:
    with pytest.raises(ValidationError, match="non-loopback"):
        HostDaemonConfig(bind="192.0.2.10")
    with pytest.raises(ValidationError, match="wildcard"):
        HostDaemonConfig(cors={"allowed_origins": ["*"]})
    with pytest.raises(ValidationError, match="exact HTTP origin"):
        HostDaemonConfig(cors={"allowed_origins": ["https://operator.example/path"]})


def test_exact_host_and_credentialed_cors_fail_closed_without_reflecting_input() -> None:
    async def exercise() -> None:
        async with _client(_settings()) as (client, policy):
            allowed = await client.get("/api/v1/factory", headers={"Origin": ORIGIN})
            bad_host = await client.get("/api/v1/factory", headers={"Host": "attacker.example"})
            bad_origin = await client.get(
                "/api/v1/factory", headers={"Origin": "https://attacker.example"}
            )
            preflight = await client.options(
                "/mcp",
                headers={
                    "Origin": ORIGIN,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )
            invalid_preflight = await client.options(
                "/mcp",
                headers={
                    "Origin": ORIGIN,
                    "Access-Control-Request-Method": "PATCH",
                },
            )

        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == ORIGIN
        assert allowed.headers["access-control-allow-credentials"] == "true"
        _assert_checked_network_error(bad_host, status=400, code="invalid_host", retryable=False)
        _assert_checked_network_error(
            bad_origin, status=403, code="invalid_origin", retryable=False
        )
        _assert_checked_network_error(
            invalid_preflight,
            status=403,
            code="invalid_preflight",
            retryable=False,
        )
        assert "attacker.example" not in bad_host.text + bad_origin.text
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == ORIGIN
        assert preflight.headers["access-control-allow-credentials"] == "true"
        assert policy.metrics.snapshot()["rejected"] == {
            "invalid_host": 1,
            "invalid_origin": 1,
            "invalid_preflight": 1,
        }

    asyncio.run(exercise())


def test_installed_loopback_application_remains_authenticated(tmp_path) -> None:
    credential_file = (tmp_path / "daemon-credentials.json").absolute()
    credential = daemon_auth.provision_credential_file(
        credential_file,
        credential_id="operator",
        audience="beadhive-host",
        principal="operator:test",
        scopes=(AuthScope.OPERATOR_READ,),
        expires_at=int(time.time()) + 3600,
    )
    settings = _settings(
        enabled=True,
        bind="127.0.0.1",
        auth={"credential_file": credential_file},
        http={
            "allowed_hosts": ["localhost"],
            "max_connections": 4,
            "max_request_body_bytes": 1024,
        },
    )
    runtime = host_daemon.DaemonRuntime()
    control_record = host_daemon.ControlRecord(
        contract=host_daemon.CONTRACT_VERSION,
        account_id="uid:test",
        bh_home=str(tmp_path),
        host_id="host-test",
        instance_id="instance-test",
        pid=1,
        process_start="test:1",
        listener_host="127.0.0.1",
        listener_port=8737,
        started_at="2026-09-02T00:00:00+00:00",
    )
    app = host_daemon.build_product_application(
        runtime=runtime,
        control_record=control_record,
        listener_host="127.0.0.1",
        listener_port=8737,
        cfg={"managed_repos": []},
        settings=settings,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                health = await client.get("/health")
                missing = await client.get("/api/v1/factory")
                allowed = await client.get(
                    "/api/v1/factory",
                    headers={
                        "Authorization": ("Bearer " + credential.bearer.reveal_for_authority())
                    },
                )
        assert health.status_code == 200
        assert (missing.status_code, missing.json()["error"]["code"]) == (
            401,
            "unauthorized",
        )
        assert allowed.status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/factory"),
        ("GET", "/api/v1/hives/example/events"),
        ("POST", "/mcp"),
    ],
)
def test_direct_tls_and_trusted_terminator_cover_http_sse_and_mcp(method: str, path: str) -> None:
    async def exercise() -> None:
        direct = _settings()
        async with _client(direct) as (client, _policy):
            response = await client.request(method, path, headers={"Origin": ORIGIN})
            assert response.status_code == 200

        proxied = _settings(
            tls={"enabled": False},
            proxy={"tls_terminating": True, "trusted_addresses": [PROXY]},
        )
        forwarded = {
            "Origin": ORIGIN,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": REMOTE,
        }
        async with _client(proxied, scheme="http", peer=PROXY, headers=forwarded) as (
            client,
            _policy,
        ):
            response = await client.request(method, path)
            assert response.status_code == 200

    asyncio.run(exercise())


def test_untrusted_forwarding_and_insecure_remote_transport_are_refused() -> None:
    async def exercise() -> None:
        proxied = _settings(
            tls={"enabled": False},
            proxy={"tls_terminating": True, "trusted_addresses": [PROXY]},
        )
        forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-For": REMOTE}
        async with _client(proxied, scheme="http", peer="192.0.2.99", headers=forwarded) as (
            client,
            _policy,
        ):
            untrusted = await client.get("/api/v1/factory")

        async with _client(_settings(), scheme="http") as (client, _policy):
            insecure = await client.get("/api/v1/factory")

        trusted = _settings(
            tls={"enabled": False},
            proxy={"tls_terminating": True, "trusted_addresses": [PROXY]},
        )
        async with _client(
            trusted,
            scheme="http",
            peer=PROXY,
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": REMOTE,
                "X-Forwarded-Port": "443",
            },
        ) as (client, _policy):
            ambiguous = await client.get("/api/v1/factory")

        assert (untrusted.status_code, untrusted.json()["error"]["code"]) == (
            403,
            "untrusted_forwarded_headers",
        )
        assert (insecure.status_code, insecure.json()["error"]["code"]) == (
            400,
            "insecure_transport",
        )
        assert (ambiguous.status_code, ambiguous.json()["error"]["code"]) == (
            400,
            "invalid_forwarded_headers",
        )

    asyncio.run(exercise())


def test_rate_bucket_cardinality_and_direct_tls_protocol_floor_are_bounded() -> None:
    async def exercise() -> None:
        policy = daemon_network.SecureNetworkAdmissionPolicy(
            _settings(auth={"token_rate_limit_per_minute": 1_000_000})
        )
        for index in range(1_100):
            scope = _scope("/api/v1/factory", scheme="https", client=REMOTE, host=HOST)
            scope["headers"] = [
                (b"host", HOST.encode()),
                (b"authorization", f"Bearer distinct-{index}".encode()),
            ]
            admission = await policy.admit(scope)
            await policy.release(admission)
        assert policy.rate_bucket_count <= 1_024
        assert "distinct-1099" not in repr(admission)

    asyncio.run(exercise())

    context = host_daemon._secure_ssl_context(
        None,
        lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
        minimum_version="TLSv1.3",
    )
    assert context.minimum_version is ssl.TLSVersion.TLSv1_3
    assert context.options & ssl.OP_NO_COMPRESSION


def test_equivalent_bearer_scheme_spellings_share_one_rate_quota() -> None:
    async def exercise() -> None:
        policy = daemon_network.SecureNetworkAdmissionPolicy(
            _settings(auth={"token_rate_limit_per_minute": 1})
        )
        first = _scope("/api/v1/factory", scheme="https", client=REMOTE, host=HOST)
        first["headers"] = [
            (b"host", HOST.encode()),
            (b"authorization", b"bEaReR opaque-token"),
        ]
        admission = await policy.admit(first)
        await policy.release(admission)

        equivalent = _scope("/api/v1/factory", scheme="https", client=REMOTE, host=HOST)
        equivalent["headers"] = [
            (b"host", HOST.encode()),
            (b"authorization", b"Bearer opaque-token"),
        ]
        with pytest.raises(daemon_network.NetworkRejected) as caught:
            await policy.admit(equivalent)

        assert caught.value.code is daemon_network.NetworkErrorCode.TOKEN_RATE_LIMITED
        assert policy.rate_bucket_count == 1
        assert "opaque-token" not in repr(caught.value) + repr(policy.metrics.snapshot())

    asyncio.run(exercise())


def test_mcp_session_ceiling_tracks_sequential_reuse_termination_failure_and_expiry() -> None:
    async def exercise() -> None:
        now = [100.0]
        settings = _settings(
            mcp={
                "max_sessions": 2,
                "session_idle_seconds": 5,
                "session_absolute_seconds": 20,
            }
        )
        app, policy = _mcp_session_app(settings, monotonic=lambda: now[0])
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=(REMOTE, 5000))
            async with httpx.AsyncClient(transport=transport, base_url=f"https://{HOST}") as client:
                failed = await client.post(
                    "/mcp",
                    headers={"X-Response-Status": "400", "X-Response-Session": "failed"},
                )
                created = await client.post("/mcp", headers={"X-Response-Session": "session-one"})
                reused = await client.post("/mcp", headers={"Mcp-Session-Id": "session-one"})
                second = await client.post("/mcp", headers={"X-Response-Session": "session-two"})
                assert policy.active_mcp_session_count == 2
                at_capacity = await client.post(
                    "/mcp", headers={"X-Response-Session": "session-three"}
                )
                terminated = await client.delete("/mcp", headers={"Mcp-Session-Id": "session-one"})
                assert policy.active_mcp_session_count == 1
                replacement = await client.post(
                    "/mcp", headers={"X-Response-Session": "session-three"}
                )
                assert policy.active_mcp_session_count == 2
                now[0] += 6
                after_idle_expiry = await client.post(
                    "/mcp", headers={"X-Response-Session": "session-four"}
                )
                assert policy.active_mcp_session_count == 1
                for timestamp in (110.0, 114.0, 118.0, 122.0, 125.0):
                    now[0] = timestamp
                    touched = await client.post("/mcp", headers={"Mcp-Session-Id": "session-four"})
                    assert touched.status_code == 200
                now[0] = 126.0
                after_absolute_expiry = await client.post(
                    "/mcp", headers={"X-Response-Session": "session-five"}
                )
                assert policy.active_mcp_session_count == 1

        assert failed.status_code == 400
        assert created.headers["mcp-session-id"] == "session-one"
        assert reused.status_code == 200
        assert second.headers["mcp-session-id"] == "session-two"
        _assert_checked_network_error(
            at_capacity,
            status=503,
            code="session_limit_reached",
            retryable=True,
        )
        assert terminated.status_code == 204
        assert replacement.headers["mcp-session-id"] == "session-three"
        assert after_idle_expiry.headers["mcp-session-id"] == "session-four"
        assert after_absolute_expiry.headers["mcp-session-id"] == "session-five"
        assert policy.metrics.snapshot()["rejected"] == {"session_limit_reached": 1}

    asyncio.run(exercise())


def test_mcp_session_owner_can_forget_one_exact_session_idempotently() -> None:
    async def exercise() -> None:
        policy = daemon_network.SecureNetworkAdmissionPolicy(_settings(mcp={"max_sessions": 2}))

        for session_id in ("session-one", "session-two"):
            scope = _scope("/mcp", scheme="https", client=REMOTE, host=HOST)
            scope["method"] = "POST"
            admission = await policy.admit(scope)
            await policy.observe_response_start(
                admission,
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"mcp-session-id", session_id.encode())],
                },
            )
            await policy.release(admission)

        await policy.forget_mcp_session("session-one")
        await policy.forget_mcp_session("session-one")
        assert policy.active_mcp_session_count == 1

        with pytest.raises(daemon_network.NetworkRejected) as caught:
            await policy.forget_mcp_session("session\nsecret")
        assert caught.value.code is daemon_network.NetworkErrorCode.INVALID_SESSION
        assert "session\nsecret" not in repr(caught.value)
        assert policy.active_mcp_session_count == 1

        await policy.forget_mcp_session("session-two")
        assert policy.active_mcp_session_count == 0

    asyncio.run(exercise())


def test_mcp_session_ceiling_reserves_concurrent_handshakes_and_releases_failures() -> None:
    async def exercise() -> None:
        handshake_entered = asyncio.Event()
        release_handshake = asyncio.Event()
        app, _policy = _mcp_session_app(
            _settings(mcp={"max_sessions": 1}),
            handshake_entered=handshake_entered,
            release_handshake=release_handshake,
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=(REMOTE, 5000))
            async with httpx.AsyncClient(transport=transport, base_url=f"https://{HOST}") as client:
                first = asyncio.create_task(
                    client.post(
                        "/mcp",
                        headers={
                            "X-Block-Handshake": "true",
                            "X-Response-Session": "session-one",
                        },
                    )
                )
                await handshake_entered.wait()
                competing = await client.post("/mcp", headers={"X-Response-Session": "session-two"})
                release_handshake.set()
                created = await first

        assert created.headers["mcp-session-id"] == "session-one"
        assert (competing.status_code, competing.json()["error"]["code"]) == (
            503,
            "session_limit_reached",
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "authority",
    [
        "[::1]:0",
        "[::1]:65536",
        "[::1]:999999",
        "[::1]:443:garbage",
        "[::1]garbage",
    ],
)
def test_host_authority_rejects_malformed_or_out_of_range_ipv6_ports(authority: str) -> None:
    async def exercise() -> None:
        policy = daemon_network.SecureNetworkAdmissionPolicy(
            _settings(http={"allowed_hosts": ["[::1]"], "max_request_body_bytes": 1024})
        )
        with pytest.raises(daemon_network.NetworkRejected) as caught:
            await policy.admit(_scope("/health", scheme="https", client=REMOTE, host=authority))
        assert caught.value.code is daemon_network.NetworkErrorCode.INVALID_HOST

        valid = await policy.admit(
            _scope("/health", scheme="https", client=REMOTE, host="[::1]:443")
        )
        await policy.release(valid)

    asyncio.run(exercise())


def test_oversized_body_connection_session_and_token_rate_limits_are_observable() -> None:
    async def exercise() -> None:
        settings = _settings(
            http={
                "allowed_hosts": [HOST],
                "max_connections": 1,
                "max_request_body_bytes": 1024,
            },
            auth={"token_rate_limit_per_minute": 1},
            mcp={"max_sessions": 1},
        )
        app, policy = _app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=(REMOTE, 5000))
            async with httpx.AsyncClient(transport=transport, base_url=f"https://{HOST}") as client:
                oversized = await client.post("/mcp", content=b"x" * 1025)
                first = await client.get(
                    "/api/v1/factory", headers={"Authorization": "Bearer opaque-token"}
                )
                rate_limited = await client.get(
                    "/api/v1/factory", headers={"Authorization": "Bearer opaque-token"}
                )

        assert (oversized.status_code, oversized.json()["error"]["code"]) == (
            413,
            "request_body_too_large",
        )
        assert first.status_code == 200
        assert (rate_limited.status_code, rate_limited.json()["error"]["code"]) == (
            429,
            "token_rate_limited",
        )
        _assert_checked_network_error(
            oversized, status=413, code="request_body_too_large", retryable=False
        )
        _assert_checked_network_error(
            rate_limited, status=429, code="token_rate_limited", retryable=True
        )
        assert "opaque-token" not in oversized.text + rate_limited.text

        first_admission = await policy.admit(
            _scope("/mcp", scheme="https", client=REMOTE, host=HOST)
        )
        with pytest.raises(daemon_network.NetworkRejected) as connection_error:
            await policy.admit(_scope("/mcp", scheme="https", client="198.51.100.10", host=HOST))
        assert connection_error.value.code == "connection_limit_reached"
        policy.record_rejection(connection_error.value.code)
        await policy.release(first_admission)

        session_policy = daemon_network.SecureNetworkAdmissionPolicy(
            _settings(
                http={
                    "allowed_hosts": [HOST],
                    "max_connections": 2,
                    "max_request_body_bytes": 1024,
                },
                mcp={"max_sessions": 1},
            )
        )
        session_scope = _scope("/mcp", scheme="https", client=REMOTE, host=HOST)
        session_scope["method"] = "POST"
        session = await session_policy.admit(session_scope)
        await session_policy.observe_response_start(
            session,
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"mcp-session-id", b"session-one")],
            },
        )
        await session_policy.release(session)
        competing_scope = _scope("/mcp", scheme="https", client="198.51.100.10", host=HOST)
        competing_scope["method"] = "POST"
        with pytest.raises(daemon_network.NetworkRejected) as session_error:
            await session_policy.admit(competing_scope)
        assert session_error.value.code == "session_limit_reached"
        session_policy.record_rejection(session_error.value.code)
        termination_scope = _scope("/mcp", scheme="https", client=REMOTE, host=HOST)
        termination_scope["method"] = "DELETE"
        termination_scope["headers"] = [
            (b"host", HOST.encode()),
            (b"mcp-session-id", b"session-one"),
        ]
        termination = await session_policy.admit(termination_scope)
        await session_policy.observe_response_start(
            termination,
            {"type": "http.response.start", "status": 204, "headers": []},
        )
        await session_policy.release(termination)
        assert session_policy.active_mcp_session_count == 0

        assert policy.metrics.snapshot()["rejected"] == {
            "connection_limit_reached": 1,
            "request_body_too_large": 1,
            "token_rate_limited": 1,
        }
        assert session_policy.metrics.snapshot()["rejected"] == {"session_limit_reached": 1}

    asyncio.run(exercise())


def test_request_body_timeout_uses_the_checked_error_envelope() -> None:
    async def exercise() -> httpx.Response:
        app, _policy = _app(
            _settings(
                http={
                    "allowed_hosts": [HOST],
                    "max_connections": 1,
                    "max_request_body_bytes": 1024,
                    "request_timeout_seconds": 0.01,
                }
            )
        )
        sent: list[dict] = []

        async def blocked_receive() -> dict:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send(message: dict) -> None:
            sent.append(message)

        async with app.router.lifespan_context(app):
            await app(
                _scope("/api/v1/factory", scheme="https", client=REMOTE, host=HOST),
                blocked_receive,
                send,
            )
        start = next(message for message in sent if message["type"] == "http.response.start")
        body = b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body"
        )
        return httpx.Response(
            start["status"],
            headers=start["headers"],
            content=body,
        )

    response = asyncio.run(exercise())
    _assert_checked_network_error(response, status=408, code="request_timeout", retryable=True)


def test_checked_operator_routes_document_live_secure_boundary_failures() -> None:
    document = operator_api.openapi_document()
    common_get = {
        "400": {"$ref": "#/components/responses/BadRequest"},
        "403": {"$ref": "#/components/responses/Forbidden"},
        "408": {"$ref": "#/components/responses/RequestTimeout"},
        "413": {"$ref": "#/components/responses/PayloadTooLarge"},
        "503": {"$ref": "#/components/responses/Unavailable"},
    }

    for path, item in document["paths"].items():
        for status, response in common_get.items():
            assert item["get"]["responses"][status] == response
        if path != "/health":
            assert item["get"]["responses"]["429"] == {"$ref": "#/components/responses/RateLimited"}
        else:
            assert "429" not in item["get"]["responses"]
        if "options" in item:
            assert {
                status: item["options"]["responses"][status] for status in ("400", "403", "503")
            } == {
                "400": {"$ref": "#/components/responses/BadRequest"},
                "403": {"$ref": "#/components/responses/Forbidden"},
                "503": {"$ref": "#/components/responses/Unavailable"},
            }
            assert not ({"408", "413", "429"} & item["options"]["responses"].keys())

    for name in ("RequestTimeout", "PayloadTooLarge", "RateLimited"):
        assert document["components"]["responses"][name]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/Error"}


def test_health_preflight_and_connection_limit_live_errors_match_checked_statuses() -> None:
    async def exercise() -> tuple[httpx.Response, httpx.Response, tuple[httpx.Response, ...]]:
        app, policy = _app(
            _settings(
                http={
                    "allowed_hosts": [HOST],
                    "max_connections": 1,
                    "max_request_body_bytes": 1024,
                },
                auth={"token_rate_limit_per_minute": 1_000_000},
            )
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=(REMOTE, 5000))
            async with httpx.AsyncClient(transport=transport, base_url=f"https://{HOST}") as client:
                health_oversized = await client.request("GET", "/health", content=b"x" * 1025)
                invalid_preflight = await client.options(
                    "/api/v1/factory",
                    headers={
                        "Host": "bad host",
                        "Origin": ORIGIN,
                        "Access-Control-Request-Method": "GET",
                    },
                )
                held = await policy.admit(
                    _scope("/api/v1/factory", scheme="https", client=REMOTE, host=HOST)
                )
                try:
                    limited = (
                        await client.get("/health"),
                        await client.get("/openapi.json"),
                        await client.options(
                            "/api/v1/factory",
                            headers={
                                "Origin": ORIGIN,
                                "Access-Control-Request-Method": "GET",
                            },
                        ),
                        await client.options(
                            "/api/v1/hives/example/events",
                            headers={
                                "Origin": ORIGIN,
                                "Access-Control-Request-Method": "GET",
                            },
                        ),
                    )
                finally:
                    await policy.release(held)
        return health_oversized, invalid_preflight, limited

    health_oversized, invalid_preflight, limited = asyncio.run(exercise())
    _assert_checked_network_error(
        health_oversized,
        status=413,
        code="request_body_too_large",
        retryable=False,
    )
    _assert_checked_network_error(
        invalid_preflight,
        status=400,
        code="invalid_host",
        retryable=False,
    )
    for response in limited:
        _assert_checked_network_error(
            response,
            status=503,
            code="connection_limit_reached",
            retryable=True,
        )


@pytest.mark.parametrize(
    ("scheme", "peer", "extra_headers"),
    [
        ("wss", REMOTE, {}),
        (
            "ws",
            PROXY,
            {"x-forwarded-proto": "https", "x-forwarded-for": REMOTE},
        ),
    ],
)
def test_websocket_origin_is_exact_for_direct_tls_and_trusted_proxy(
    scheme: str, peer: str, extra_headers: dict[str, str]
) -> None:
    settings = (
        _settings()
        if scheme == "wss"
        else _settings(
            tls={"enabled": False},
            proxy={"tls_terminating": True, "trusted_addresses": [PROXY]},
        )
    )
    app, _policy = _app(settings)

    async def handshake(origin: str) -> list[dict]:
        sent: list[dict] = []
        received = False

        async def receive() -> dict:
            nonlocal received
            if not received:
                received = True
                return {"type": "websocket.connect"}
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message: dict) -> None:
            sent.append(message)

        headers = [(b"host", HOST.encode()), (b"origin", origin.encode())]
        headers.extend((key.encode(), value.encode()) for key, value in extra_headers.items())
        await app(
            _scope(
                "/ws/terminal",
                scope_type="websocket",
                scheme=scheme,
                client=peer,
                host=HOST,
                headers=headers,
            ),
            receive,
            send,
        )
        return sent

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            allowed = await handshake(ORIGIN)
            refused = await handshake("https://attacker.example")
        assert any(message["type"] == "websocket.accept" for message in allowed)
        assert refused == [{"type": "websocket.close", "code": 4403, "reason": "Forbidden"}]

    asyncio.run(exercise())
