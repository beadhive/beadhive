"""Focused contract tests for the product host-daemon core (bh-76a7z.1)."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.routing import Route
from typer.testing import CliRunner

from beadhive import host_daemon
from beadhive.cli import app as cli_app

runner = CliRunner()


def _key(tmp_path, *, host_id: str = "host-1") -> host_daemon.DaemonKey:
    return host_daemon.DaemonKey(
        account_id="uid:1234", bh_home=str(tmp_path.resolve()), host_id=host_id
    )


def _route_paths(app) -> set[str]:
    return {route.path for route in app.routes if hasattr(route, "path")}


def test_singleton_record_is_verified_and_second_owner_is_refused(tmp_path):
    key = _key(tmp_path)
    singleton = host_daemon.DaemonSingleton.acquire(
        key, listener_host="127.0.0.1", listener_port=8420
    )
    try:
        payload = json.loads(singleton.paths.control.read_text())
        assert payload["bh_home"] == str(tmp_path.resolve())
        assert payload["host_id"] == "host-1"
        assert payload["instance_id"] == singleton.record.instance_id
        assert payload["process_start"] != "unavailable"
        assert singleton.paths.control.stat().st_mode & 0o777 == 0o600

        status = host_daemon.daemon_status(key)
        assert (status.state, status.running, status.verified) == ("running", True, True)
        assert status.record == singleton.record

        with pytest.raises(host_daemon.AlreadyRunningError, match="already held"):
            host_daemon.DaemonSingleton.acquire(key, listener_host="127.0.0.1", listener_port=8421)
    finally:
        singleton.release()

    assert not singleton.paths.control.exists()
    assert host_daemon.daemon_status(key).state == "stopped"


def test_stale_record_is_replaced_without_touching_its_unrelated_pid(tmp_path, monkeypatch):
    key = _key(tmp_path)
    paths = host_daemon.DaemonPaths.for_key(key)
    paths.directory.mkdir(parents=True)
    stale = host_daemon.ControlRecord.create(key, listener_host="127.0.0.1", listener_port=8420)
    stale = replace(stale, pid=999_999, process_start="linux:old")
    host_daemon._write_control(paths.control, stale)

    # A stale record is only data.  Reclamation takes the named flock and never signals or
    # adopts the PID written by an earlier incarnation.
    monkeypatch.setattr(
        host_daemon.os,
        "kill",
        lambda *_args: pytest.fail("stale-record handling must not signal a PID"),
        raising=False,
    )
    assert host_daemon.daemon_status(key).state == "stale"

    singleton = host_daemon.DaemonSingleton.acquire(
        key, listener_host="127.0.0.1", listener_port=8421
    )
    try:
        assert singleton.record.instance_id != stale.instance_id
        assert singleton.record.pid == host_daemon.os.getpid()
    finally:
        singleton.release()


def test_status_rejects_wrong_host_and_pid_incarnation(tmp_path):
    key = _key(tmp_path)
    singleton = host_daemon.DaemonSingleton.acquire(
        key, listener_host="127.0.0.1", listener_port=8420
    )
    try:
        wrong_host = replace(singleton.record, host_id="another-host")
        host_daemon._write_control(singleton.paths.control, wrong_host)
        status = host_daemon.daemon_status(key)
        assert status.state == "unverified"
        assert "identity" in status.detail

        wrong_process = replace(singleton.record, process_start="linux:not-this-process")
        host_daemon._write_control(singleton.paths.control, wrong_process)
        status = host_daemon.daemon_status(key)
        assert status.state == "unverified"
        assert "PID incarnation" in status.detail
    finally:
        # Restore this lease's identity so release removes its own record.
        host_daemon._write_control(singleton.paths.control, singleton.record)
        singleton.release()


def test_outer_lifespan_orders_startup_and_bounded_drain_and_closes_admission():
    events: list[str] = []
    runtime = host_daemon.DaemonRuntime(shutdown_budget=1.0)

    async def telemetry_start():
        events.append("start:telemetry")

    async def resources_start():
        events.append("start:resources")

    async def reject_new():
        assert not runtime.ready
        assert not runtime.accepting
        events.append("stop:reject")

    async def close_sessions():
        events.append("stop:sessions")

    async def close_resources():
        events.append("stop:resources")

    async def flush_telemetry():
        events.append("stop:telemetry")

    # Registration order differs from phase order on purpose.
    runtime.register_startup(host_daemon.StartupPhase.RESOURCES, "resources", resources_start)
    runtime.register_startup(host_daemon.StartupPhase.TELEMETRY, "telemetry", telemetry_start)
    runtime.register_drain(host_daemon.ShutdownPhase.FLUSH_TELEMETRY, "telemetry", flush_telemetry)
    runtime.register_drain(host_daemon.ShutdownPhase.CLOSE_RESOURCES, "resources", close_resources)
    runtime.register_drain(host_daemon.ShutdownPhase.REJECT_NEW_WORK, "admission", reject_new)
    runtime.register_drain(host_daemon.ShutdownPhase.CLOSE_SESSIONS, "sessions", close_sessions)

    @asynccontextmanager
    async def component_lifespan(_app):
        events.append("start:component")
        yield
        events.append("stop:component")

    async def work(_request):
        return JSONResponse({"ok": True})

    app = host_daemon.build_application(
        runtime=runtime,
        routes=[Route("/work", work)],
        components=[
            host_daemon.LifespanComponent(
                "component",
                component_lifespan,
                shutdown_phase=host_daemon.ShutdownPhase.CLOSE_RESOURCES,
            )
        ],
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            assert app.state.daemon_runtime is runtime
            assert runtime.ready and runtime.accepting
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://daemon") as client:
                assert (await client.get("/work")).status_code == 200
                runtime.begin_shutdown()
                assert (await client.get("/work")).status_code == 503
                health = await client.get("/health")
                assert health.status_code == 200
                assert health.json() == {
                    "live": True,
                    "ready": False,
                    "contract": host_daemon.CONTRACT_VERSION,
                }

    asyncio.run(exercise())
    assert events == [
        "start:telemetry",
        "start:resources",
        "start:component",
        "stop:reject",
        "stop:sessions",
        "stop:resources",
        "stop:component",
        "stop:telemetry",
    ]
    assert all(result.status == "completed" for result in runtime.shutdown_results)


def test_one_shutdown_deadline_cancels_a_slow_owner_and_skips_later_work():
    runtime = host_daemon.DaemonRuntime(shutdown_budget=0.02)
    later_called = False

    async def slow():
        await asyncio.sleep(10)

    async def later():
        nonlocal later_called
        later_called = True

    runtime.register_drain(host_daemon.ShutdownPhase.DRAIN_IN_FLIGHT, "slow", slow)
    runtime.register_drain(host_daemon.ShutdownPhase.CLOSE_RESOURCES, "later", later)
    started = time.monotonic()
    results = asyncio.run(runtime.shutdown())

    assert time.monotonic() - started < 0.2
    assert not later_called
    assert [(item.name, item.status) for item in results] == [
        ("slow", "timed_out"),
        ("later", "skipped_budget_exhausted"),
    ]


def test_mcp_lifespan_can_be_composed_but_phase_one_cannot_expose_mcp():
    factory_calls = 0
    events: list[str] = []

    class FakeServer:
        def http_app(self, **kwargs):
            assert kwargs == {
                "path": "/mcp",
                "transport": "streamable-http",
                "stateless_http": False,
                "json_response": False,
            }

            @asynccontextmanager
            async def lifespan(_app):
                events.append("mcp:start")
                yield
                events.append("mcp:stop")

            async def endpoint(_request):
                return JSONResponse({"mcp": True})

            class App:
                routes = [Route("/mcp", endpoint)]

            result = App()
            result.lifespan = lifespan
            return result

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return FakeServer()

    phase_one = host_daemon.build_application(mcp_server_factory=factory)
    assert factory_calls == 0
    assert "/mcp" not in _route_paths(phase_one)

    enabled = host_daemon.build_application(enable_mcp_http=True, mcp_server_factory=factory)
    assert factory_calls == 1
    assert _route_paths(enabled) == {"/health", "/mcp"}

    async def exercise():
        async with enabled.router.lifespan_context(enabled):
            assert events == ["mcp:start"]

    asyncio.run(exercise())
    assert events == ["mcp:start", "mcp:stop"]


def test_real_fastmcp_http_app_uses_the_outer_lifespan():
    app = host_daemon.build_application(enable_mcp_http=True)
    assert "/mcp" in _route_paths(app)

    async def exercise():
        async with app.router.lifespan_context(app):
            assert app.state.daemon_runtime.ready

    asyncio.run(exercise())
    assert not app.state.daemon_runtime.ready
    assert [result.name for result in app.state.daemon_runtime.shutdown_results] == ["fastmcp-http"]


@pytest.mark.parametrize("listener_host", ["0.0.0.0", "192.0.2.1", "localhost"])
def test_phase_one_refuses_every_nonliteral_or_nonloopback_listener(listener_host):
    with pytest.raises(host_daemon.ListenerConfigurationError):
        host_daemon.validate_listener(listener_host, 8420)


def test_serve_acquires_before_uvicorn_and_releases_on_signal(tmp_path, monkeypatch):
    key = _key(tmp_path)
    observed = {}

    monkeypatch.setattr(host_daemon.DaemonKey, "current", classmethod(lambda _cls: key))

    import uvicorn

    def interrupted(app, **kwargs):
        status = host_daemon.daemon_status(key)
        assert status.verified
        assert status.record.listener_port == 9000
        assert "/mcp" not in _route_paths(app)
        observed.update(kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        host_daemon.serve(listener_host="127.0.0.1", listener_port=9000, shutdown_budget=0.5)

    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 9000
    assert observed["timeout_graceful_shutdown"] == 0.5
    assert host_daemon.daemon_status(key).state == "stopped"


def test_installed_daemon_commands_render_verified_status_and_run_foreground(monkeypatch):
    key = host_daemon.DaemonKey(account_id="uid:1234", bh_home="/tmp/example-bh", host_id="host-1")
    status = host_daemon.DaemonStatus(
        state="running",
        running=True,
        verified=True,
        detail="verified",
        key=key,
    )
    monkeypatch.setattr(host_daemon, "daemon_status", lambda: status)

    result = runner.invoke(cli_app, ["host", "daemon", "status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == status.payload()

    calls = []
    monkeypatch.setattr(host_daemon, "serve", lambda **kwargs: calls.append(kwargs))
    result = runner.invoke(
        cli_app,
        [
            "host",
            "daemon",
            "serve",
            "--host",
            "::1",
            "--port",
            "9001",
            "--shutdown-budget",
            "2.5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [{"listener_host": "::1", "listener_port": 9001, "shutdown_budget": 2.5}]
