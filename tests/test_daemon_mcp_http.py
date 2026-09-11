"""Transport contracts for the authenticated host-daemon MCP surface."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.server.streamable_http import StreamableHTTPServerTransport

from beadhive import config as config_mod
from beadhive import daemon_auth, daemon_state_broker, host_daemon
from beadhive.daemon_config import HostDaemonConfig
from beadhive.daemon_contract import AuthScope

_raw_build_product_application = host_daemon.build_product_application


def _build_product_application(**kwargs):
    return _raw_build_product_application(
        state_broker_factory=daemon_state_broker.DaemonStateBroker.for_host,
        **kwargs,
    )


# Inert input for every registered tool. Refusal is a valid parity result; a transport error is
# not. Keeping the table exact makes discovery fail closed when the authoritative stdio registry
# changes.
_TOOL_ARGS = {
    "plan_check": {"spec": {}},
    "plan_file": {"spec": {}, "dry_run": True},
    "work_refine": {"bead": "bh-none", "dry_run": True},
    "bd_create": {"issues": []},
    "hive_list": {},
    "config_set": {"key": "otel.protocol", "value": "not-a-protocol"},
    "hive_add": {"provider": "", "org": "", "repo": ""},
    "hive_onboard": {"provider": "", "org": "", "repo": ""},
    "hive_status": {},
    "toolchain_exec": {"argv": []},
}


def _settings(
    tmp_path: Path,
    *,
    mode: str,
    revalidation_seconds: float = 30.0,
    max_sessions: int = 64,
    idle_seconds: float = 900.0,
    absolute_seconds: float = 14_400.0,
) -> tuple[HostDaemonConfig, str]:
    credential_file = (tmp_path / "daemon-credentials.json").absolute()
    credential = daemon_auth.provision_credential_file(
        credential_file,
        credential_id="mcp-client",
        audience="beadhive-host",
        principal="agent:test",
        scopes=(AuthScope.MCP_CONTROL,),
        expires_at=4_102_444_800,
    )
    settings = HostDaemonConfig(
        enabled=True,
        auth={
            "credential_file": credential_file,
            "session_revalidation_seconds": revalidation_seconds,
        },
        http={"allowed_hosts": ["localhost"]},
        mcp={
            "mode": mode,
            "max_sessions": max_sessions,
            "session_idle_seconds": idle_seconds,
            "session_absolute_seconds": absolute_seconds,
        },
    )
    return settings, credential.bearer.reveal_for_authority()


def _record(tmp_path: Path) -> host_daemon.ControlRecord:
    return host_daemon.ControlRecord(
        contract=host_daemon.CONTRACT_VERSION,
        account_id="uid:test",
        bh_home=str(tmp_path),
        host_id="host-test",
        instance_id="instance-test",
        pid=os.getpid(),
        process_start="test:1",
        listener_host="127.0.0.1",
        listener_port=8737,
        started_at="2026-09-03T00:00:00Z",
    )


def _headers(bearer: str, *, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _initialize_request(client_name: str = "session-test") -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1"},
        },
    }


def _tools_list_request() -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def _manager(app):
    route = next(route for route in app.routes if getattr(route, "path", None) == "/mcp")
    return route.endpoint.session_manager


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for MCP lifecycle state")
        await asyncio.sleep(0.005)


async def _surface(transport) -> tuple[dict[str, dict], dict[str, dict]]:
    async with Client(transport, timeout=20) as client:
        tools = await client.list_tools()
        schemas = {tool.name: tool.inputSchema for tool in tools}
        assert set(schemas) == set(_TOOL_ARGS)
        outcomes: dict[str, dict] = {}
        for name in sorted(schemas):
            try:
                result = await client.call_tool(name, _TOOL_ARGS[name])
                outcomes[name] = {"ok": True, "data": result.data}
            except ToolError as exc:
                outcomes[name] = {"ok": False, "error": str(exc)}
        return schemas, outcomes


@pytest.mark.parametrize("mode", ["sessionful", "stateless"])
def test_authenticated_product_mcp_matches_exact_stdio_surface(tmp_path: Path, mode: str) -> None:
    settings, bearer = _settings(tmp_path, mode=mode)
    runtime = host_daemon.DaemonRuntime(shutdown_budget=1.0)
    app = _build_product_application(
        runtime=runtime,
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )

    async def exercise() -> None:
        stdio = StdioTransport(
            command=sys.executable,
            args=["-m", "beadhive.mcp"],
            cwd=str(Path.cwd()),
            env={**os.environ, "OTEL_SDK_DISABLED": "true"},
            keep_alive=False,
            log_file=tmp_path / "stdio.log",
        )

        def httpx_client_factory(**kwargs) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
                **kwargs,
            )

        http = StreamableHttpTransport(
            "http://localhost/mcp",
            headers={"Authorization": f"Bearer {bearer}"},
            httpx_client_factory=httpx_client_factory,
        )
        async with app.router.lifespan_context(app):
            http_surface = await _surface(http)
            assert (http.get_session_id() is not None) is (mode == "sessionful")
            if mode == "stateless":
                assert app.state.credential_sessions.active_session_count == 0
                assert not hasattr(app.state, "mcp_sessions")

        # The daemon lifecycle is additive: stdio starts and serves the exact same registry after
        # the daemon is already unavailable.
        stdio_surface = await _surface(stdio)
        assert stdio_surface == http_surface

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


def test_mcp_methods_do_not_weaken_non_mcp_read_only_routes(tmp_path: Path) -> None:
    settings, bearer = _settings(tmp_path, mode="stateless")
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                response = await client.post(
                    "/api/v1/factory", headers={"Authorization": f"Bearer {bearer}"}
                )
        assert (response.status_code, response.json()["error"]["code"]) == (
            405,
            "read_only_profile",
        )

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


def test_mcp_session_is_owned_by_exact_authenticated_credential_and_principal(
    tmp_path: Path,
) -> None:
    settings, owner_bearer = _settings(tmp_path, mode="sessionful")
    same_principal = daemon_auth.add_credential(
        settings.auth.credential_file,
        credential_id="same-principal-other-credential",
        audience="beadhive-host",
        principal="agent:test",
        scopes=(AuthScope.MCP_CONTROL,),
        expires_at=4_102_444_800,
    )
    other_principal = daemon_auth.add_credential(
        settings.auth.credential_file,
        credential_id="other-principal",
        audience="beadhive-host",
        principal="agent:other",
        scopes=(AuthScope.MCP_CONTROL,),
        expires_at=4_102_444_800,
    )
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                initialized = await client.post(
                    "/mcp", headers=_headers(owner_bearer), json=_initialize_request()
                )
                session_id = initialized.headers["mcp-session-id"]
                accepted = await client.post(
                    "/mcp",
                    headers=_headers(owner_bearer, session_id=session_id),
                    json=_tools_list_request(),
                )
                rejected_same_principal = await client.post(
                    "/mcp",
                    headers=_headers(
                        same_principal.bearer.reveal_for_authority(),
                        session_id=session_id,
                    ),
                    json=_tools_list_request(),
                )
                rejected_other_principal = await client.post(
                    "/mcp",
                    headers=_headers(
                        other_principal.bearer.reveal_for_authority(),
                        session_id=session_id,
                    ),
                    json=_tools_list_request(),
                )

        assert initialized.status_code == accepted.status_code == 200
        assert rejected_same_principal.status_code == 404
        assert rejected_other_principal.status_code == 404
        assert session_id not in rejected_same_principal.text + rejected_other_principal.text

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


def test_live_mcp_session_registers_and_normal_termination_clears_every_owner(
    tmp_path: Path,
) -> None:
    settings, bearer = _settings(tmp_path, mode="sessionful")
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                initialized = await client.post(
                    "/mcp", headers=_headers(bearer), json=_initialize_request()
                )
                session_id = initialized.headers["mcp-session-id"]
                assert app.state.credential_sessions.active_session_count == 1
                assert app.state.mcp_sessions.active_session_count == 1
                assert len(_manager(app)._server_instances) == 1
                assert len(_manager(app)._session_owners) == 1

                terminated = await client.delete(
                    "/mcp", headers=_headers(bearer, session_id=session_id)
                )
                assert terminated.status_code == 200
                assert app.state.credential_sessions.active_session_count == 0
                assert app.state.mcp_sessions.active_session_count == 0
                assert not _manager(app)._server_instances
                assert not _manager(app)._session_owners

        assert app.state.credential_sessions.active_session_count == 0
        assert app.state.mcp_sessions.active_session_count == 0
        assert not _manager(app)._server_instances
        assert not _manager(app)._session_owners

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("rotate", daemon_auth.AuthFailureCode.ROTATED),
        ("revoke", daemon_auth.AuthFailureCode.REVOKED),
    ],
)
def test_mcp_authority_change_closes_only_affected_live_sessions(
    tmp_path: Path,
    mutation: str,
    reason: daemon_auth.AuthFailureCode,
) -> None:
    settings, affected_bearer = _settings(
        tmp_path,
        mode="sessionful",
        revalidation_seconds=0.02,
    )
    unaffected = daemon_auth.add_credential(
        settings.auth.credential_file,
        credential_id="unaffected-client",
        audience="beadhive-host",
        principal="agent:unaffected",
        scopes=(AuthScope.MCP_CONTROL,),
        expires_at=4_102_444_800,
    )
    unaffected_bearer = unaffected.bearer.reveal_for_authority()
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                affected = await client.post(
                    "/mcp", headers=_headers(affected_bearer), json=_initialize_request("affected")
                )
                stable = await client.post(
                    "/mcp",
                    headers=_headers(unaffected_bearer),
                    json=_initialize_request("unaffected"),
                )
                affected_id = affected.headers["mcp-session-id"]
                stable_id = stable.headers["mcp-session-id"]
                replacement_bearer = affected_bearer
                if mutation == "rotate":
                    replacement_bearer = daemon_auth.rotate_credential(
                        settings.auth.credential_file,
                        "mcp-client",
                    ).bearer.reveal_for_authority()
                else:
                    daemon_auth.revoke_credential(settings.auth.credential_file, "mcp-client")

                await _wait_for(lambda: app.state.mcp_sessions.active_session_count == 1)
                await app.state.credential_sessions.drain_close_callbacks()
                stale = await client.post(
                    "/mcp",
                    headers=_headers(replacement_bearer, session_id=affected_id),
                    json=_tools_list_request(),
                )
                still_stable = await client.post(
                    "/mcp",
                    headers=_headers(unaffected_bearer, session_id=stable_id),
                    json=_tools_list_request(),
                )

                assert stale.status_code in ({404} if mutation == "rotate" else {401})
                assert still_stable.status_code == 200
                assert app.state.credential_sessions.active_session_count == 1
                assert app.state.mcp_sessions.active_session_count == 1
                assert app.state.network_admission.active_mcp_session_count == 1
                assert set(_manager(app)._server_instances) == {stable_id}
                assert set(_manager(app)._session_owners) == {stable_id}
                closure = app.state.credential_sessions.closure_records[-1]
                assert (closure.credential_id, closure.principal, closure.reason) == (
                    "mcp-client",
                    "agent:test",
                    reason,
                )
                daemon_auth.assert_redacted(
                    (closure, app.state.mcp_sessions),
                    (affected_bearer, unaffected_bearer, replacement_bearer),
                )

        assert app.state.credential_sessions.active_session_count == 0
        assert app.state.mcp_sessions.active_session_count == 0
        assert app.state.network_admission.active_mcp_session_count == 0
        assert not _manager(app)._server_instances
        assert not _manager(app)._session_owners

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


@pytest.mark.parametrize("expiry", ["idle", "absolute"])
def test_mcp_expiry_terminates_transport_before_max_one_replacement(
    tmp_path: Path,
    expiry: str,
) -> None:
    settings, bearer = _settings(
        tmp_path,
        mode="sessionful",
        revalidation_seconds=0.02,
        max_sessions=1,
        idle_seconds=0.04 if expiry == "idle" else 0.08,
        absolute_seconds=1.0 if expiry == "idle" else 0.12,
    )
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                first = await client.post(
                    "/mcp", headers=_headers(bearer), json=_initialize_request("expiring")
                )
                old_id = first.headers["mcp-session-id"]
                if expiry == "absolute":
                    deadline = asyncio.get_running_loop().time() + 0.14
                    while asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(0.02)
                        touched = await client.post(
                            "/mcp",
                            headers=_headers(bearer, session_id=old_id),
                            json=_tools_list_request(),
                        )
                        if touched.status_code == 404:
                            break

                await _wait_for(lambda: app.state.mcp_sessions.active_session_count == 0)
                assert app.state.credential_sessions.active_session_count == 0
                assert app.state.network_admission.active_mcp_session_count == 0
                assert not _manager(app)._server_instances
                assert not _manager(app)._session_owners

                stale = await client.post(
                    "/mcp",
                    headers=_headers(bearer, session_id=old_id),
                    json=_tools_list_request(),
                )
                assert stale.status_code == 404
                replacement = await client.post(
                    "/mcp", headers=_headers(bearer), json=_initialize_request("replacement")
                )
                assert replacement.status_code == 200
                assert replacement.headers["mcp-session-id"] != old_id
                assert app.state.credential_sessions.active_session_count == 1
                assert app.state.mcp_sessions.active_session_count == 1
                assert app.state.network_admission.active_mcp_session_count == 1
                assert len(_manager(app)._server_instances) == 1
                assert len(_manager(app)._session_owners) == 1

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


def test_concurrent_mcp_initialization_reserves_one_session_without_leaks(
    tmp_path: Path,
) -> None:
    settings, bearer = _settings(tmp_path, mode="sessionful", max_sessions=1)
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                responses = await asyncio.gather(
                    client.post("/mcp", headers=_headers(bearer), json=_initialize_request("one")),
                    client.post("/mcp", headers=_headers(bearer), json=_initialize_request("two")),
                )
                assert sorted(response.status_code for response in responses) == [200, 503]
                accepted = next(response for response in responses if response.status_code == 200)
                assert app.state.credential_sessions.active_session_count == 1
                assert app.state.mcp_sessions.active_session_count == 1
                assert app.state.network_admission.active_mcp_session_count == 1
                assert len(_manager(app)._server_instances) == 1
                terminated = await client.delete(
                    "/mcp",
                    headers=_headers(bearer, session_id=accepted.headers["mcp-session-id"]),
                )
                assert terminated.status_code == 200
                assert app.state.credential_sessions.active_session_count == 0
                assert app.state.mcp_sessions.active_session_count == 0
                assert app.state.network_admission.active_mcp_session_count == 0
                assert not _manager(app)._server_instances
                assert not _manager(app)._session_owners

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


def test_cancelled_pre_response_mcp_initialization_releases_every_session_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings, bearer = _settings(tmp_path, mode="sessionful", max_sessions=1)
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )
    created = asyncio.Event()
    never = asyncio.Event()
    original_handle_request = StreamableHTTPServerTransport.handle_request

    async def blocked_before_response(self, scope, receive, send) -> None:
        if self.mcp_session_id is not None and not any(
            name.lower() == b"mcp-session-id" for name, _value in scope.get("headers", ())
        ):
            created.set()
            await never.wait()
        await original_handle_request(self, scope, receive, send)

    monkeypatch.setattr(
        StreamableHTTPServerTransport,
        "handle_request",
        blocked_before_response,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                request = asyncio.create_task(
                    client.post(
                        "/mcp",
                        headers=_headers(bearer),
                        json=_initialize_request("cancelled"),
                    )
                )
                await asyncio.wait_for(created.wait(), timeout=1.0)
                assert len(_manager(app)._server_instances) == 1
                assert len(_manager(app)._session_owners) == 1
                assert app.state.mcp_sessions.active_session_count == 0
                assert app.state.credential_sessions.active_session_count == 0
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request

                assert app.state.credential_sessions.active_session_count == 0
                assert app.state.mcp_sessions.active_session_count == 0
                assert app.state.network_admission.active_mcp_session_count == 0
                assert not _manager(app)._server_instances
                assert not _manager(app)._session_owners

                monkeypatch.setattr(
                    StreamableHTTPServerTransport,
                    "handle_request",
                    original_handle_request,
                )
                replacement = await client.post(
                    "/mcp", headers=_headers(bearer), json=_initialize_request("replacement")
                )
                assert replacement.status_code == 200

        assert app.state.credential_sessions.active_session_count == 0
        assert app.state.mcp_sessions.active_session_count == 0
        assert app.state.network_admission.active_mcp_session_count == 0
        assert not _manager(app)._server_instances
        assert not _manager(app)._session_owners

    try:
        asyncio.run(asyncio.wait_for(exercise(), timeout=2.0))
    finally:
        app.state.operator_sources.close()
    assert bearer not in caplog.text


@pytest.mark.parametrize(
    "failure",
    ["exception", "no-response", "malformed-response", "incomplete-response"],
)
def test_failed_pre_response_mcp_initialization_releases_every_session_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    settings, bearer = _settings(tmp_path, mode="sessionful", max_sessions=1)
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )
    original_handle_request = StreamableHTTPServerTransport.handle_request

    async def fail_before_session_header(self, _scope, _receive, send) -> None:
        if failure == "exception":
            raise RuntimeError("checked downstream failure")
        if failure == "malformed-response":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
        if failure == "incomplete-response":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-length", b"0"),
                        (b"mcp-session-id", self.mcp_session_id.encode()),
                    ],
                }
            )

    monkeypatch.setattr(
        StreamableHTTPServerTransport,
        "handle_request",
        fail_before_session_header,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                if failure == "malformed-response":
                    response = await client.post(
                        "/mcp", headers=_headers(bearer), json=_initialize_request("failed")
                    )
                    assert response.status_code == 200
                else:
                    expected = RuntimeError if failure == "exception" else AssertionError
                    with pytest.raises(expected) as caught:
                        await client.post(
                            "/mcp", headers=_headers(bearer), json=_initialize_request("failed")
                        )
                    assert bearer not in repr(caught.value)

                assert app.state.credential_sessions.active_session_count == 0
                assert app.state.mcp_sessions.active_session_count == 0
                assert app.state.network_admission.active_mcp_session_count == 0
                assert not app.state.network_admission._mcp_reservations
                assert not _manager(app)._server_instances
                assert not _manager(app)._session_owners

                monkeypatch.setattr(
                    StreamableHTTPServerTransport,
                    "handle_request",
                    original_handle_request,
                )
                replacement = await client.post(
                    "/mcp", headers=_headers(bearer), json=_initialize_request("replacement")
                )
                assert replacement.status_code == 200
                assert len(_manager(app)._server_instances) == 1
                assert len(_manager(app)._session_owners) == 1

        assert app.state.credential_sessions.active_session_count == 0
        assert app.state.mcp_sessions.active_session_count == 0
        assert app.state.network_admission.active_mcp_session_count == 0
        assert not app.state.network_admission._mcp_reservations
        assert not _manager(app)._server_instances
        assert not _manager(app)._session_owners

    try:
        asyncio.run(asyncio.wait_for(exercise(), timeout=2.0))
    finally:
        app.state.operator_sources.close()
    assert bearer not in caplog.text


def test_failed_pre_response_initialization_does_not_disturb_existing_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, bearer = _settings(tmp_path, mode="sessionful", max_sessions=2)
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )
    original_handle_request = StreamableHTTPServerTransport.handle_request

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                stable = await client.post(
                    "/mcp", headers=_headers(bearer), json=_initialize_request("stable")
                )
                stable_id = stable.headers["mcp-session-id"]

                async def fail_new_initialization(self, scope, receive, send) -> None:
                    if not any(
                        name.lower() == b"mcp-session-id"
                        for name, _value in scope.get("headers", ())
                    ):
                        raise RuntimeError("new initialization failed")
                    await original_handle_request(self, scope, receive, send)

                monkeypatch.setattr(
                    StreamableHTTPServerTransport,
                    "handle_request",
                    fail_new_initialization,
                )
                with pytest.raises(RuntimeError):
                    await client.post(
                        "/mcp", headers=_headers(bearer), json=_initialize_request("failed")
                    )

                assert app.state.credential_sessions.active_session_count == 1
                assert app.state.mcp_sessions.active_session_count == 1
                assert app.state.network_admission.active_mcp_session_count == 1
                assert set(_manager(app)._server_instances) == {stable_id}
                assert set(_manager(app)._session_owners) == {stable_id}
                stable_response = await client.post(
                    "/mcp",
                    headers=_headers(bearer, session_id=stable_id),
                    json=_tools_list_request(),
                )
                assert stable_response.status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


def test_sessionful_restart_rejects_an_old_active_session(tmp_path: Path) -> None:
    settings, bearer = _settings(tmp_path, mode="sessionful")
    request_headers = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "restart-test", "version": "1"},
        },
    }

    async def exercise() -> None:
        first = _build_product_application(
            runtime=host_daemon.DaemonRuntime(),
            control_record=_record(tmp_path),
            cfg={"managed_repos": []},
            settings=settings,
        )
        try:
            async with first.router.lifespan_context(first):
                transport = httpx.ASGITransport(app=first, client=("127.0.0.1", 5000))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost"
                ) as client:
                    initialized = await client.post(
                        "/mcp", headers=request_headers, json=initialize
                    )
            assert initialized.status_code == 200
            old_session = initialized.headers["mcp-session-id"]
            assert ("fastmcp-http", "completed") in {
                (result.name, result.status)
                for result in first.state.daemon_runtime.shutdown_results
            }
            assert first.state.credential_sessions.active_session_count == 0
            assert first.state.mcp_sessions.active_session_count == 0
            assert first.state.network_admission.active_mcp_session_count == 0
            assert not _manager(first)._server_instances
            assert not _manager(first)._session_owners
        finally:
            first.state.operator_sources.close()

        second = _build_product_application(
            runtime=host_daemon.DaemonRuntime(),
            control_record=_record(tmp_path),
            cfg={"managed_repos": []},
            settings=settings,
        )
        try:
            async with second.router.lifespan_context(second):
                transport = httpx.ASGITransport(app=second, client=("127.0.0.1", 5001))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost"
                ) as client:
                    stale = await client.post(
                        "/mcp",
                        headers={**request_headers, "Mcp-Session-Id": old_session},
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    )
            assert stale.status_code == 404
            assert old_session not in stale.text
            assert second.state.credential_sessions.active_session_count == 0
            assert second.state.mcp_sessions.active_session_count == 0
            assert second.state.network_admission.active_mcp_session_count == 0
            assert not _manager(second)._server_instances
            assert not _manager(second)._session_owners
        finally:
            second.state.operator_sources.close()

    asyncio.run(exercise())


def test_product_mcp_session_exhaustion_is_bounded_and_recoverable(tmp_path: Path) -> None:
    settings, bearer = _settings(tmp_path, mode="sessionful")
    settings = settings.model_copy(
        update={"mcp": settings.mcp.model_copy(update={"max_sessions": 1})}
    )
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "limit-test", "version": "1"},
        },
    }

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                first = await client.post("/mcp", headers=headers, json=initialize)
                exhausted = await client.post("/mcp", headers=headers, json=initialize)
                assert first.status_code == 200
                assert (exhausted.status_code, exhausted.json()["error"]["code"]) == (
                    503,
                    "session_limit_reached",
                )

                released = await client.delete(
                    "/mcp",
                    headers={**headers, "Mcp-Session-Id": first.headers["mcp-session-id"]},
                )
                assert released.status_code == 200
                replacement = await client.post("/mcp", headers=headers, json=initialize)
                assert replacement.status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


def test_mcp_mutation_rereads_bearer_authority_before_tool_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, bearer = _settings(tmp_path, mode="sessionful")
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=_record(tmp_path),
        cfg={"managed_repos": []},
        settings=settings,
    )

    def forbidden_mutation(*_args, **_kwargs):
        pytest.fail("revoked authority reached the MCP mutation body")

    monkeypatch.setattr(config_mod, "set_value", forbidden_mutation)

    async def exercise() -> None:
        def httpx_client_factory(**kwargs) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
                **kwargs,
            )

        transport = StreamableHttpTransport(
            "http://localhost/mcp",
            headers={"Authorization": f"Bearer {bearer}"},
            httpx_client_factory=httpx_client_factory,
        )
        async with app.router.lifespan_context(app):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                async with Client(transport) as client:
                    assert {tool.name for tool in await client.list_tools()} == set(_TOOL_ARGS)
                    daemon_auth.revoke_credential(settings.auth.credential_file, "mcp-client")
                    await client.call_tool("config_set", {"key": "otel.protocol", "value": "grpc"})
            assert exc_info.value.response.status_code == 401

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()
