"""Focused contract tests for the product host-daemon core (bh-76a7z.1)."""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.routing import Route
from typer.testing import CliRunner

from beadhive import daemon_state_broker, host_daemon
from beadhive.cli import app as cli_app
from beadhive.daemon_config import HostDaemonConfig

runner = CliRunner()


def _key(tmp_path, *, host_id: str = "host-1") -> host_daemon.DaemonKey:
    return host_daemon.DaemonKey(
        account_id="uid:1234", bh_home=str(tmp_path.resolve()), host_id=host_id
    )


def _route_paths(app) -> set[str]:
    return {route.path for route in app.routes if hasattr(route, "path")}


def _settings(tmp_path, **overrides) -> HostDaemonConfig:
    credential = tmp_path / "daemon-verifiers.json"
    credential.write_text('{"keys": []}\n')
    credential.chmod(0o600)
    values = {
        "enabled": True,
        "auth": {"credential_file": credential.absolute()},
    }
    values.update(overrides)
    return HostDaemonConfig(**values)


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
        assert payload["authentication"] == singleton.record.authentication
        assert len(payload["authentication"]) == 64
        assert singleton.paths.control.stat().st_mode & 0o777 == 0o600
        assert singleton.paths.lock.stat().st_mode & 0o777 == 0o600

        status = host_daemon.daemon_status(key)
        assert (status.state, status.running, status.verified) == ("running", True, True)
        assert status.record == singleton.record

        with pytest.raises(host_daemon.AlreadyRunningError, match="already held"):
            host_daemon.DaemonSingleton.acquire(key, listener_host="127.0.0.1", listener_port=8421)
    finally:
        singleton.release()

    assert not singleton.paths.control.exists()
    assert host_daemon.daemon_status(key).state == "stopped"


def test_control_record_authentication_rejects_tampering(tmp_path):
    key = _key(tmp_path)
    singleton = host_daemon.DaemonSingleton.acquire(
        key, listener_host="127.0.0.1", listener_port=8420
    )
    try:
        payload = json.loads(singleton.paths.control.read_text())
        payload["listener_port"] = 65535
        singleton.paths.control.write_text(json.dumps(payload))

        status = host_daemon.daemon_status(key)
        assert status.state == "unverified"
        assert not status.verified
        assert "authentication" in status.detail
    finally:
        singleton.release()


def test_control_record_verification_refuses_a_missing_or_corrupt_key(tmp_path):
    key = _key(tmp_path)
    singleton = host_daemon.DaemonSingleton.acquire(
        key, listener_host="127.0.0.1", listener_port=8420
    )
    try:
        singleton.paths.lock.write_bytes(b"not-a-valid-verification-key")
        status = host_daemon.daemon_status(key)
        assert status.state == "unverified"
        assert not status.verified
        assert "verification key" in status.detail
    finally:
        # The held descriptor still owns the original key and lock; release remains finite.
        singleton.release()


def test_stale_record_is_replaced_without_touching_its_unrelated_pid(tmp_path, monkeypatch):
    key = _key(tmp_path)
    paths = host_daemon.DaemonPaths.for_key(key)
    paths.directory.mkdir(parents=True)
    stale = host_daemon.ControlRecord.create(
        key,
        listener_host="127.0.0.1",
        listener_port=8420,
        verification_key=b"s" * 32,
    )
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
        host_daemon._write_control(
            singleton.paths.control, wrong_host.authenticated(singleton.verification_key)
        )
        status = host_daemon.daemon_status(key)
        assert status.state == "unverified"
        assert "identity" in status.detail

        wrong_process = replace(singleton.record, process_start="linux:not-this-process")
        host_daemon._write_control(
            singleton.paths.control, wrong_process.authenticated(singleton.verification_key)
        )
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
                    "schemaVersion": 1,
                    "status": "stopping",
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


