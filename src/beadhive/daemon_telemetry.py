"""Bounded adapter/composition telemetry owned by one host-daemon lifespan.

Daemon route, session/SSE, dependency, gauge, and shutdown facts are translated through the
kernel semantic port.  This adapter also preserves the established ``bh.daemon.*`` dashboard
instruments while :mod:`beadhive.otel` owns the shared SDK provider.  It retains no event journal
or export queue; delivery durability and collector lifecycle remain external concerns.
"""

from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from . import otel
from .kernel.telemetry import (
    AttributeKey,
    ErrorClassification,
    EventError,
    Outcome,
    SemanticEventName,
    SemanticTelemetryPort,
    TelemetryAttribute,
    TelemetryObservation,
)

SERVICE_NAME = "bh-host-daemon"

_ROUTES = frozenset(
    {
        "/health",
        "/mcp",
        "/openapi.json",
        "/api/v1/factory",
        "/api/v1/factory/hives",
        "/api/v1/hives/{hive_id}/snapshot",
        "/api/v1/hives/{hive_id}/events",
        "/api/v1/runs/{run_id}/activity",
        "/api/v1/terminal/attach-token",
        "/ws/terminal",
        "unmatched",
    }
)
_METHODS = frozenset({"GET", "POST", "DELETE", "OPTIONS", "WEBSOCKET"})
_PROTOCOLS = frozenset({"http", "websocket"})
_CONNECTIONS = frozenset({"mcp", "sse", "terminal"})
_DEPENDENCIES = frozenset({"hq", "dolt", "bead-state", "run-journals"})
_DEPENDENCY_STATES = frozenset({"ready", "degraded", "unavailable", "unknown"})
_REASONS = frozenset(
    {
        "auth_expired",
        "auth_revoked",
        "auth_rotated",
        "cancelled",
        "client_closed",
        "daemon_shutdown",
        "event_loop_closed",
        "expired_cursor",
        "future_sequence",
        "hive_removed",
        "resnapshot_required",
        "slow_consumer",
        "source_discontinuity",
        "unknown_epoch",
        "retention_gap",
        "timeout",
        "other",
    }
)
_HIVE_ROUTE = re.compile(r"^/api/v1/hives/[^/]+/(snapshot|events)$")
_RUN_ROUTE = re.compile(r"^/api/v1/runs/[^/]+/activity$")


def _bounded(value: str, allowed: frozenset[str], fallback: str) -> str:
    return value if value in allowed else fallback


def route_template(path: str) -> str:
    """Map an ASGI path to the finite product route vocabulary without retaining raw IDs."""
    if path in _ROUTES:
        return path
    if path == "/mcp" or path.startswith("/mcp/"):
        return "/mcp"
    match = _HIVE_ROUTE.fullmatch(path)
    if match is not None:
        return f"/api/v1/hives/{{hive_id}}/{match.group(1)}"
    if _RUN_ROUTE.fullmatch(path):
        return "/api/v1/runs/{run_id}/activity"
    return "unmatched"


@dataclass(frozen=True)
class RequestToken:
    route_template: str
    method: str
    protocol: str
    started_at: float
    observation: TelemetryObservation | None = None


@dataclass(frozen=True)
class ConnectionToken:
    kind: str
    observation: TelemetryObservation | None = None


