"""Authenticated, read-only Development gateway profile.

This module is deliberately separate from :mod:`beadhive.operator_api`: the local profile keeps
its loopback-only, unauthenticated contract while this boundary authenticates and projects a
small, explicitly allowlisted remote representation.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from joserfc import jwt
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

CONTRACT_VERSION = "gateway.v1"
SCHEMA_VERSION = 1
DEVELOPMENT_INSTANCE_ID = "dev/demo"
DEVELOPMENT_ISSUER = "https://rapid-snail-6758.clerk.accounts.dev"
_ALGORITHM = "RS256"


@dataclass(frozen=True)
class DevelopmentGatewayConfig:
    issuer: str
    audience: str
    app_origin: str
    gateway_origin: str

    def __post_init__(self) -> None:
        if self.audience != "beadhive-gateway-dev":
            raise ValueError("Development gateway audience must be beadhive-gateway-dev")
        _require_exact_https_origin(self.issuer, "issuer")
        _require_exact_https_origin(self.app_origin, "application origin")
        _require_exact_https_origin(self.gateway_origin, "gateway origin")
        if self.app_origin != "https://app.dev.beadhive.cloud":
            raise ValueError("Development gateway requires the canonical Development app origin")
        if self.gateway_origin != "https://gateway.dev.beadhive.cloud":
            raise ValueError("Development gateway requires the canonical Development host")
        if self.issuer != DEVELOPMENT_ISSUER:
            raise ValueError("Development gateway requires the exact Clerk Development issuer")


def _require_exact_https_origin(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        raise ValueError(f"{label} must be one exact HTTPS origin")


class AuthenticationFailed(Exception):
    """A deliberately detail-free authentication failure."""


class RemoteProjectionFailed(Exception):
    """An internal payload did not satisfy the remote disclosure contract."""


class RuntimeCallTimedOut(Exception):
    """A runtime source exceeded its isolated call budget."""


@dataclass(frozen=True)
class ClerkTokenVerifier:
    config: DevelopmentGatewayConfig
    key: Any
    revoked_subjects: frozenset[str] = frozenset()
    now: Callable[[], float] = time.time

    def verify(self, encoded: str) -> str:
        if not 1 <= len(encoded) <= 16_384:
            raise AuthenticationFailed
        try:
            token = jwt.decode(encoded, self.key, algorithms=[_ALGORITHM])
            claims = token.claims
            issuer = claims.get("iss")
            audience = claims.get("aud")
            subject = claims.get("sub")
            expires_at = claims.get("exp")
            not_before = claims.get("nbf")
            now = self.now()
            if issuer != self.config.issuer or audience != self.config.audience:
                raise AuthenticationFailed
            if not isinstance(subject, str) or not subject or subject in self.revoked_subjects:
                raise AuthenticationFailed
            if not isinstance(expires_at, int | float) or expires_at <= now:
                raise AuthenticationFailed
            if not_before is not None and (
                not isinstance(not_before, int | float) or not_before > now
            ):
                raise AuthenticationFailed
        except AuthenticationFailed:
            raise
        except Exception as exc:
            raise AuthenticationFailed from exc
        return subject


@dataclass(frozen=True)
class RemoteInstance:
    display_name: str
    authorized_subjects: frozenset[str]
    snapshot: Callable[[], Awaitable[Mapping[str, object]]]
    online: Callable[[], Awaitable[bool]]

    def __post_init__(self) -> None:
        for operation, label in ((self.snapshot, "snapshot"), (self.online, "online")):
            if not inspect.iscoroutinefunction(operation):
                raise TypeError(f"remote {label} operation must be async and cancellation-aware")


@dataclass(frozen=True)
class RuntimeCallPolicy:
    """Concurrency bulkheads and per-call deadline for async runtime sources."""

    deadline_seconds: float = 5.0
    snapshot_concurrency: int = 4
    availability_concurrency: int = 2

    def __post_init__(self) -> None:
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, int | float)
            or not math.isfinite(self.deadline_seconds)
            or not 0.01 <= self.deadline_seconds <= 60.0
        ):
            raise ValueError("runtime deadline must be between 0.01 and 60 seconds")
        for value, label in (
            (self.snapshot_concurrency, "snapshot concurrency"),
            (self.availability_concurrency, "availability concurrency"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
                raise ValueError(f"{label} must be between 1 and 32")


class _BoundedRuntimeCalls:
    """Bound cancellation-aware runtime operations without an internal work queue."""

    def __init__(self, *, concurrency: int, deadline_seconds: float, name: str) -> None:
        self._deadline_seconds = deadline_seconds
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._name = name

    async def call(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        if self._closed or self._slots.locked():
            raise RuntimeCallTimedOut
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._deadline_seconds
        await self._slots.acquire()
        task = asyncio.create_task(operation(), name=self._name)
        self._tasks.add(task)
        try:
            async with asyncio.timeout_at(deadline):
                return await task
        except TimeoutError as exc:
            raise RuntimeCallTimedOut from exc
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._tasks.discard(task)
            self._slots.release()

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True)
class DevelopmentInstanceRegistry:
    instances: Mapping[str, RemoteInstance]

    def __post_init__(self) -> None:
        if set(self.instances) - {DEVELOPMENT_INSTANCE_ID}:
            raise ValueError("Development registry may contain only dev/demo")

    def authorized(self, subject: str) -> dict[str, RemoteInstance]:
        return {
            instance_id: instance
            for instance_id, instance in self.instances.items()
            if subject in instance.authorized_subjects
        }


_ERROR_KEYS = frozenset({"error"})
_ERROR_DETAIL_KEYS = frozenset({"code", "message", "retryable"})
_INSTANCE_PAGE_KEYS = frozenset({"schemaVersion", "items", "nextCursor"})
_INSTANCE_KEYS = frozenset({"id", "displayName", "availability", "capabilities"})
_ENVELOPE_KEYS = frozenset({"schemaVersion", "contractVersion", "instanceId", "snapshot"})
_SNAPSHOT_KEYS = frozenset({"schemaVersion", "revision", "generatedAt", "workItems", "agents"})
_WORK_ITEM_KEYS = frozenset(
    {"id", "title", "status", "issueType", "priority", "labels", "assignee", "updatedAt"}
)
_AGENT_KEYS = frozenset({"id", "state", "ownerSeat", "startedAt", "updatedAt", "endedAt"})
_MAX_WORK_ITEMS = 1_000
_MAX_AGENTS = 256
_MAX_LABELS = 64
_MAX_JSON_SAFE_INTEGER = 2**53 - 1


def _exact_keys(value: object, expected: frozenset[str]) -> bool:
    return isinstance(value, dict) and set(value) == expected


def _string(value: object, *, maximum: int, optional: bool = False) -> bool:
    return (optional and value is None) or (isinstance(value, str) and 0 < len(value) <= maximum)


def _timestamp(value: object, *, optional: bool = False) -> bool:
    return (optional and value is None) or (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_JSON_SAFE_INTEGER
    )


def _schema_version(value: object) -> bool:
    return type(value) is int and value == SCHEMA_VERSION


def _work_item_is_allowlisted(value: object) -> bool:
    if not _exact_keys(value, _WORK_ITEM_KEYS):
        return False
    labels = value["labels"]
    return (
        _string(value["id"], maximum=256)
        and _string(value["title"], maximum=4_096)
        and _string(value["status"], maximum=128)
        and _string(value["issueType"], maximum=128)
        and isinstance(value["priority"], int)
        and not isinstance(value["priority"], bool)
        and 0 <= value["priority"] <= 4
        and isinstance(labels, list)
        and len(labels) <= _MAX_LABELS
        and all(_string(label, maximum=256) for label in labels)
        and _string(value["assignee"], maximum=256, optional=True)
        and _timestamp(value["updatedAt"])
    )


def _agent_is_allowlisted(value: object) -> bool:
    return (
        _exact_keys(value, _AGENT_KEYS)
        and _string(value["id"], maximum=256)
        and _string(value["state"], maximum=128)
        and _string(value["ownerSeat"], maximum=256, optional=True)
        and _timestamp(value["startedAt"], optional=True)
        and _timestamp(value["updatedAt"])
        and _timestamp(value["endedAt"], optional=True)
    )


def remote_payload_is_allowlisted(kind: str, payload: object) -> bool:
    """Return whether *payload* is exactly one public remote wire shape."""
    if kind == "error":
        return (
            _exact_keys(payload, _ERROR_KEYS)
            and _exact_keys(payload["error"], _ERROR_DETAIL_KEYS)
            and _string(payload["error"]["code"], maximum=128)
            and _string(payload["error"]["message"], maximum=512)
            and isinstance(payload["error"]["retryable"], bool)
        )
    if kind == "instances":
        if not _exact_keys(payload, _INSTANCE_PAGE_KEYS):
            return False
        items = payload["items"]
        return (
            _schema_version(payload["schemaVersion"])
            and payload["nextCursor"] is None
            and isinstance(items, list)
            and len(items) <= 1
            and all(
                _exact_keys(item, _INSTANCE_KEYS)
                and item["id"] == DEVELOPMENT_INSTANCE_ID
                and _string(item["displayName"], maximum=256)
                and item["availability"] in ("online", "offline")
                and item["capabilities"] == ["snapshot"]
                for item in items
            )
        )
    if kind == "snapshot":
        if not _exact_keys(payload, _ENVELOPE_KEYS):
            return False
        snapshot = payload["snapshot"]
        if not _exact_keys(snapshot, _SNAPSHOT_KEYS):
            return False
        work_items = snapshot["workItems"]
        agents = snapshot["agents"]
        return (
            _schema_version(payload["schemaVersion"])
            and payload["contractVersion"] == CONTRACT_VERSION
            and payload["instanceId"] == DEVELOPMENT_INSTANCE_ID
            and _schema_version(snapshot["schemaVersion"])
            and _string(snapshot["revision"], maximum=256)
            and _timestamp(snapshot["generatedAt"])
            and isinstance(work_items, list)
            and len(work_items) <= _MAX_WORK_ITEMS
            and all(_work_item_is_allowlisted(item) for item in work_items)
            and isinstance(agents, list)
            and len(agents) <= _MAX_AGENTS
            and all(_agent_is_allowlisted(item) for item in agents)
        )
    return False


def _response(kind: str, payload: dict[str, object], status_code: int = 200) -> JSONResponse:
    if not remote_payload_is_allowlisted(kind, payload):
        raise RemoteProjectionFailed("remote response did not match its disclosure allowlist")
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _error(code: str, message: str, status_code: int, *, retryable: bool = False) -> JSONResponse:
    return _response(
        "error",
        {"error": {"code": code, "message": message, "retryable": retryable}},
        status_code,
    )


def _public_snapshot(raw: Mapping[str, object]) -> dict[str, object]:
    try:
        if not _schema_version(raw["schemaVersion"]):
            raise RemoteProjectionFailed("runtime snapshot is incompatible")
        revision = raw["revision"]
        generated_at = raw["generatedAt"]
        if not isinstance(revision, str) or not revision:
            raise RemoteProjectionFailed("runtime snapshot is incompatible")
        if not isinstance(generated_at, int) or isinstance(generated_at, bool):
            raise RemoteProjectionFailed("runtime snapshot is incompatible")
        raw_work_items = raw["workItems"]
        raw_agents = raw["agents"]
        if (
            not isinstance(raw_work_items, list)
            or len(raw_work_items) > _MAX_WORK_ITEMS
            or not isinstance(raw_agents, list)
            or len(raw_agents) > _MAX_AGENTS
        ):
            raise RemoteProjectionFailed("runtime snapshot is incompatible")
        work_items = []
        for item in raw_work_items:
            record = item["record"]
            labels = record["labels"]
            if not isinstance(labels, list) or len(labels) > _MAX_LABELS:
                raise RemoteProjectionFailed("runtime snapshot is incompatible")
            work_items.append(
                {
                    "id": record["id"],
                    "title": record["title"],
                    "status": record["status"],
                    "issueType": record["issueType"],
                    "priority": record["priority"],
                    "labels": list(labels),
                    "assignee": record["assignee"],
                    "updatedAt": item["updatedAt"],
                }
            )
        agents = []
        for item in raw_agents:
            agents.append(
                {
                    "id": item["ref"]["id"],
                    "state": item["state"],
                    "ownerSeat": item["ownerSeat"],
                    "startedAt": item["startedAt"],
                    "updatedAt": item["updatedAt"],
                    "endedAt": item["endedAt"],
                }
            )
        public = {
            "schemaVersion": SCHEMA_VERSION,
            "revision": revision,
            "generatedAt": generated_at,
            "workItems": work_items,
            "agents": agents,
        }
    except (KeyError, TypeError) as exc:
        raise RemoteProjectionFailed("runtime snapshot is incompatible") from exc
    if not _exact_keys(public, _SNAPSHOT_KEYS):
        raise RemoteProjectionFailed("runtime snapshot is incompatible")
    return public


async def _read_public_snapshot(instance: RemoteInstance) -> dict[str, object]:
    return _public_snapshot(await instance.snapshot())


def build_development_gateway_application(
    *,
    config: DevelopmentGatewayConfig,
    verifier: ClerkTokenVerifier,
    registry: DevelopmentInstanceRegistry,
    runtime_calls: RuntimeCallPolicy | None = None,
) -> Starlette:
    """Build the remote Development read profile without mutating the loopback application."""
    runtime_calls = runtime_calls or RuntimeCallPolicy()
    gateway_host = urlsplit(config.gateway_origin).netloc
    discovery_availability_calls = _BoundedRuntimeCalls(
        concurrency=runtime_calls.availability_concurrency,
        deadline_seconds=runtime_calls.deadline_seconds,
        name="beadhive-gateway-discovery-availability",
    )
    snapshot_availability_calls = _BoundedRuntimeCalls(
        concurrency=runtime_calls.availability_concurrency,
        deadline_seconds=runtime_calls.deadline_seconds,
        name="beadhive-gateway-snapshot-availability",
    )
    snapshot_calls = _BoundedRuntimeCalls(
        concurrency=runtime_calls.snapshot_concurrency,
        deadline_seconds=runtime_calls.deadline_seconds,
        name="beadhive-gateway-snapshot",
    )

    def authorize(request: Request) -> str:
        if request.headers.getlist("host") != [gateway_host]:
            raise PermissionError
        origins = request.headers.getlist("origin")
        if origins != [config.app_origin]:
            raise PermissionError
        authorizations = request.headers.getlist("authorization")
        if len(authorizations) != 1 or not authorizations[0].startswith("Bearer "):
            raise AuthenticationFailed
        encoded = authorizations[0].removeprefix("Bearer ")
        if not encoded or encoded.strip() != encoded:
            raise AuthenticationFailed
        return verifier.verify(encoded)

    async def read_availability(calls: _BoundedRuntimeCalls, instance: RemoteInstance) -> bool:
        availability = await calls.call(instance.online)
        if type(availability) is not bool:
            raise RemoteProjectionFailed("runtime availability is incompatible")
        return availability

    async def public_instance(instance_id: str, instance: RemoteInstance) -> dict[str, object]:
        availability = await read_availability(discovery_availability_calls, instance)
        return {
            "id": instance_id,
            "displayName": instance.display_name,
            "availability": "online" if availability else "offline",
            "capabilities": ["snapshot"],
        }

    async def instances(request: Request) -> JSONResponse:
        try:
            subject = authorize(request)
            if set(request.query_params) - {"limit", "cursor"}:
                return _error("invalid_request", "The request is not valid.", 400)
            if request.query_params.getlist("limit") != ["50"]:
                return _error("invalid_request", "The request is not valid.", 400)
            if request.query_params.getlist("cursor"):
                return _error("invalid_request", "The request is not valid.", 400)
            items = [
                await public_instance(instance_id, instance)
                for instance_id, instance in registry.authorized(subject).items()
            ]
            return _response("instances", {"schemaVersion": 1, "items": items, "nextCursor": None})
        except PermissionError:
            return _error("request_denied", "The request is not allowed.", 403)
        except AuthenticationFailed:
            return _error("authentication_failed", "Authentication failed.", 401)
        except Exception:
            return _error("runtime_unavailable", "The runtime is unavailable.", 503, retryable=True)

    async def snapshot(request: Request) -> JSONResponse:
        try:
            subject = authorize(request)
            instance_id = f"{request.path_params['stage']}/{request.path_params['slug']}"
            instance = registry.authorized(subject).get(instance_id)
            if instance is None:
                return _error("resource_not_found", "The resource was not found.", 404)
            if not await read_availability(snapshot_availability_calls, instance):
                return _error(
                    "runtime_unavailable", "The runtime is unavailable.", 503, retryable=True
                )
            projected = await snapshot_calls.call(lambda: _read_public_snapshot(instance))
            return _response(
                "snapshot",
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "contractVersion": CONTRACT_VERSION,
                    "instanceId": instance_id,
                    "snapshot": projected,
                },
            )
        except PermissionError:
            return _error("request_denied", "The request is not allowed.", 403)
        except AuthenticationFailed:
            return _error("authentication_failed", "Authentication failed.", 401)
        except RemoteProjectionFailed:
            return _error("runtime_unavailable", "The runtime is unavailable.", 503, retryable=True)
        except Exception:
            return _error("runtime_unavailable", "The runtime is unavailable.", 503, retryable=True)

    async def preflight(request: Request) -> Response:
        origins = request.headers.getlist("origin")
        methods = request.headers.getlist("access-control-request-method")
        requested_headers = request.headers.getlist("access-control-request-headers")
        normalized = {
            item.strip().lower()
            for value in requested_headers
            for item in value.split(",")
            if item.strip()
        }
        if (
            request.headers.getlist("host") != [gateway_host]
            or origins != [config.app_origin]
            or methods != ["GET"]
            or len(requested_headers) != 1
            or normalized != {"authorization"}
        ):
            return _error("request_denied", "The request is not allowed.", 403)
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": config.app_origin,
                "Access-Control-Allow-Methods": "GET",
                "Access-Control-Allow-Headers": "Authorization",
                "Vary": "Origin",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def absent(request: Request) -> JSONResponse:
        try:
            authorize(request)
            return _error("resource_not_found", "The resource was not found.", 404)
        except PermissionError:
            return _error("request_denied", "The request is not allowed.", 403)
        except AuthenticationFailed:
            return _error("authentication_failed", "Authentication failed.", 401)

    async def absent_preflight(_request: Request) -> JSONResponse:
        return _error("request_denied", "The request is not allowed.", 403)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            yield
        finally:
            await asyncio.gather(
                snapshot_calls.close(),
                snapshot_availability_calls.close(),
                discovery_availability_calls.close(),
            )

    app = Starlette(
        routes=[
            Route("/v1/instances", instances, methods=["GET"]),
            Route("/v1/instances/{stage}/{slug}/snapshot", snapshot, methods=["GET"]),
            Route("/v1/instances", preflight, methods=["OPTIONS"]),
            Route("/v1/instances/{stage}/{slug}/snapshot", preflight, methods=["OPTIONS"]),
            Route("/{path:path}", absent, methods=["GET"]),
            Route("/{path:path}", absent_preflight, methods=["OPTIONS"]),
        ],
        lifespan=lifespan,
    )

    async def cors_and_read_only(request: Request, call_next):
        if request.method not in {"GET", "OPTIONS"}:
            response = _error("read_only_profile", "The gateway is read-only.", 405)
        else:
            response = await call_next(request)
        if request.headers.get("origin") == config.app_origin:
            response.headers["Access-Control-Allow-Origin"] = config.app_origin
            response.headers["Vary"] = "Origin"
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=cors_and_read_only)

    return app