def test_daemon_phase_order_maps_to_the_shared_host_lifecycle_without_reordering():
    from beadhive.kernel.lifecycle import HostLifecyclePhase

    assert [phase.value for phase in host_daemon.StartupPhase] == [10, 20, 30]
    assert {phase.lifecycle_phase for phase in host_daemon.StartupPhase} == {
        HostLifecyclePhase.STARTUP
    }
    assert [phase.lifecycle_phase for phase in host_daemon.ShutdownPhase] == [
        HostLifecyclePhase.DRAIN,
        HostLifecyclePhase.DRAIN,
        HostLifecyclePhase.SHUTDOWN,
        HostLifecyclePhase.SHUTDOWN,
        HostLifecyclePhase.SHUTDOWN,
        HostLifecyclePhase.TELEMETRY_FLUSH,
    ]


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


def test_partial_startup_cleans_entered_components_in_reverse_order():
    events: list[str] = []
    runtime = host_daemon.DaemonRuntime(shutdown_budget=1.0)

    @asynccontextmanager
    async def first(_app):
        events.append("first:start")
        try:
            yield
        finally:
            events.append("first:stop")

    @asynccontextmanager
    async def second(_app):
        events.append("second:start")
        raise RuntimeError("partial startup")
        yield  # pragma: no cover

    app = host_daemon.build_application(
        runtime=runtime,
        components=[
            host_daemon.LifespanComponent("first", first),
            host_daemon.LifespanComponent("second", second),
        ],
    )

    async def exercise():
        with pytest.raises(RuntimeError, match="partial startup"):
            async with app.router.lifespan_context(app):
                pass

    asyncio.run(exercise())
    assert events == ["first:start", "second:start", "first:stop"]
    assert [(result.name, result.status) for result in runtime.shutdown_results] == [
        ("first", "completed")
    ]


def test_outer_lifespan_cancellation_closes_components_in_reverse_order():
    events: list[str] = []
    runtime = host_daemon.DaemonRuntime(shutdown_budget=1.0)

    def component(name):
        @asynccontextmanager
        async def lifespan(_app):
            events.append(f"{name}:start")
            try:
                yield
            finally:
                events.append(f"{name}:stop")

        return host_daemon.LifespanComponent(name, lifespan)

    app = host_daemon.build_application(
        runtime=runtime, components=[component("first"), component("second")]
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exercise())
    assert events == ["first:start", "second:start", "second:stop", "first:stop"]
    assert [result.status for result in runtime.shutdown_results] == ["completed", "completed"]


def test_cancelled_component_exit_finishes_reverse_cleanup_before_propagating():
    events: list[str] = []
    runtime = host_daemon.DaemonRuntime(shutdown_budget=1.0)

    @asynccontextmanager
    async def stable(_app):
        events.append("stable:start")
        try:
            yield
        finally:
            events.append("stable:stop")

    @asynccontextmanager
    async def cancelled(_app):
        events.append("cancelled:start")
        try:
            yield
        finally:
            events.append("cancelled:stop")
            raise asyncio.CancelledError

    app = host_daemon.build_application(
        runtime=runtime,
        components=[
            host_daemon.LifespanComponent("stable", stable),
            host_daemon.LifespanComponent("cancelled", cancelled),
        ],
    )

    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            async with app.router.lifespan_context(app):
                pass
        # Assert inside the running loop: ``stable`` must have been closed by the daemon's
        # bounded shutdown, not later by ``asyncio.run`` finalizing abandoned generators.
        assert events == [
            "stable:start",
            "cancelled:start",
            "cancelled:stop",
            "stable:stop",
        ]
        assert [(result.name, result.status) for result in runtime.shutdown_results] == [
            ("cancelled", "cancelled"),
            ("stable", "completed"),
        ]

    asyncio.run(exercise())


