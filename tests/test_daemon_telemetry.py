"""Daemon-scoped telemetry identity, RED metrics, gauges, and bounded flush."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from typer.testing import CliRunner

from beadhive import cli, daemon_supervisor, daemon_telemetry, host_daemon, otel
from beadhive.kernel.telemetry import (
    ErrorClassification,
    EventIdentity,
    EventPhase,
    Outcome,
    RecordingTelemetrySink,
    SemanticEventName,
    SemanticTelemetry,
)


class _Instrument:
    def __init__(self) -> None:
        self.values: list[tuple[float, dict[str, object]]] = []

    def add(self, value, attributes=None) -> None:
        self.values.append((value, dict(attributes or {})))

    def record(self, value, attributes=None) -> None:
        self.values.append((value, dict(attributes or {})))


class _Meter:
    def __init__(self) -> None:
        self.instruments: dict[str, _Instrument] = {}
        self.callbacks: dict[str, object] = {}

    def create_counter(self, name, **_kwargs):
        return self.instruments.setdefault(name, _Instrument())

    def create_histogram(self, name, **_kwargs):
        return self.instruments.setdefault(name, _Instrument())

    def create_observable_gauge(self, name, *, callbacks, **_kwargs):
        self.callbacks[name] = callbacks[0]
        return self.instruments.setdefault(name, _Instrument())


@dataclass
class _Flush:
    status: str
    duration_seconds: float = 0.01


def test_supported_foreground_entrypoint_defers_generic_otel_to_daemon_lifespan(
    monkeypatch,
) -> None:
    meter = _Meter()
    init_services = []
    daemon_started = []
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)

    def init(_cfg, *, service_name="bh", **_kwargs):
        init_services.append(service_name)
        return True

    def shutdown(*, before_close=None, **_kwargs):
        result = _Flush("completed")
        if before_close is not None:
            before_close(result)
        return result

    def serve(**_kwargs):
        telemetry = daemon_telemetry.DaemonTelemetry(
            cfg={"otel": {"enabled": True}},
            host_id="host-stable",
            instance_id="instance-one",
            flush_budget_seconds=0.2,
        )

        async def lifespan() -> None:
            daemon_started.append(await telemetry.start())
            await telemetry.stop()

        asyncio.run(lifespan())

    monkeypatch.setattr(otel, "init", init)
    monkeypatch.setattr(otel, "shutdown", shutdown)
    monkeypatch.setattr(host_daemon, "serve", serve)

    result = CliRunner().invoke(cli.app, ["host", "daemon", "serve"])

    assert result.exit_code == 0, result.output
    assert daemon_started == [True]
    assert init_services == ["bh-host-daemon"]

    init_services.clear()

    def unavailable():
        raise FileNotFoundError("no daemon")

    monkeypatch.setattr(daemon_supervisor, "daemon_service_status", unavailable)
    ordinary = CliRunner().invoke(cli.app, ["host", "daemon", "status"])
    assert ordinary.exit_code == 1
    assert init_services == ["bh"]


def test_daemon_lifespan_has_distinct_stable_identity_and_one_bounded_flush(monkeypatch) -> None:
    meter = _Meter()
    init_calls = []
    shutdown_calls = []
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)
    monkeypatch.setattr(
        otel,
        "init",
        lambda cfg, **kwargs: init_calls.append((cfg, kwargs)) or True,
    )

    def shutdown(**kwargs):
        shutdown_calls.append(kwargs)
        result = _Flush("completed")
        kwargs["before_close"](result)
        return result

    monkeypatch.setattr(otel, "shutdown", shutdown)

    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": True}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
    )

    async def exercise() -> None:
        assert await telemetry.start() is True
        assert await telemetry.start() is False
        await telemetry.stop()
        await telemetry.stop()

    asyncio.run(exercise())

    assert init_calls == [
        (
            {"otel": {"enabled": True}},
            {
                "service_name": "bh-host-daemon",
                "resource_attributes": {
                    "bh.host.id": "host-stable",
                    "service.instance.id": "instance-one",
                },
                "enrich_resource": False,
                "shutdown_timeout_seconds": 0.2,
                "register_atexit": False,
            },
        )
    ]
    assert len(shutdown_calls) == 1
    assert shutdown_calls[0]["timeout_seconds"] == 0.2
    assert callable(shutdown_calls[0]["before_close"])
    assert set(meter.instruments) == {
        "bh.daemon.backpressure",
        "bh.daemon.cancellations",
        "bh.daemon.connections",
        "bh.daemon.connections.active",
        "bh.daemon.dependency.duration",
        "bh.daemon.dependency.probes",
        "bh.daemon.errors",
        "bh.daemon.lifecycle",
        "bh.daemon.queue.depth",
        "bh.daemon.replay.gaps",
        "bh.daemon.request.duration",
        "bh.daemon.requests",
        "bh.daemon.requests.active",
        "bh.daemon.resets",
        "bh.daemon.restarts",
        "bh.daemon.shutdown.duration",
        "bh.daemon.telemetry.flush",
        "bh.daemon.uptime",
    }
    assert meter.instruments["bh.daemon.restarts"].values == [(1, {})]
    assert meter.instruments["bh.daemon.telemetry.flush"].values[-1][1] == {
        "bh.daemon.flush.outcome": "completed"
    }


def test_shutdown_summary_is_emitted_while_provider_can_still_export(monkeypatch) -> None:
    accepted = []
    rejected = []

    class Provider:
        closed = False

        def force_flush(self, *, timeout_millis):
            return True

        def shutdown(self, *, timeout_millis):
            self.closed = True

    provider = Provider()

    class Instrument:
        def __init__(self, name):
            self.name = name

        def add(self, _value, _attributes=None):
            (rejected if provider.closed else accepted).append(self.name)

        def record(self, _value, _attributes=None):
            (rejected if provider.closed else accepted).append(self.name)

    class Meter:
        def create_counter(self, name, **_kwargs):
            return Instrument(name)

        def create_histogram(self, name, **_kwargs):
            return Instrument(name)

        def create_observable_gauge(self, name, **_kwargs):
            return Instrument(name)

    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(otel, "get_meter", lambda _name: Meter())
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": True}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
    )

    async def exercise() -> None:
        await telemetry.start()
        monkeypatch.setattr(otel, "_initialized", True)
        monkeypatch.setattr(otel, "_providers", (provider,))
        monkeypatch.setattr(
            otel,
            "_shutdown_ports",
            (
                otel._ShutdownPort(
                    name="metrics",
                    export_timeout_seconds=0.01,
                    force_flush=lambda timeout_millis: provider.force_flush(
                        timeout_millis=timeout_millis
                    ),
                    close=lambda timeout_millis: provider.shutdown(timeout_millis=timeout_millis),
                    worker_alive=lambda: False,
                ),
            ),
        )
        await telemetry.stop()

    asyncio.run(exercise())

    assert provider.closed is True
    assert {
        "bh.daemon.shutdown.duration",
        "bh.daemon.telemetry.flush",
        "bh.daemon.lifecycle",
    }.issubset(accepted)
    assert rejected == []


def test_request_and_session_metrics_use_bounded_dimensions_and_gauges_return_zero(
    monkeypatch,
) -> None:
    meter = _Meter()
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(otel, "shutdown", lambda **_kwargs: _Flush("completed"))
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": True}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
        monotonic=lambda: 10.0,
    )
    asyncio.run(telemetry.start())

    request = telemetry.begin_request(
        route_template="/api/v1/hives/{hive_id}/snapshot",
        method="GET",
        protocol="http",
    )
    telemetry.finish_request(request, status_code=503)
    session = telemetry.open_connection("mcp")
    telemetry.close_connection(session, reason="cancelled")
    telemetry.record_backpressure("sse", "slow_consumer")
    telemetry.record_replay_gap("expired_cursor")
    telemetry.record_reset("source_discontinuity")
    telemetry.record_dependency_probe("hq", "ready", 0.02)
    telemetry.set_queue_depth("sse-client", 4)
    telemetry.set_queue_depth("sse-client", 0)

    request_attrs = meter.instruments["bh.daemon.requests"].values[-1][1]
    assert request_attrs == {
        "http.route": "/api/v1/hives/{hive_id}/snapshot",
        "http.request.method": "GET",
        "network.protocol.name": "http",
        "bh.daemon.request.outcome": "error",
    }
    assert "hive_id" not in request_attrs
    assert meter.instruments["bh.daemon.cancellations"].values[-1][1] == {
        "bh.daemon.surface": "mcp",
        "bh.daemon.reason": "cancelled",
    }

    request_observations = list(meter.callbacks["bh.daemon.requests.active"](None))
    connection_observations = list(meter.callbacks["bh.daemon.connections.active"](None))
    assert [item.value for item in request_observations] == [0]
    assert [item.value for item in connection_observations] == [0]
    assert list(meter.callbacks["bh.daemon.queue.depth"](None))[0].value == 0


def test_raw_or_unknown_metric_dimensions_collapse_instead_of_creating_series(monkeypatch) -> None:
    meter = _Meter()
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: True)
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": True}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
    )
    asyncio.run(telemetry.start())
    request = telemetry.begin_request(
        route_template="/api/v1/hives/secret-customer/snapshot?token=secret",
        method="EXPLODE",
        protocol="something-new",
    )
    telemetry.finish_request(request, status_code=200)
    assert meter.instruments["bh.daemon.requests"].values[-1][1] == {
        "http.route": "unmatched",
        "http.request.method": "OTHER",
        "network.protocol.name": "other",
        "bh.daemon.request.outcome": "ok",
    }


def test_asgi_middleware_records_stream_lifetime_by_route_template(monkeypatch) -> None:
    meter = _Meter()
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: True)
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": True}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
    )
    asyncio.run(telemetry.start())

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = daemon_telemetry.DaemonTelemetryMiddleware(app, telemetry=telemetry)

    async def request() -> None:
        sent = []

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/runs/private-run-id/activity",
            },
            receive,
            send,
        )

    asyncio.run(request())
    attrs = meter.instruments["bh.daemon.requests"].values[-1][1]
    assert attrs["http.route"] == "/api/v1/runs/{run_id}/activity"
    assert "private-run-id" not in repr(attrs)
    assert list(meter.callbacks["bh.daemon.requests.active"](None))[0].value == 0


def test_websocket_app_failure_fails_request_and_connection_without_leaking(
    monkeypatch,
) -> None:
    meter = _Meter()
    sink = RecordingTelemetrySink(capacity=8)
    semantic = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(
            service="bh-host-daemon",
            instance_id="instance-one",
            host_id="host-stable",
        ),
    )
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: True)
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": True}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
        semantic_telemetry=semantic,
    )
    asyncio.run(telemetry.start())

    async def app(_scope, _receive, send):
        await send({"type": "websocket.accept"})
        raise RuntimeError("private-websocket-failure")

    middleware = daemon_telemetry.DaemonTelemetryMiddleware(app, telemetry=telemetry)
    sent = []

    async def request() -> None:
        async def receive():
            return {"type": "websocket.disconnect", "code": 1011}

        async def send(message):
            sent.append(message)

        with pytest.raises(RuntimeError, match="private-websocket-failure"):
            await middleware(
                {
                    "type": "websocket",
                    "path": "/api/v1/terminal/private-session",
                },
                receive,
                send,
            )

    asyncio.run(request())

    started = {
        event.correlation_id: event for event in sink.events if event.phase is EventPhase.STARTED
    }
    completed = [event for event in sink.events if event.phase is EventPhase.COMPLETED]
    assert [event.outcome for event in completed] == [Outcome.FAILED, Outcome.FAILED]
    assert [event.error.classification for event in completed if event.error is not None] == [
        ErrorClassification.INTERNAL,
        ErrorClassification.INTERNAL,
    ]
    assert [event.error.code for event in completed if event.error is not None] == [
        "request.failed",
        "connection.failed",
    ]
    for terminal in completed:
        assert terminal.causation_id == started[terminal.correlation_id].event_id
    assert "private-websocket-failure" not in repr(sink.events)
    assert sent == [{"type": "websocket.accept"}]
    assert telemetry._semantic_request_tokens == {}
    assert telemetry._semantic_connection_tokens == {}
    assert telemetry._request_tokens == {}
    assert telemetry._connection_tokens == {}
    assert list(meter.callbacks["bh.daemon.requests.active"](None))[0].value == 0
    assert list(meter.callbacks["bh.daemon.connections.active"](None))[0].value == 0
    assert (
        meter.instruments["bh.daemon.requests"].values[-1][1]["bh.daemon.request.outcome"]
        == "error"
    )
    assert meter.instruments["bh.daemon.errors"].values[-1][1]["http.response.status_code"] == 500
    assert meter.instruments["bh.daemon.connections"].values[-1][1] == {
        "bh.daemon.connection": "terminal",
        "bh.daemon.connection.outcome": "closed",
        "bh.daemon.reason": "client_closed",
    }


@pytest.mark.parametrize(
    ("reason", "failed", "expected_outcome", "expected_classification", "expected_code"),
    (
        ("client_closed", False, Outcome.SUCCEEDED, None, None),
        (
            "cancelled",
            False,
            Outcome.CANCELLED,
            ErrorClassification.CANCELLATION,
            "connection.cancelled",
        ),
        (
            "timeout",
            False,
            Outcome.CANCELLED,
            ErrorClassification.CANCELLATION,
            "connection.cancelled",
        ),
        (
            "daemon_shutdown",
            False,
            Outcome.CANCELLED,
            ErrorClassification.CANCELLATION,
            "connection.cancelled",
        ),
        (
            "client_closed",
            True,
            Outcome.FAILED,
            ErrorClassification.INTERNAL,
            "connection.failed",
        ),
    ),
)
def test_connection_semantic_terminal_classification_is_exactly_once(
    reason,
    failed,
    expected_outcome,
    expected_classification,
    expected_code,
) -> None:
    sink = RecordingTelemetrySink(capacity=4)
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": False}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
        semantic_telemetry=SemanticTelemetry(
            sink=sink,
            identity=EventIdentity(
                service="bh-host-daemon",
                instance_id="instance-one",
                host_id="host-stable",
            ),
        ),
    )

    connection = telemetry.open_connection("terminal")
    telemetry.close_connection(connection, reason=reason, failed=failed)
    telemetry.close_connection(connection, reason="other", failed=not failed)

    completed = [event for event in sink.events if event.phase is EventPhase.COMPLETED]
    assert len(completed) == 1
    assert completed[0].outcome is expected_outcome
    if expected_classification is None:
        assert completed[0].error is None
    else:
        assert completed[0].error is not None
        assert completed[0].error.classification is expected_classification
        assert completed[0].error.code == expected_code
    assert telemetry._semantic_connection_tokens == {}


def test_exact_mcp_session_register_and_terminate_own_active_gauge(monkeypatch) -> None:
    meter = _Meter()
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: True)
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": True}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
    )
    asyncio.run(telemetry.start())

    class Credentials:
        def open(self, *_args, **_kwargs):
            return object()

        def unregister(self, _session):
            pass

    class Network:
        async def forget_mcp_session(self, _session_id):
            pass

    class Principal:
        principal = "operator"

    lifecycle = host_daemon._McpSessionLifecycle(
        credential_sessions=Credentials(),
        network_policy=Network(),
        idle_seconds=10,
        absolute_seconds=20,
        telemetry=telemetry,
    )

    async def exercise() -> None:
        await lifecycle.register("sensitive-session-id", bearer=object(), principal=Principal())
        active = list(meter.callbacks["bh.daemon.connections.active"](None))
        assert [(item.value, item.attributes) for item in active] == [
            (1, {"bh.daemon.connection": "mcp"})
        ]
        await lifecycle.terminate("sensitive-session-id", reason="daemon_shutdown")

    asyncio.run(exercise())
    assert list(meter.callbacks["bh.daemon.connections.active"](None))[0].value == 0
    assert "sensitive-session-id" not in repr(meter.instruments["bh.daemon.connections"].values)


def test_daemon_routes_sessions_probes_and_flush_share_semantic_port_exactly_once(
    monkeypatch,
) -> None:
    meter = _Meter()
    sink = RecordingTelemetrySink(capacity=16)
    semantic = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(
            service="bh-host-daemon",
            instance_id="instance-one",
            host_id="host-stable",
        ),
    )
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: True)

    def shutdown(**kwargs):
        result = _Flush("completed")
        kwargs["before_close"](result)
        return result

    monkeypatch.setattr(otel, "shutdown", shutdown)
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": True}},
        host_id="host-stable",
        instance_id="instance-one",
        flush_budget_seconds=0.2,
        semantic_telemetry=semantic,
    )

    async def exercise() -> None:
        await telemetry.start()
        request = telemetry.begin_request(
            route_template="/api/v1/runs/{run_id}/activity",
            method="GET",
            protocol="http",
        )
        telemetry.finish_request(request, status_code=200)
        telemetry.finish_request(request, status_code=500)
        session = telemetry.open_connection("sse")
        telemetry.close_connection(session, reason="daemon_shutdown")
        telemetry.close_connection(session, reason="other")
        telemetry.record_dependency_probe("dolt", "unavailable", 0.01)
        await telemetry.stop()

    asyncio.run(exercise())

    assert len(sink.events) == 8
    assert [event.event_name for event in sink.events] == [
        SemanticEventName.OPERATION_EXECUTION,
        SemanticEventName.OPERATION_EXECUTION,
        SemanticEventName.OPERATION_EXECUTION,
        SemanticEventName.OPERATION_EXECUTION,
        SemanticEventName.OPERATION_EXECUTION,
        SemanticEventName.OPERATION_EXECUTION,
        SemanticEventName.TELEMETRY_FLUSH,
        SemanticEventName.TELEMETRY_FLUSH,
    ]
    assert [event.outcome for event in sink.events if event.outcome is not None] == [
        Outcome.SUCCEEDED,
        Outcome.CANCELLED,
        Outcome.FAILED,
        Outcome.SUCCEEDED,
    ]
    assert "private-run" not in repr(sink.events)
    assert "host-stable" in repr(sink.events)
    assert list(meter.callbacks["bh.daemon.requests.active"](None))[0].value == 0
    assert list(meter.callbacks["bh.daemon.connections.active"](None))[0].value == 0


@pytest.mark.parametrize(
    "otel_config",
    ({"enabled": False}, {"enabled": True}),
    ids=("disabled", "sdk-unavailable"),
)
def test_semantic_terminals_do_not_depend_on_legacy_otel_wiring(monkeypatch, otel_config) -> None:
    sink = RecordingTelemetrySink(capacity=32)
    semantic = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(
            service="bh-host-daemon",
            instance_id="instance-unwired",
            host_id="host-stable",
        ),
    )
    completion_observations = []
    actual_complete = semantic.complete

    def complete(observation, outcome, *, error=None):
        completion_observations.append(observation)
        return actual_complete(observation, outcome, error=error)

    monkeypatch.setattr(semantic, "complete", complete)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: False)
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": otel_config},
        host_id="host-stable",
        instance_id="instance-unwired",
        flush_budget_seconds=0.2,
        semantic_telemetry=semantic,
    )

    async def exercise() -> None:
        assert await telemetry.start() is False
        succeeded = telemetry.begin_request(route_template="/health", method="GET", protocol="http")
        failed = telemetry.begin_request(
            route_template="/api/v1/factory", method="GET", protocol="http"
        )
        cancelled = telemetry.begin_request(
            route_template="/api/v1/hives/{hive_id}/events",
            method="GET",
            protocol="http",
        )
        closed = telemetry.open_connection("sse")
        aborted = telemetry.open_connection("sse")

        telemetry.finish_request(succeeded, status_code=200)
        telemetry.finish_request(succeeded, status_code=503)
        telemetry.finish_request(failed, status_code=503)
        telemetry.finish_request(failed, status_code=200)
        telemetry.finish_request(cancelled, status_code=499, cancelled=True)
        telemetry.finish_request(cancelled, status_code=200)
        telemetry.close_connection(closed, reason="client_closed")
        telemetry.close_connection(closed, reason="daemon_shutdown")
        telemetry.close_connection(aborted, reason="daemon_shutdown")
        telemetry.close_connection(aborted, reason="client_closed")
        telemetry.record_dependency_probe("dolt", "unavailable", 0.01)
        await telemetry.stop()

    asyncio.run(exercise())

    completed = [event for event in sink.events if event.phase is EventPhase.COMPLETED]
    assert [event.outcome for event in completed] == [
        Outcome.SUCCEEDED,
        Outcome.FAILED,
        Outcome.CANCELLED,
        Outcome.SUCCEEDED,
        Outcome.CANCELLED,
        Outcome.FAILED,
    ]
    assert len(sink.events) == 12
    assert len(completion_observations) == 6
    assert all(observation is not None for observation in completion_observations)
    started_by_correlation = {
        event.correlation_id: event for event in sink.events if event.phase is EventPhase.STARTED
    }
    assert len(started_by_correlation) == 6
    for terminal in completed:
        started = started_by_correlation[terminal.correlation_id]
        assert terminal.correlation_id == started.correlation_id
        assert terminal.causation_id == started.event_id
    assert telemetry._semantic_request_tokens == {}
    assert telemetry._semantic_connection_tokens == {}


def test_unwired_daemon_stop_cancels_open_semantic_observations_once(monkeypatch) -> None:
    sink = RecordingTelemetrySink(capacity=8)
    semantic = SemanticTelemetry(
        sink=sink,
        identity=EventIdentity(
            service="bh-host-daemon",
            instance_id="instance-unwired",
            host_id="host-stable",
        ),
    )
    completion_observations = []
    actual_complete = semantic.complete

    def complete(observation, outcome, *, error=None):
        completion_observations.append(observation)
        return actual_complete(observation, outcome, error=error)

    monkeypatch.setattr(semantic, "complete", complete)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: False)
    telemetry = daemon_telemetry.DaemonTelemetry(
        cfg={"otel": {"enabled": False}},
        host_id="host-stable",
        instance_id="instance-unwired",
        flush_budget_seconds=0.2,
        semantic_telemetry=semantic,
    )

    async def exercise() -> None:
        await telemetry.start()
        request = telemetry.begin_request(route_template="/health", method="GET", protocol="http")
        connection = telemetry.open_connection("sse")
        await telemetry.stop()
        telemetry.finish_request(request, status_code=200)
        telemetry.close_connection(connection)

    asyncio.run(exercise())

    completed = [event for event in sink.events if event.phase is EventPhase.COMPLETED]
    assert [event.outcome for event in completed] == [Outcome.CANCELLED, Outcome.CANCELLED]
    assert len(sink.events) == 4
    assert len(completion_observations) == 2
    assert all(observation is not None for observation in completion_observations)
    assert telemetry._semantic_request_tokens == {}
    assert telemetry._semantic_connection_tokens == {}


def test_daemon_error_cancel_and_restart_zero_every_active_gauge_without_leaks(
    monkeypatch,
) -> None:
    meter = _Meter()
    sink = RecordingTelemetrySink(capacity=32)
    monkeypatch.setattr(otel, "get_meter", lambda _name: meter)
    monkeypatch.setattr(otel, "init", lambda *_args, **_kwargs: True)

    def shutdown(**kwargs):
        result = _Flush("completed")
        kwargs["before_close"](result)
        return result

    monkeypatch.setattr(otel, "shutdown", shutdown)

    async def run_incarnation(instance_id: str, *, cancel_request: bool) -> None:
        semantic = SemanticTelemetry(
            sink=sink,
            identity=EventIdentity(
                service="bh-host-daemon",
                instance_id=instance_id,
                host_id="host-stable",
            ),
        )
        telemetry = daemon_telemetry.DaemonTelemetry(
            cfg={"otel": {"enabled": True}},
            host_id="host-stable",
            instance_id=instance_id,
            flush_budget_seconds=0.2,
            semantic_telemetry=semantic,
        )
        await telemetry.start()
        request = telemetry.begin_request(
            route_template="/api/v1/hives/{hive_id}/events",
            method="GET",
            protocol="http",
        )
        telemetry.finish_request(
            request,
            status_code=499 if cancel_request else 503,
            cancelled=cancel_request,
        )
        telemetry.open_connection("sse")
        telemetry.set_queue_depth("sse-client", 4)
        await telemetry.stop()
        assert telemetry._request_tokens == {}
        assert telemetry._connection_tokens == {}

    async def exercise() -> None:
        await run_incarnation("instance-one", cancel_request=False)
        await run_incarnation("instance-two", cancel_request=True)

    asyncio.run(exercise())

    assert meter.instruments["bh.daemon.restarts"].values == [(1, {}), (1, {})]
    assert list(meter.callbacks["bh.daemon.requests.active"](None))[0].value == 0
    assert list(meter.callbacks["bh.daemon.connections.active"](None))[0].value == 0
    assert list(meter.callbacks["bh.daemon.queue.depth"](None))[0].value == 0
    assert {event.identity.instance_id for event in sink.events} == {
        "instance-one",
        "instance-two",
    }
    assert [
        event.outcome
        for event in sink.events
        if event.event_name is SemanticEventName.OPERATION_EXECUTION and event.outcome is not None
    ] == [Outcome.FAILED, Outcome.CANCELLED, Outcome.CANCELLED, Outcome.CANCELLED]
