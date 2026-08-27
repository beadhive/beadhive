"""Authenticated, read-only Development gateway profile.

This module is deliberately separate from :mod:`beadhive.operator_api`: the local profile keeps
its loopback-only, unauthenticated contract while this boundary authenticates and projects a
small, explicitly allowlisted remote representation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
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
    snapshot: Callable[[], Mapping[str, object]]
    online: Callable[[], bool] = lambda: True


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


def _exact_keys(value: object, expected: frozenset[str]) -> bool:
    return isinstance(value, dict) and set(value) == expected


def remote_payload_is_allowlisted(kind: str, payload: object) -> bool:
    """Return whether *payload* is exactly one public remote wire shape."""
    if kind == "error":
        return (
            _exact_keys(payload, _ERROR_KEYS) and _exact_keys(payload["error"], _ERROR_DETAIL_KEYS)  # type: ignore[index]
        )
    if kind == "instances":
        return (
            _exact_keys(payload, _INSTANCE_PAGE_KEYS)
            and isinstance(payload["items"], list)  # type: ignore[index]
            and all(_exact_keys(item, _INSTANCE_KEYS) for item in payload["items"])  # type: ignore[index]
        )
    if kind == "snapshot":
        if not _exact_keys(payload, _ENVELOPE_KEYS):
            return False
        snapshot = payload["snapshot"]  # type: ignore[index]
        return (
            _exact_keys(snapshot, _SNAPSHOT_KEYS)
            and isinstance(snapshot["workItems"], list)  # type: ignore[index]
            and all(_exact_keys(item, _WORK_ITEM_KEYS) for item in snapshot["workItems"])  # type: ignore[index]
            and isinstance(snapshot["agents"], list)  # type: ignore[index]
            and all(_exact_keys(item, _AGENT_KEYS) for item in snapshot["agents"])  # type: ignore[index]
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
        if raw["schemaVersion"] != SCHEMA_VERSION:
            raise RemoteProjectionFailed("runtime snapshot is incompatible")
        revision = raw["revision"]
        generated_at = raw["generatedAt"]
        if not isinstance(revision, str) or not revision:
            raise RemoteProjectionFailed("runtime snapshot is incompatible")
        if not isinstance(generated_at, int) or isinstance(generated_at, bool):
            raise RemoteProjectionFailed("runtime snapshot is incompatible")
        work_items = []
        for item in raw["workItems"]:  # type: ignore[union-attr]
            record = item["record"]
            work_items.append(
                {
                    "id": record["id"],
                    "title": record["title"],
                    "status": record["status"],
                    "issueType": record["issueType"],
                    "priority": record["priority"],
                    "labels": list(record["labels"]),
                    "assignee": record["assignee"],
                    "updatedAt": item["updatedAt"],
                }
            )
        agents = []
        for item in raw["agents"]:  # type: ignore[union-attr]
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


def build_development_gateway_application(
    *,
    config: DevelopmentGatewayConfig,
    verifier: ClerkTokenVerifier,
    registry: DevelopmentInstanceRegistry,
) -> Starlette:
    """Build the remote Development read profile without mutating the loopback application."""
    gateway_host = urlsplit(config.gateway_origin).netloc

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
                {
                    "id": instance_id,
                    "displayName": instance.display_name,
                    "availability": "online" if instance.online() else "offline",
                    "capabilities": ["snapshot"],
                }
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
            if not instance.online():
                return _error(
                    "runtime_unavailable", "The runtime is unavailable.", 503, retryable=True
                )
            projected = _public_snapshot(instance.snapshot())
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

    app = Starlette(
        routes=[
            Route("/v1/instances", instances, methods=["GET"]),
            Route("/v1/instances/{stage}/{slug}/snapshot", snapshot, methods=["GET"]),
            Route("/v1/instances", preflight, methods=["OPTIONS"]),
            Route("/v1/instances/{stage}/{slug}/snapshot", preflight, methods=["OPTIONS"]),
            Route("/{path:path}", absent, methods=["GET"]),
            Route("/{path:path}", absent_preflight, methods=["OPTIONS"]),
        ]
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