def test_cancellation_still_obeys_one_finite_shutdown_budget():
    events: list[str] = []
    runtime = host_daemon.DaemonRuntime(shutdown_budget=0.02)

    async def cancelled():
        events.append("cancelled")
        raise asyncio.CancelledError

    async def slow():
        events.append("slow:start")
        try:
            await asyncio.sleep(10)
        finally:
            events.append("slow:stop")

    async def later():
        events.append("later")

    runtime.register_drain(host_daemon.ShutdownPhase.CLOSE_SESSIONS, "cancelled", cancelled)
    runtime.register_drain(host_daemon.ShutdownPhase.CLOSE_RESOURCES, "slow", slow)
    runtime.register_drain(host_daemon.ShutdownPhase.FLUSH_TELEMETRY, "later", later)

    async def exercise():
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await runtime.shutdown()
        assert time.monotonic() - started < 0.2
        assert events == ["cancelled", "slow:start", "slow:stop"]
        assert [(result.name, result.status) for result in runtime.shutdown_results] == [
            ("cancelled", "cancelled"),
            ("slow", "timed_out"),
            ("later", "skipped_budget_exhausted"),
        ]

    asyncio.run(exercise())


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
    settings = _settings(
        tmp_path,
        bind="127.0.0.1",
        port=9000,
        http={"max_connections": 17},
        shutdown={
            "graceful_seconds": 0.5,
            "request_drain_seconds": 0.25,
            "telemetry_flush_seconds": 0.1,
        },
    )

    monkeypatch.setattr(host_daemon.DaemonKey, "current", classmethod(lambda _cls: key))

    import uvicorn

    def interrupted(app, **kwargs):
        status = host_daemon.daemon_status(key)
        assert status.verified
        assert status.record.listener_port == 9000
        assert "/mcp" in _route_paths(app)
        observed.update(kwargs)
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(uvicorn, "run", interrupted)

    def terminated(*_args):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminated)
    try:
        with pytest.raises(KeyboardInterrupt):
            host_daemon.serve(
                settings=settings,
                state_broker_factory=daemon_state_broker.DaemonStateBroker.for_host,
            )
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 9000
    assert observed["timeout_graceful_shutdown"] == 0.5
    assert observed["limit_concurrency"] == 17
    assert host_daemon.daemon_status(key).state == "stopped"


def test_partial_application_startup_releases_singleton(tmp_path, monkeypatch):
    key = _key(tmp_path)
    settings = _settings(tmp_path)
    monkeypatch.setattr(host_daemon.DaemonKey, "current", classmethod(lambda _cls: key))
    monkeypatch.setattr(
        host_daemon,
        "build_product_application",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("application failed")),
    )

    with pytest.raises(RuntimeError, match="application failed"):
        host_daemon.serve(
            settings=settings,
            state_broker_factory=daemon_state_broker.DaemonStateBroker.for_host,
        )

    assert host_daemon.daemon_status(key).state == "stopped"


def test_installed_daemon_commands_render_verified_status_and_run_foreground(monkeypatch):
    from beadhive import daemon_supervisor

    key = host_daemon.DaemonKey(account_id="uid:1234", bh_home="/tmp/example-bh", host_id="host-1")
    status = host_daemon.DaemonStatus(
        state="running",
        running=True,
        verified=True,
        detail="verified",
        key=key,
    )
    monkeypatch.setattr(host_daemon.DaemonKey, "current", classmethod(lambda _cls: key))
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: status)
    monkeypatch.setattr(
        daemon_supervisor,
        "get_supervisor_backend",
        lambda: daemon_supervisor.RecordingSupervisorBackend(),
    )

    result = runner.invoke(cli_app, ["host", "daemon", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "running"
    assert payload["control_record"]["verified"] is True
    assert payload["readiness"]["state"] == "unavailable"

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
    assert len(calls) == 1
    assert calls[0].pop("state_broker_factory") == daemon_state_broker.DaemonStateBroker.for_host
    assert calls == [{"listener_host": "::1", "listener_port": 9001, "shutdown_budget": 2.5}]


def test_daemon_entrypoint_is_installed_and_ordinary_imports_do_not_load_runtime():
    pyproject = Path("pyproject.toml").read_text()
    assert 'bh-host-daemon = "beadhive.bootstrap.host:main"' in pyproject

    probe = (
        "import sys; import beadhive.cli; import beadhive.mcp; "
        "assert 'beadhive.host_daemon' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr
