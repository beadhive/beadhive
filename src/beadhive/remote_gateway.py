"""Authenticated, read-only Development gateway profile.

This module is deliberately separate from :mod:`beadhive.operator_api`: the local profile keeps
its loopback-only, unauthenticated contract while this boundary authenticates and projects a
small, explicitly allowlisted remote representation.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from joserfc import jwt
from joserfc.jws import JWSRegistry
from joserfc.registry import HeaderParameter
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from beadhive import gateway_read as gateway_read_mod

CONTRACT_VERSION = "gateway.v1"
SCHEMA_VERSION = 1
DEVELOPMENT_INSTANCE_ID = "dev/demo"
DEVELOPMENT_ISSUER = "https://rapid-snail-6758.clerk.accounts.dev"
_ALGORITHM = "RS256"


def _validate_clerk_token_category(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"cl_[A-Za-z0-9_-]{1,128}", value) is None:
        raise ValueError("must be a Clerk token category")


_CLERK_JWS_REGISTRY = JWSRegistry(
    header_registry={
        "cat": HeaderParameter("Clerk token category", _validate_clerk_token_category),
    },
    algorithms=[_ALGORITHM],
)


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
        if self.app_origin != "https://app-dev.beadhive.cloud":
            raise ValueError("Development gateway requires the canonical Development app origin")
        if self.gateway_origin != "https://gateway-dev.beadhive.cloud":
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


class StaleCommandScope(Exception):
    """The runtime atomically refused a command against an old revision."""


class StaleEventCursor(Exception):
    """A stream cursor cannot be replayed and requires a fresh snapshot."""


class EventRetentionGap(StaleEventCursor):
    """The requested cursor predates the retained event window."""


class ProducerEpochChanged(StaleEventCursor):
    """The event producer restarted since the supplied cursor."""


@dataclass(frozen=True)
class ClerkTokenVerifier:
    config: DevelopmentGatewayConfig
    key: Any
    revoked_subjects: frozenset[str] = frozenset()
    now: Callable[[], float] = time.time
    subject_is_revoked: Callable[[str], bool] = lambda _subject: False

    def verify(self, encoded: str) -> str:
        if not 1 <= len(encoded) <= 16_384:
            raise AuthenticationFailed
        try:
            token = jwt.decode(
                encoded,
                self.key,
                algorithms=[_ALGORITHM],
                registry=_CLERK_JWS_REGISTRY,
            )
            claims = token.claims
            issuer = claims.get("iss")
            audience = claims.get("aud")
            subject = claims.get("sub")
            expires_at = claims.get("exp")
            not_before = claims.get("nbf")
            now = self.now()
            if issuer != self.config.issuer or audience != self.config.audience:
                raise AuthenticationFailed
            if (
                not isinstance(subject, str)
                or not subject
                or subject in self.revoked_subjects
                or self.subject_is_revoked(subject)
            ):
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
    refresh: Callable[[str, str], Awaitable[Mapping[str, object]]] | None = None
    events: Callable[[str], Awaitable[AsyncIterator[Mapping[str, object]]]] | None = None
    close: Callable[[], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        for operation, label in ((self.snapshot, "snapshot"), (self.online, "online")):
            if not inspect.iscoroutinefunction(operation):
                raise TypeError(f"remote {label} operation must be async and cancellation-aware")
        if self.refresh is not None and not inspect.iscoroutinefunction(self.refresh):
            raise TypeError("remote refresh operation must be async and cancellation-aware")
        if self.events is not None and not inspect.iscoroutinefunction(self.events):
            raise TypeError("remote events operation must be async and cancellation-aware")
        if self.close is not None and not inspect.iscoroutinefunction(self.close):
            raise TypeError("remote close operation must be async and cancellation-aware")


@dataclass(frozen=True)
class RuntimeCallPolicy:
    """Concurrency bulkheads and per-call deadline for async runtime sources.

    Rich reads and SSE subscriptions have both a per-authenticated-subject partition and a
    process-wide ceiling.  One principal therefore cannot consume another principal's reserved
    admission, while the process still has a hard aggregate safety bound.
    """

    deadline_seconds: float = 5.0
    snapshot_concurrency: int = 4
    availability_concurrency: int = 2
    command_concurrency: int = 2
    rich_read_concurrency: int = 4
    stream_concurrency: int = 16
    rich_read_concurrency_per_subject: int = 1
    stream_concurrency_per_subject: int = 1
    stream_reauthorize_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, int | float)
            or not math.isfinite(self.deadline_seconds)
            or not 0.01 <= self.deadline_seconds <= 60.0
        ):
            raise ValueError("runtime deadline must be between 0.01 and 60 seconds")
        if (
            isinstance(self.stream_reauthorize_seconds, bool)
            or not isinstance(self.stream_reauthorize_seconds, int | float)
            or not math.isfinite(self.stream_reauthorize_seconds)
            or not 0.05 <= self.stream_reauthorize_seconds <= 30.0
        ):
            raise ValueError("stream reauthorization interval must be between 0.05 and 30 seconds")
        for value, label in (
            (self.snapshot_concurrency, "snapshot concurrency"),
            (self.availability_concurrency, "availability concurrency"),
            (self.command_concurrency, "command concurrency"),
            (self.rich_read_concurrency, "rich read concurrency"),
            (self.stream_concurrency, "stream concurrency"),
            (self.rich_read_concurrency_per_subject, "rich read concurrency per subject"),
            (self.stream_concurrency_per_subject, "stream concurrency per subject"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
                raise ValueError(f"{label} must be between 1 and 32")
        if self.rich_read_concurrency_per_subject > self.rich_read_concurrency:
            raise ValueError("rich read subject concurrency exceeds the process ceiling")
        if self.stream_concurrency_per_subject > self.stream_concurrency:
            raise ValueError("stream subject concurrency exceeds the process ceiling")


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


class _SubjectAdmission:
    """Non-queuing admission partitioned by subject under one aggregate ceiling."""

    def __init__(self, *, process_limit: int, subject_limit: int) -> None:
        self._process_limit = process_limit
        self._subject_limit = subject_limit
        self._active = 0
        self._subjects: dict[str, int] = {}

    def acquire(self, subject: str) -> bool:
        subject_active = self._subjects.get(subject, 0)
        if self._active >= self._process_limit or subject_active >= self._subject_limit:
            return False
        self._active += 1
        self._subjects[subject] = subject_active + 1
        return True

    def release(self, subject: str) -> None:
        subject_active = self._subjects.get(subject, 0)
        if subject_active <= 0 or self._active <= 0:
            raise RuntimeError("subject admission released without ownership")
        self._active -= 1
        if subject_active == 1:
            del self._subjects[subject]
        else:
            self._subjects[subject] = subject_active - 1


class _SubjectBoundedRuntimeCalls:
    """Deadline-bound calls using subject partitions and a process-wide ceiling."""

    def __init__(
        self, *, process_limit: int, subject_limit: int, deadline_seconds: float, name: str
    ) -> None:
        self._deadline_seconds = deadline_seconds
        self._admission = _SubjectAdmission(
            process_limit=process_limit, subject_limit=subject_limit
        )
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._name = name

    async def call(self, subject: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        if self._closed or not self._admission.acquire(subject):
            raise RuntimeCallTimedOut
        task = asyncio.create_task(operation(), name=self._name)
        self._tasks.add(task)
        try:
            async with asyncio.timeout(self._deadline_seconds):
                return await task
        except TimeoutError as exc:
            raise RuntimeCallTimedOut from exc
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._tasks.discard(task)
            self._admission.release(subject)

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
_COMMAND_INPUT_KEYS = frozenset({"schemaVersion", "correlationId", "expectedRevision"})
_COMMAND_ENVELOPE_KEYS = frozenset(
    {"schemaVersion", "contractVersion", "instanceId", "command", "correlationId", "result"}
)
_COMMAND_RESULT_KEYS = frozenset({"status", "revision"})
_SNAPSHOT_KEYS = frozenset({"schemaVersion", "revision", "generatedAt", "workItems", "agents"})
_STREAM_SNAPSHOT_KEYS = _SNAPSHOT_KEYS | {"eventCursor"}
_WORK_ITEM_KEYS = frozenset(
    {"id", "title", "status", "issueType", "priority", "labels", "assignee", "updatedAt"}
)
_AGENT_KEYS = frozenset({"id", "state", "ownerSeat", "startedAt", "updatedAt", "endedAt"})
_MAX_WORK_ITEMS = 1_000
_MAX_AGENTS = 256
_MAX_LABELS = 64
_MAX_JSON_SAFE_INTEGER = 2**53 - 1
_MAX_COMMAND_BODY = 2_048
_CORRELATION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_REVISION = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EVENT_CURSOR = re.compile(
    r"(?P<epoch>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r":(?P<sequence>0|[1-9][0-9]{0,15})\Z"
)


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
                and item["capabilities"]
                in (
                    ["snapshot"],
                    ["snapshot", "refresh"],
                    ["snapshot", "events"],
                    ["snapshot", "refresh", "events"],
                )
                for item in items
            )
        )
    if kind == "snapshot":
        if not _exact_keys(payload, _ENVELOPE_KEYS):
            return False
        snapshot = payload["snapshot"]
        if not (
            _exact_keys(snapshot, _SNAPSHOT_KEYS) or _exact_keys(snapshot, _STREAM_SNAPSHOT_KEYS)
        ):
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
            and (
                "eventCursor" not in snapshot
                or (
                    isinstance(snapshot["eventCursor"], str)
                    and _EVENT_CURSOR.fullmatch(snapshot["eventCursor"]) is not None
                )
            )
        )
    if kind == "commandResult":
        if not _exact_keys(payload, _COMMAND_ENVELOPE_KEYS):
            return False
        result = payload["result"]
        return (
            _schema_version(payload["schemaVersion"])
            and payload["contractVersion"] == CONTRACT_VERSION
            and payload["instanceId"] == DEVELOPMENT_INSTANCE_ID
            and payload["command"] == "refresh"
            and isinstance(payload["correlationId"], str)
            and _CORRELATION_ID.fullmatch(payload["correlationId"]) is not None
            and _exact_keys(result, _COMMAND_RESULT_KEYS)
            and result["status"] == "completed"
            and isinstance(result["revision"], str)
            and _REVISION.fullmatch(result["revision"]) is not None
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


def _public_snapshot(raw: Mapping[str, object], *, with_events: bool) -> dict[str, object]:
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
        if with_events:
            event_cursor = raw["eventCursor"]
            if not isinstance(event_cursor, str) or _EVENT_CURSOR.fullmatch(event_cursor) is None:
                raise RemoteProjectionFailed("runtime snapshot is incompatible")
            public["eventCursor"] = event_cursor
    except (KeyError, TypeError) as exc:
        raise RemoteProjectionFailed("runtime snapshot is incompatible") from exc
    expected_keys = _STREAM_SNAPSHOT_KEYS if with_events else _SNAPSHOT_KEYS
    if not _exact_keys(public, expected_keys):
        raise RemoteProjectionFailed("runtime snapshot is incompatible")
    return public


async def _read_public_snapshot(instance: RemoteInstance) -> dict[str, object]:
    return _public_snapshot(await instance.snapshot(), with_events=instance.events is not None)


async def _read_command_input(request: Request) -> dict[str, object]:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip() != "application/json":
        raise ValueError
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_COMMAND_BODY:
            raise ValueError
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError from exc
    if not _exact_keys(value, _COMMAND_INPUT_KEYS):
        raise ValueError
    correlation_id = value["correlationId"]
    expected_revision = value["expectedRevision"]
    if (
        not _schema_version(value["schemaVersion"])
        or not isinstance(correlation_id, str)
        or _CORRELATION_ID.fullmatch(correlation_id) is None
        or not isinstance(expected_revision, str)
        or _REVISION.fullmatch(expected_revision) is None
    ):
        raise ValueError
    return value


async def _invoke_refresh(
    instance: RemoteInstance, expected_revision: str, correlation_id: str
) -> dict[str, object]:
    if instance.refresh is None:
        raise LookupError
    raw = await instance.refresh(expected_revision, correlation_id)
    try:
        public = {"status": raw["status"], "revision": raw["revision"]}
    except (KeyError, TypeError) as exc:
        raise RemoteProjectionFailed("runtime command result is incompatible") from exc
    if (
        public["status"] != "completed"
        or not isinstance(public["revision"], str)
        or _REVISION.fullmatch(public["revision"]) is None
    ):
        raise RemoteProjectionFailed("runtime command result is incompatible")
    return public


def build_development_gateway_application(
    *,
    config: DevelopmentGatewayConfig,
    verifier: ClerkTokenVerifier,
    registry: DevelopmentInstanceRegistry,
    runtime_calls: RuntimeCallPolicy | None = None,
    read_source: gateway_read_mod.GatewayReadSource | None = None,
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
    command_calls = _BoundedRuntimeCalls(
        concurrency=runtime_calls.command_concurrency,
        deadline_seconds=runtime_calls.deadline_seconds,
        name="beadhive-gateway-command",
    )
    stream_open_calls = _BoundedRuntimeCalls(
        concurrency=runtime_calls.stream_concurrency,
        deadline_seconds=runtime_calls.deadline_seconds,
        name="beadhive-gateway-stream-open",
    )
    rich_read_calls = _SubjectBoundedRuntimeCalls(
        process_limit=runtime_calls.rich_read_concurrency,
        subject_limit=runtime_calls.rich_read_concurrency_per_subject,
        deadline_seconds=runtime_calls.deadline_seconds,
        name="beadhive-gateway-rich-read",
    )
    stream_admission = _SubjectAdmission(
        process_limit=runtime_calls.stream_concurrency,
        subject_limit=runtime_calls.stream_concurrency_per_subject,
    )
    active_streams: set[asyncio.Task[Any]] = set()

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

    def authorize_bridge(request: Request) -> str:
        subject = authorize(request)
        instance_id = f"{request.path_params['stage']}/{request.path_params['slug']}"
        if instance_id != gateway_read_mod.INSTANCE_ID or instance_id not in registry.authorized(
            subject
        ):
            raise gateway_read_mod.ReadSourceNotFound
        return subject

    def rich_error(
        code: str, message: str, status_code: int, *, retryable: bool = False
    ) -> JSONResponse:
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if code == "rate_limited" or (status_code == 503 and retryable):
            headers["Retry-After"] = "1"
        return JSONResponse(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "requestId": "req_" + secrets.token_urlsafe(12),
                    "details": {},
                }
            },
            status_code=status_code,
            headers=headers,
        )

    def rich_response(request: Request, payload: Mapping[str, object]) -> Response:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        boundary = read_source.cache_boundary.encode() if read_source is not None else b""
        etag = '"sha256:' + hashlib.sha256(boundary + b"\0" + encoded).hexdigest() + '"'
        conditional = request.headers.getlist("if-none-match")
        headers = {
            "Cache-Control": "private, max-age=0, must-revalidate",
            "ETag": etag,
            "Vary": "Authorization, Origin, Accept",
            "X-Content-Type-Options": "nosniff",
        }
        if conditional:
            if len(conditional) != 1 or conditional[0] != etag:
                if len(conditional) != 1 or len(conditional[0]) > 256:
                    raise gateway_read_mod.ReadSourceInvalidRequest
            else:
                return Response(status_code=304, headers=headers)
        return JSONResponse(payload, headers=headers)

    def bridge_hive_id(request: Request, suffix: str) -> str:
        raw_path = request.scope.get("raw_path", b"")
        if not isinstance(raw_path, bytes):
            raise gateway_read_mod.ReadSourceInvalidRequest
        pattern = (
            rb"/v1/instances/dev/demo/hives/"
            rb"([A-Za-z0-9._~-]+%2F[A-Za-z0-9._~-]+%2F[A-Za-z0-9._~-]+)" + suffix.encode() + rb"\Z"
        )
        match = re.fullmatch(pattern, raw_path.split(b"?", 1)[0])
        if match is None:
            raise gateway_read_mod.ReadSourceInvalidRequest
        hive_id = request.path_params.get("hive_id")
        if not isinstance(hive_id, str) or hive_id != match.group(1).decode().replace("%2F", "/"):
            raise gateway_read_mod.ReadSourceInvalidRequest
        return hive_id

    async def bridge_hives(request: Request) -> Response:
        try:
            if read_source is None:
                raise gateway_read_mod.ReadSourceNotFound
            subject = authorize_bridge(request)
            if set(request.query_params) - {"limit", "after"}:
                raise gateway_read_mod.ReadSourceInvalidRequest
            limits = request.query_params.getlist("limit")
            if len(limits) > 1:
                raise gateway_read_mod.ReadSourceInvalidRequest
            raw_limit = limits[0] if limits else "50"
            if not raw_limit.isdecimal() or len(raw_limit) > 3 or str(int(raw_limit)) != raw_limit:
                raise gateway_read_mod.ReadSourceInvalidRequest
            after_values = request.query_params.getlist("after")
            if len(after_values) > 1 or (after_values and not 1 <= len(after_values[0]) <= 2048):
                raise gateway_read_mod.ReadSourceInvalidRequest
            page = await rich_read_calls.call(
                subject,
                lambda: read_source.list_hives(
                    subject,
                    limit=int(raw_limit),
                    after=after_values[0] if after_values else None,
                ),
            )
            return rich_response(request, page)
        except PermissionError:
            return rich_error("request_denied", "The request is not allowed.", 403)
        except AuthenticationFailed:
            return rich_error("authentication_failed", "Authentication failed.", 401)
        except gateway_read_mod.ReadSourceNotFound:
            return rich_error("resource_not_found", "The resource was not found.", 404)
        except gateway_read_mod.ReadSourceInvalidRequest:
            return rich_error("invalid_request", "The request is not valid.", 400)
        except gateway_read_mod.ReadSourceResnapshotRequired:
            return rich_error(
                "resnapshot_required", "A fresh snapshot is required.", 409, retryable=True
            )
        except RuntimeCallTimedOut:
            return rich_error("rate_limited", "The read limit was exceeded.", 429, retryable=True)
        except Exception:
            return rich_error(
                "read_plane_unavailable", "The read plane is unavailable.", 503, retryable=True
            )

    async def bridge_snapshot(request: Request) -> Response:
        try:
            if read_source is None:
                raise gateway_read_mod.ReadSourceNotFound
            subject = authorize_bridge(request)
            hive_id = bridge_hive_id(request, "/snapshot")
            if set(request.query_params) - {"detail"}:
                raise gateway_read_mod.ReadSourceInvalidRequest
            details = request.query_params.getlist("detail")
            if details not in ([], ["live"]):
                raise gateway_read_mod.ReadSourceInvalidRequest
            envelope = await rich_read_calls.call(
                subject,
                lambda: read_source.snapshot(
                    subject,
                    factory_id=gateway_read_mod.FACTORY_ID,
                    hive_id=hive_id,
                    detail="live",
                ),
            )
            return rich_response(request, envelope)
        except PermissionError:
            return rich_error("request_denied", "The request is not allowed.", 403)
        except AuthenticationFailed:
            return rich_error("authentication_failed", "Authentication failed.", 401)
        except gateway_read_mod.ReadSourceNotFound:
            return rich_error("resource_not_found", "The resource was not found.", 404)
        except gateway_read_mod.ReadSourceInvalidRequest:
            return rich_error("invalid_request", "The request is not valid.", 400)
        except RuntimeCallTimedOut:
            return rich_error("rate_limited", "The read limit was exceeded.", 429, retryable=True)
        except Exception:
            return rich_error(
                "snapshot_unavailable", "The snapshot is unavailable.", 503, retryable=False
            )

    async def bridge_events(request: Request) -> Response:
        admitted_subject: str | None = None
        try:
            if read_source is None:
                raise gateway_read_mod.ReadSourceNotFound
            subject = authorize_bridge(request)
            hive_id = bridge_hive_id(request, "/events")
            if set(request.query_params) - {"subscription", "after"}:
                raise gateway_read_mod.ReadSourceInvalidRequest
            subscriptions = request.query_params.getlist("subscription")
            if (
                len(subscriptions) != 1
                or not 1 <= len(subscriptions[0]) <= 512
                or subscriptions[0].strip() != subscriptions[0]
            ):
                raise gateway_read_mod.ReadSourceInvalidRequest
            after_values = request.query_params.getlist("after")
            header_values = request.headers.getlist("last-event-id")
            if len(after_values) > 1 or len(header_values) > 1:
                raise gateway_read_mod.ReadSourceInvalidRequest
            if after_values and header_values and after_values[0] != header_values[0]:
                raise gateway_read_mod.ReadSourceInvalidRequest
            after = after_values[0] if after_values else header_values[0] if header_values else None
            if after is not None and not 1 <= len(after) <= 512:
                raise gateway_read_mod.ReadSourceInvalidRequest
            if not stream_admission.acquire(subject):
                return rich_error(
                    "rate_limited", "The stream limit was exceeded.", 429, retryable=True
                )
            admitted_subject = subject
            source = await rich_read_calls.call(
                subject,
                lambda: read_source.events(
                    subject,
                    factory_id=gateway_read_mod.FACTORY_ID,
                    hive_id=hive_id,
                    subscription=subscriptions[0],
                    after=after,
                ),
            )
            if not hasattr(source, "__aiter__"):
                raise RemoteProjectionFailed("gateway read event stream is incompatible")

            async def stream():
                owner = asyncio.current_task()
                iterator = None
                next_event = None
                previous_sequence = int(after.rsplit(":", 1)[1]) if after is not None else None
                expected_epoch = after.rsplit(":", 1)[0] if after is not None else None
                if owner is not None:
                    active_streams.add(owner)
                try:
                    iterator = source.__aiter__()
                    next_event = asyncio.create_task(
                        anext(iterator), name="beadhive-gateway-rich-event-next"
                    )
                    while True:
                        done, _ = await asyncio.wait(
                            {next_event}, timeout=runtime_calls.stream_reauthorize_seconds
                        )
                        try:
                            current_subject = authorize_bridge(request)
                        except (
                            AuthenticationFailed,
                            PermissionError,
                            gateway_read_mod.ReadSourceNotFound,
                        ):
                            return
                        if current_subject != subject:
                            return
                        if not done:
                            yield ": keep-alive\n\n"
                            continue
                        try:
                            envelope = next_event.result()
                        except StopAsyncIteration:
                            yield 'event: resnapshot-required\ndata: {"schemaVersion":1}\n\n'
                            return
                        if (
                            not isinstance(envelope, Mapping)
                            or envelope.get("schemaVersion") != gateway_read_mod.SCHEMA_VERSION
                            or envelope.get("contractVersion") != gateway_read_mod.CONTRACT_VERSION
                            or envelope.get("instanceId") != gateway_read_mod.INSTANCE_ID
                            or envelope.get("factoryId") != gateway_read_mod.FACTORY_ID
                            or envelope.get("hiveId") != hive_id
                            or envelope.get("detailLevel") != "live"
                        ):
                            raise RemoteProjectionFailed(
                                "gateway read event envelope is incompatible"
                            )
                        event = envelope.get("event")
                        if not isinstance(event, Mapping):
                            raise RemoteProjectionFailed("gateway read event is incompatible")
                        epoch = event.get("producerEpoch")
                        sequence = event.get("sequence")
                        base_sequence = event.get("baseSequence")
                        if (
                            not isinstance(epoch, str)
                            or not epoch
                            or type(sequence) is not int
                            or type(base_sequence) is not int
                            or sequence != base_sequence + 1
                            or (expected_epoch is not None and epoch != expected_epoch)
                            or (
                                previous_sequence is not None and base_sequence != previous_sequence
                            )
                            or event.get("subscriptionId") != subscriptions[0]
                            or event.get("hiveId") != hive_id
                        ):
                            raise RemoteProjectionFailed("gateway read cursor is incompatible")
                        expected_epoch = epoch
                        previous_sequence = sequence
                        cursor = f"{epoch}:{sequence}"
                        data = json.dumps(envelope, separators=(",", ":"))
                        next_event = asyncio.create_task(
                            anext(iterator), name="beadhive-gateway-rich-event-next"
                        )
                        yield f"id: {cursor}\nevent: operator-event\ndata: {data}\n\n"
                except Exception:
                    yield 'event: resnapshot-required\ndata: {"schemaVersion":1}\n\n'
                finally:
                    try:
                        if next_event is not None:
                            if not next_event.done():
                                next_event.cancel()
                            await asyncio.gather(next_event, return_exceptions=True)
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()
                    finally:
                        stream_admission.release(subject)
                        if owner is not None:
                            active_streams.discard(owner)

            admitted_subject = None
            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store, no-transform",
                    "X-Accel-Buffering": "no",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except PermissionError:
            return rich_error("request_denied", "The request is not allowed.", 403)
        except AuthenticationFailed:
            return rich_error("authentication_failed", "Authentication failed.", 401)
        except gateway_read_mod.ReadSourceNotFound:
            return rich_error("resource_not_found", "The resource was not found.", 404)
        except gateway_read_mod.ReadSourceInvalidRequest:
            return rich_error("invalid_request", "The request is not valid.", 400)
        except gateway_read_mod.ReadSourceResnapshotRequired:
            return rich_error(
                "resnapshot_required", "A fresh snapshot is required.", 409, retryable=True
            )
        except RuntimeCallTimedOut:
            return rich_error("rate_limited", "The stream limit was exceeded.", 429, retryable=True)
        except Exception:
            return rich_error(
                "read_plane_unavailable", "The read plane is unavailable.", 503, retryable=True
            )
        finally:
            if admitted_subject is not None:
                stream_admission.release(admitted_subject)

    async def read_availability(calls: _BoundedRuntimeCalls, instance: RemoteInstance) -> bool:
        availability = await calls.call(instance.online)
        if type(availability) is not bool:
            raise RemoteProjectionFailed("runtime availability is incompatible")
        return availability

    async def public_instance(instance_id: str, instance: RemoteInstance) -> dict[str, object]:
        availability = await read_availability(discovery_availability_calls, instance)
        capabilities = ["snapshot"]
        if instance.refresh is not None:
            capabilities.append("refresh")
        if instance.events is not None:
            capabilities.append("events")
        return {
            "id": instance_id,
            "displayName": instance.display_name,
            "availability": "online" if availability else "offline",
            "capabilities": capabilities,
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

    async def refresh(request: Request) -> JSONResponse:
        try:
            subject = authorize(request)
            instance_id = f"{request.path_params['stage']}/{request.path_params['slug']}"
            instance = registry.authorized(subject).get(instance_id)
            if instance is None or instance.refresh is None:
                return _error("resource_not_found", "The resource was not found.", 404)
            command_input = await _read_command_input(request)
            expected_revision = command_input["expectedRevision"]
            correlation_id = command_input["correlationId"]
            assert isinstance(expected_revision, str)
            assert isinstance(correlation_id, str)
            result = await command_calls.call(
                lambda: _invoke_refresh(instance, expected_revision, correlation_id)
            )
            return _response(
                "commandResult",
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "contractVersion": CONTRACT_VERSION,
                    "instanceId": instance_id,
                    "command": "refresh",
                    "correlationId": correlation_id,
                    "result": result,
                },
            )
        except PermissionError:
            return _error("request_denied", "The request is not allowed.", 403)
        except AuthenticationFailed:
            return _error("authentication_failed", "Authentication failed.", 401)
        except ValueError:
            return _error("invalid_request", "The request is not valid.", 400)
        except StaleCommandScope:
            return _error("scope_conflict", "The command scope is stale.", 409)
        except (RemoteProjectionFailed, RuntimeCallTimedOut):
            return _error("runtime_unavailable", "The runtime is unavailable.", 503, retryable=True)
        except Exception:
            return _error("runtime_unavailable", "The runtime is unavailable.", 503, retryable=True)

    async def events(request: Request) -> Response:
        try:
            subject = authorize(request)
            instance_id = f"{request.path_params['stage']}/{request.path_params['slug']}"
            instance = registry.authorized(subject).get(instance_id)
            if instance is None or instance.events is None:
                return _error("resource_not_found", "The resource was not found.", 404)
            if set(request.query_params) != {"cursor"}:
                return _error("invalid_request", "The request is not valid.", 400)
            cursors = request.query_params.getlist("cursor")
            if len(cursors) != 1 or _EVENT_CURSOR.fullmatch(cursors[0]) is None:
                return _error("invalid_request", "The request is not valid.", 400)
            cursor = cursors[0]
            match = _EVENT_CURSOR.fullmatch(cursor)
            assert match is not None
            epoch = match.group("epoch")
            sequence = int(match.group("sequence"))
            if not stream_admission.acquire(subject):
                return _error(
                    "runtime_unavailable", "The runtime is unavailable.", 503, retryable=True
                )
            try:
                source = await stream_open_calls.call(lambda: instance.events(cursor))
            except BaseException:
                stream_admission.release(subject)
                raise
            if not hasattr(source, "__aiter__"):
                stream_admission.release(subject)
                raise RemoteProjectionFailed("runtime event stream is incompatible")

            async def stream():
                nonlocal sequence
                owner = asyncio.current_task()
                if owner is not None:
                    active_streams.add(owner)
                iterator = None
                next_event = None
                try:
                    iterator = source.__aiter__()
                    next_event = asyncio.create_task(
                        anext(iterator), name="beadhive-gateway-event-next"
                    )
                    while True:
                        done, _ = await asyncio.wait(
                            {next_event}, timeout=runtime_calls.stream_reauthorize_seconds
                        )
                        try:
                            current_subject = authorize(request)
                        except (AuthenticationFailed, PermissionError):
                            return
                        current = registry.authorized(current_subject).get(instance_id)
                        if current is None or current.events is not instance.events:
                            return
                        if not done:
                            continue
                        try:
                            raw = next_event.result()
                        except StopAsyncIteration:
                            return
                        try:
                            next_cursor = raw["cursor"]
                            revision = raw["revision"]
                        except (KeyError, TypeError):
                            yield 'event: resnapshot-required\ndata: {"schemaVersion":1}\n\n'
                            return
                        next_match = (
                            _EVENT_CURSOR.fullmatch(next_cursor)
                            if isinstance(next_cursor, str)
                            else None
                        )
                        if (
                            next_match is None
                            or next_match.group("epoch") != epoch
                            or int(next_match.group("sequence")) != sequence + 1
                            or not isinstance(revision, str)
                            or _REVISION.fullmatch(revision) is None
                        ):
                            yield 'event: resnapshot-required\ndata: {"schemaVersion":1}\n\n'
                            return
                        sequence += 1
                        data = json.dumps(
                            {
                                "schemaVersion": SCHEMA_VERSION,
                                "contractVersion": CONTRACT_VERSION,
                                "instanceId": instance_id,
                                "cursor": next_cursor,
                                "event": {
                                    "type": "snapshot-invalidated",
                                    "revision": revision,
                                },
                            },
                            separators=(",", ":"),
                        )
                        next_event = asyncio.create_task(
                            anext(iterator), name="beadhive-gateway-event-next"
                        )
                        yield f"id: {next_cursor}\nevent: snapshot-invalidated\ndata: {data}\n\n"
                finally:
                    try:
                        if next_event is not None:
                            if not next_event.done():
                                next_event.cancel()
                            await asyncio.gather(next_event, return_exceptions=True)
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()
                    finally:
                        stream_admission.release(subject)
                        if owner is not None:
                            active_streams.discard(owner)

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except PermissionError:
            return _error("request_denied", "The request is not allowed.", 403)
        except AuthenticationFailed:
            return _error("authentication_failed", "Authentication failed.", 401)
        except StaleEventCursor:
            return _error("resnapshot_required", "A fresh snapshot is required.", 409)
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

    async def command_preflight(request: Request) -> Response:
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
            or methods != ["POST"]
            or normalized != {"authorization", "content-type"}
        ):
            return _error("request_denied", "The request is not allowed.", 403)
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": config.app_origin,
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
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

    async def health(request: Request) -> JSONResponse:
        if request.headers.getlist("host") != [gateway_host] or request.headers.getlist("origin"):
            return _error("request_denied", "The request is not allowed.", 403)
        return JSONResponse(
            {"live": True, "contractVersion": CONTRACT_VERSION},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            yield
        finally:
            streams = tuple(active_streams)
            for task in streams:
                task.cancel()
            await asyncio.gather(*streams, return_exceptions=True)
            await asyncio.gather(
                snapshot_calls.close(),
                rich_read_calls.close(),
                command_calls.close(),
                stream_open_calls.close(),
                snapshot_availability_calls.close(),
                discovery_availability_calls.close(),
            )
            close_operations = {
                instance.close for instance in registry.instances.values() if instance.close
            }
            await asyncio.gather(*(close() for close in close_operations), return_exceptions=True)

    app = Starlette(
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/v1/instances", instances, methods=["GET"]),
            Route("/v1/instances/{stage}/{slug}/hives", bridge_hives, methods=["GET"]),
            Route(
                "/v1/instances/{stage}/{slug}/hives/{hive_id:path}/snapshot",
                bridge_snapshot,
                methods=["GET"],
            ),
            Route(
                "/v1/instances/{stage}/{slug}/hives/{hive_id:path}/events",
                bridge_events,
                methods=["GET"],
            ),
            Route("/v1/instances/{stage}/{slug}/snapshot", snapshot, methods=["GET"]),
            Route("/v1/instances/{stage}/{slug}/events", events, methods=["GET"]),
            Route("/v1/instances/{stage}/{slug}/commands/refresh", refresh, methods=["POST"]),
            Route("/v1/instances/{stage}/{slug}/commands/{command}", absent, methods=["POST"]),
            Route("/v1/instances", preflight, methods=["OPTIONS"]),
            Route("/v1/instances/{stage}/{slug}/hives", preflight, methods=["OPTIONS"]),
            Route(
                "/v1/instances/{stage}/{slug}/hives/{hive_id:path}/snapshot",
                preflight,
                methods=["OPTIONS"],
            ),
            Route(
                "/v1/instances/{stage}/{slug}/hives/{hive_id:path}/events",
                preflight,
                methods=["OPTIONS"],
            ),
            Route("/v1/instances/{stage}/{slug}/snapshot", preflight, methods=["OPTIONS"]),
            Route("/v1/instances/{stage}/{slug}/events", preflight, methods=["OPTIONS"]),
            Route(
                "/v1/instances/{stage}/{slug}/commands/refresh",
                command_preflight,
                methods=["OPTIONS"],
            ),
            Route("/{path:path}", absent, methods=["GET"]),
            Route("/{path:path}", absent_preflight, methods=["OPTIONS"]),
        ],
        lifespan=lifespan,
    )

    async def cors_and_read_only(request: Request, call_next):
        is_command = (
            request.url.path.startswith("/v1/instances/") and "/commands/" in request.url.path
        )
        if request.method not in {"GET", "OPTIONS"} and not (
            request.method == "POST" and is_command
        ):
            response = _error("read_only_profile", "The gateway is read-only.", 405)
        else:
            response = await call_next(request)
        if request.headers.get("origin") == config.app_origin:
            response.headers["Access-Control-Allow-Origin"] = config.app_origin
            if "vary" not in response.headers:
                response.headers["Vary"] = "Origin"
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=cors_and_read_only)

    return app