class DaemonTelemetry:
    """One instrumentation set for one daemon incarnation."""

    def __init__(
        self,
        *,
        cfg: dict[str, Any] | None,
        host_id: str,
        instance_id: str,
        flush_budget_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        semantic_telemetry: SemanticTelemetryPort | None = None,
    ) -> None:
        if not host_id or not instance_id:
            raise ValueError("daemon telemetry requires host and instance identities")
        if not math.isfinite(flush_budget_seconds) or flush_budget_seconds < 0:
            raise ValueError("telemetry flush budget must be finite and non-negative")
        self.cfg = cfg
        self.host_id = host_id
        self.instance_id = instance_id
        self.flush_budget_seconds = float(flush_budget_seconds)
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._started = False
        self._stopped = False
        self._wired = False
        self._started_at = 0.0
        self._meter: Any = None
        self._instruments: dict[str, Any] = {}
        self._active_requests: Counter[tuple[str, str]] = Counter()
        self._active_connections: Counter[str] = Counter()
        self._request_tokens: dict[int, RequestToken] = {}
        self._connection_tokens: dict[int, ConnectionToken] = {}
        self._semantic_request_tokens: dict[int, TelemetryObservation] = {}
        self._semantic_connection_tokens: dict[int, TelemetryObservation] = {}
        self._queue_depths: dict[str, int] = {}
        self._semantic = semantic_telemetry

    async def start(self) -> bool:
        """Initialize the process-global OTel SDK exactly once for this outer lifespan."""
        with self._lock:
            if self._started:
                return False
            self._started = True
            self._started_at = self._monotonic()
        self._wired = otel.init(
            self.cfg,
            service_name=SERVICE_NAME,
            resource_attributes={
                "bh.host.id": self.host_id,
                "service.instance.id": self.instance_id,
            },
            enrich_resource=False,
            shutdown_timeout_seconds=self.flush_budget_seconds,
            register_atexit=False,
        )
        if not self._wired:
            return False
        if self._semantic is None:
            self._semantic = otel.current_semantic_telemetry()
        self._meter = otel.get_meter(SERVICE_NAME)
        self._create_instruments()
        self._add("bh.daemon.restarts", 1, {})
        self._add("bh.daemon.lifecycle", 1, {"bh.daemon.lifecycle.event": "started"})
        return True

    async def stop(self) -> None:
        """Zero current-state gauges and finish export within the configured finite budget."""
        with self._lock:
            if not self._started or self._stopped:
                return
            self._stopped = True
            open_semantic = (
                *self._semantic_request_tokens.values(),
                *self._semantic_connection_tokens.values(),
            )
            self._semantic_request_tokens.clear()
            self._semantic_connection_tokens.clear()
            self._request_tokens.clear()
            self._connection_tokens.clear()
            self._active_requests.clear()
            self._active_connections.clear()
            self._queue_depths = {name: 0 for name in self._queue_depths}
        cancellation = EventError(ErrorClassification.CANCELLATION, "daemon.shutdown")
        for observation in open_semantic:
            self._semantic_complete(observation, Outcome.CANCELLED, cancellation)
        started = self._monotonic()
        if not self._wired:
            return
        self._add("bh.daemon.lifecycle", 1, {"bh.daemon.lifecycle.event": "stopping"})
        flush_observation = None
        if self._semantic is not None and self._semantic is not otel.current_semantic_telemetry():
            flush_observation = self._semantic_begin(
                SemanticEventName.TELEMETRY_FLUSH,
                (
                    TelemetryAttribute(AttributeKey.OPERATION_KIND, "internal"),
                    TelemetryAttribute(AttributeKey.SURFACE, "daemon"),
                    TelemetryAttribute(AttributeKey.SHUTDOWN_PHASE, "flush-telemetry"),
                ),
            )

        def emit_shutdown_summary(flush_result: otel.ShutdownResult) -> None:
            duration = max(0.0, self._monotonic() - started)
            status = _bounded(
                str(getattr(flush_result, "status", "error")),
                frozenset({"completed", "timed_out", "error", "inactive", "refused"}),
                "error",
            )
            self._record("bh.daemon.shutdown.duration", duration, {})
            self._add("bh.daemon.telemetry.flush", 1, {"bh.daemon.flush.outcome": status})
            self._add(
                "bh.daemon.lifecycle",
                1,
                {"bh.daemon.lifecycle.event": "stopped"},
            )
            if flush_observation is not None:
                if status == "completed":
                    self._semantic_complete(flush_observation, Outcome.SUCCEEDED)
                elif status == "timed_out":
                    self._semantic_complete(
                        flush_observation,
                        Outcome.TIMED_OUT,
                        EventError(
                            ErrorClassification.TIMEOUT,
                            "telemetry.flush.timeout",
                            True,
                        ),
                    )
                else:
                    self._semantic_complete(
                        flush_observation,
                        Outcome.FAILED,
                        EventError(
                            ErrorClassification.DEPENDENCY,
                            "telemetry.flush.failed",
                            True,
                        ),
                    )

        otel.shutdown(
            timeout_seconds=self.flush_budget_seconds,
            before_close=emit_shutdown_summary,
        )

    def _semantic_begin(
        self,
        event_name: SemanticEventName,
        attributes: tuple[TelemetryAttribute, ...],
    ) -> TelemetryObservation | None:
        if self._semantic is None:
            return None
        try:
            return self._semantic.begin(event_name, attributes=attributes)
        except BaseException:
            return None

    def _semantic_complete(
        self,
        observation: TelemetryObservation | None,
        outcome: Outcome,
        error: EventError | None = None,
    ) -> None:
        if self._semantic is None or observation is None:
            return
        try:
            self._semantic.complete(observation, outcome, error=error)
        except BaseException:
            pass

    def _create_instruments(self) -> None:
        for name, description in (
            ("bh.daemon.requests", "daemon HTTP request count"),
            ("bh.daemon.errors", "daemon HTTP request errors"),
            ("bh.daemon.connections", "daemon MCP/SSE/terminal connection count"),
            ("bh.daemon.cancellations", "daemon cancellation count"),
            ("bh.daemon.backpressure", "daemon bounded-queue backpressure count"),
            ("bh.daemon.replay.gaps", "daemon SSE replay gap count"),
            ("bh.daemon.resets", "daemon stream reset count"),
            ("bh.daemon.restarts", "daemon incarnation starts"),
            ("bh.daemon.lifecycle", "daemon lifecycle events"),
            ("bh.daemon.dependency.probes", "daemon real dependency probe count"),
            ("bh.daemon.telemetry.flush", "daemon telemetry flush outcomes"),
        ):
            self._instruments[name] = self._meter.create_counter(
                name, unit="1", description=description
            )
        for name, unit, description in (
            ("bh.daemon.request.duration", "s", "daemon HTTP request duration"),
            ("bh.daemon.dependency.duration", "s", "daemon real dependency probe duration"),
            ("bh.daemon.shutdown.duration", "s", "daemon shutdown duration"),
        ):
            self._instruments[name] = self._meter.create_histogram(
                name, unit=unit, description=description
            )
        self._instruments["bh.daemon.requests.active"] = self._meter.create_observable_gauge(
            "bh.daemon.requests.active",
            callbacks=[self._observe_active_requests],
            unit="1",
            description="currently active daemon requests by stable route template",
        )
        self._instruments["bh.daemon.connections.active"] = self._meter.create_observable_gauge(
            "bh.daemon.connections.active",
            callbacks=[self._observe_active_connections],
            unit="1",
            description="currently active daemon MCP/SSE/terminal connections",
        )
        self._instruments["bh.daemon.queue.depth"] = self._meter.create_observable_gauge(
            "bh.daemon.queue.depth",
            callbacks=[self._observe_queue_depths],
            unit="1",
            description="current bounded daemon queue depth",
        )
        self._instruments["bh.daemon.uptime"] = self._meter.create_observable_gauge(
            "bh.daemon.uptime",
            callbacks=[self._observe_uptime],
            unit="s",
            description="daemon incarnation uptime",
        )

    @staticmethod
    def _observation(value: int | float, attributes: dict[str, str]):
        from opentelemetry.metrics import Observation

        return Observation(value, attributes)

    def _observe_active_requests(self, _options):
        with self._lock:
            values = dict(self._active_requests)
        if not values:
            values = {("unmatched", "http"): 0}
        return tuple(
            self._observation(value, {"http.route": route, "network.protocol.name": protocol})
            for (route, protocol), value in sorted(values.items())
        )

    def _observe_active_connections(self, _options):
        with self._lock:
            values = dict(self._active_connections)
        if not values:
            values = {"mcp": 0}
        return tuple(
            self._observation(value, {"bh.daemon.connection": kind})
            for kind, value in sorted(values.items())
        )

    def _observe_queue_depths(self, _options):
        with self._lock:
            values = dict(self._queue_depths)
        if not values:
            values = {"sse-client": 0}
        return tuple(
            self._observation(value, {"bh.daemon.queue": queue})
            for queue, value in sorted(values.items())
        )

    def _observe_uptime(self, _options):
        with self._lock:
            value = 0.0 if self._stopped else max(0.0, self._monotonic() - self._started_at)
        return (self._observation(value, {}),)

    def _counter(self, name: str):
        return self._instruments[name]

    def _histogram(self, name: str):
        return self._instruments[name]

    def _add(self, name: str, value: int, attributes: dict[str, object]) -> None:
        try:
            self._counter(name).add(value, attributes)
        except Exception:
            pass

    def _record(self, name: str, value: float, attributes: dict[str, object]) -> None:
        try:
            self._histogram(name).record(value, attributes)
        except Exception:
            pass

    def begin_request(self, *, route_template: str, method: str, protocol: str) -> RequestToken:
        route = _bounded(route_template, _ROUTES, "unmatched")
        bounded_method = _bounded(method.upper(), _METHODS, "OTHER")
        bounded_protocol = _bounded(protocol, _PROTOCOLS, "other")
        semantic_transport = "websocket" if bounded_protocol == "websocket" else "http"
        observation = self._semantic_begin(
            SemanticEventName.OPERATION_EXECUTION,
            (
                TelemetryAttribute(AttributeKey.OPERATION_KIND, "route"),
                TelemetryAttribute(AttributeKey.SURFACE, "daemon"),
                TelemetryAttribute(AttributeKey.TRANSPORT, semantic_transport),
                TelemetryAttribute(
                    AttributeKey.HTTP_METHOD,
                    bounded_method if bounded_method != "WEBSOCKET" else "OTHER",
                ),
            ),
        )
        token = RequestToken(
            route,
            bounded_method,
            bounded_protocol,
            self._monotonic(),
            observation,
        )
        with self._lock:
            if observation is not None:
                self._semantic_request_tokens[id(token)] = observation
            if self._wired:
                self._active_requests[(route, bounded_protocol)] += 1
                self._request_tokens[id(token)] = token
        return token

    def finish_request(
        self, token: RequestToken, *, status_code: int, cancelled: bool = False
    ) -> None:
        with self._lock:
            observation = self._semantic_request_tokens.pop(id(token), None)
            legacy_token = self._request_tokens.pop(id(token), None)
            if legacy_token is not None:
                key = (token.route_template, token.protocol)
                self._active_requests[key] = max(0, self._active_requests[key] - 1)
        if cancelled:
            self._semantic_complete(
                observation,
                Outcome.CANCELLED,
                EventError(ErrorClassification.CANCELLATION, "request.cancelled"),
            )
        elif status_code >= 400:
            self._semantic_complete(
                observation,
                Outcome.FAILED,
                EventError(ErrorClassification.INTERNAL, "request.failed"),
            )
        else:
            self._semantic_complete(observation, Outcome.SUCCEEDED)
        if legacy_token is None:
            return
        outcome = "cancelled" if cancelled else "error" if status_code >= 400 else "ok"
        attrs = {
            "http.route": token.route_template,
            "http.request.method": token.method,
            "network.protocol.name": token.protocol,
            "bh.daemon.request.outcome": outcome,
        }
        self._add("bh.daemon.requests", 1, attrs)
        self._record(
            "bh.daemon.request.duration",
            max(0.0, self._monotonic() - token.started_at),
            attrs,
        )
        if status_code >= 400:
            self._add(
                "bh.daemon.errors",
                1,
                {"http.route": token.route_template, "http.response.status_code": status_code},
            )
        if cancelled:
            self.record_cancellation(token.protocol, "cancelled")

    def open_connection(self, kind: str) -> ConnectionToken:
        bounded_kind = _bounded(kind, _CONNECTIONS, "mcp")
        semantic_transport = {"sse": "sse", "terminal": "websocket", "mcp": "http"}[bounded_kind]
        observation = self._semantic_begin(
            SemanticEventName.OPERATION_EXECUTION,
            (
                TelemetryAttribute(AttributeKey.OPERATION_KIND, "internal"),
                TelemetryAttribute(AttributeKey.SURFACE, "daemon"),
                TelemetryAttribute(AttributeKey.TRANSPORT, semantic_transport),
            ),
        )
        token = ConnectionToken(bounded_kind, observation)
        with self._lock:
            if observation is not None:
                self._semantic_connection_tokens[id(token)] = observation
            if self._wired:
                self._active_connections[bounded_kind] += 1
        if self._wired:
            self._add(
                "bh.daemon.connections",
                1,
                {"bh.daemon.connection": bounded_kind, "bh.daemon.connection.outcome": "opened"},
            )
            with self._lock:
                self._connection_tokens[id(token)] = token
        return token

    def close_connection(
        self,
        token: ConnectionToken,
        *,
        reason: str = "client_closed",
        failed: bool = False,
    ) -> None:
        bounded_reason = _bounded(reason, _REASONS, "other")
        with self._lock:
            observation = self._semantic_connection_tokens.pop(id(token), None)
            legacy_token = self._connection_tokens.pop(id(token), None)
            if legacy_token is not None:
                self._active_connections[token.kind] = max(
                    0, self._active_connections[token.kind] - 1
                )
        semantic_reason = bounded_reason.replace("_", "-")
        if failed:
            self._semantic_complete(
                observation,
                Outcome.FAILED,
                EventError(ErrorClassification.INTERNAL, "connection.failed"),
            )
        elif semantic_reason in {"cancelled", "daemon-shutdown", "timeout"}:
            self._semantic_complete(
                observation,
                Outcome.CANCELLED,
                EventError(ErrorClassification.CANCELLATION, "connection.cancelled"),
            )
        else:
            self._semantic_complete(observation, Outcome.SUCCEEDED)
        if legacy_token is None:
            return
        self._add(
            "bh.daemon.connections",
            1,
            {
                "bh.daemon.connection": token.kind,
                "bh.daemon.connection.outcome": "closed",
                "bh.daemon.reason": bounded_reason,
            },
        )
        if bounded_reason in {
            "auth_expired",
            "auth_revoked",
            "auth_rotated",
            "cancelled",
            "daemon_shutdown",
            "timeout",
        }:
            self.record_cancellation(token.kind, bounded_reason)

    def record_cancellation(self, surface: str, reason: str) -> None:
        if not self._wired:
            return
        self._add(
            "bh.daemon.cancellations",
            1,
            {
                "bh.daemon.surface": _bounded(surface, _CONNECTIONS | _PROTOCOLS, "other"),
                "bh.daemon.reason": _bounded(reason, _REASONS, "other"),
            },
        )

    def set_queue_depth(self, queue: str, depth: int) -> None:
        if not self._wired:
            return
        bounded_queue = _bounded(
            queue,
            frozenset({"sse-client", "sse-replay", "source-reads"}),
            "other",
        )
        with self._lock:
            self._queue_depths[bounded_queue] = max(0, int(depth))

    def record_backpressure(self, surface: str, reason: str) -> None:
        if self._wired:
            self._add(
                "bh.daemon.backpressure",
                1,
                {
                    "bh.daemon.surface": _bounded(surface, _CONNECTIONS, "mcp"),
                    "bh.daemon.reason": _bounded(reason, _REASONS, "other"),
                },
            )

    def record_replay_gap(self, reason: str) -> None:
        if self._wired:
            self._add(
                "bh.daemon.replay.gaps",
                1,
                {"bh.daemon.reason": _bounded(reason, _REASONS, "other")},
            )

    def record_reset(self, reason: str) -> None:
        if self._wired:
            self._add(
                "bh.daemon.resets", 1, {"bh.daemon.reason": _bounded(reason, _REASONS, "other")}
            )

    def record_dependency_probe(self, dependency: str, status: str, seconds: float) -> None:
        semantic_dependency = dependency if dependency in {"hq", "dolt"} else "other"
        observation = self._semantic_begin(
            SemanticEventName.OPERATION_EXECUTION,
            (
                TelemetryAttribute(AttributeKey.OPERATION_KIND, "internal"),
                TelemetryAttribute(AttributeKey.SURFACE, "daemon"),
                TelemetryAttribute(AttributeKey.DEPENDENCY, semantic_dependency),
            ),
        )
        if status == "ready":
            self._semantic_complete(observation, Outcome.SUCCEEDED)
        else:
            self._semantic_complete(
                observation,
                Outcome.FAILED,
                EventError(ErrorClassification.DEPENDENCY, "dependency.unavailable", True),
            )
        if not self._wired:
            return
        attrs = {
            "bh.daemon.dependency": _bounded(dependency, _DEPENDENCIES, "unknown"),
            "bh.daemon.dependency.status": _bounded(status, _DEPENDENCY_STATES, "unknown"),
        }
        self._add("bh.daemon.dependency.probes", 1, attrs)
        self._record("bh.daemon.dependency.duration", max(0.0, seconds), attrs)


class DaemonTelemetryMiddleware:
    """Record request RED and connection lifetime around the complete ASGI response stream."""

    def __init__(self, app: ASGIApp, *, telemetry: DaemonTelemetry) -> None:
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = str(scope.get("type", ""))
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        route = route_template(str(scope.get("path", "")))
        method = "WEBSOCKET" if scope_type == "websocket" else str(scope.get("method", "OTHER"))
        request = self.telemetry.begin_request(
            route_template=route,
            method=method,
            protocol=scope_type,
        )
        kind = "terminal" if scope_type == "websocket" else None
        connection = self.telemetry.open_connection(kind) if kind is not None else None
        status_code = 500
        cancelled = False
        failed = False

        async def observe(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            elif message["type"] == "websocket.accept":
                status_code = 101
            await send(message)

        try:
            await self.app(scope, receive, observe)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            failed = True
            raise
        finally:
            self.telemetry.finish_request(
                request,
                status_code=500 if failed else status_code,
                cancelled=cancelled,
            )
            if connection is not None:
                self.telemetry.close_connection(
                    connection,
                    reason="cancelled" if cancelled else "client_closed",
                    failed=failed,
                )


__all__ = [
    "ConnectionToken",
    "DaemonTelemetry",
    "DaemonTelemetryMiddleware",
    "RequestToken",
    "SERVICE_NAME",
    "route_template",
]
