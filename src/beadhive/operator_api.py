"""Phase-one loopback-only read API for the unified host application."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
from collections.abc import Callable, Sequence
from importlib import resources
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import operator_contract
from .operator_feed import OperatorFeed
from .operator_sources import OperatorSourceError, OperatorSources, validate_canonical_identity

OPENAPI_CONTRACT = "beadhive-host-openapi-v1.json"
_CURSOR = re.compile(r"^([A-Za-z0-9._~-]+):(0|[1-9][0-9]*)$")
_EVENTS_RAW_PATH = re.compile(
    rb"^/api/v1/hives/[A-Za-z0-9._~-]+%2F[A-Za-z0-9._~-]+%2F"
    rb"[A-Za-z0-9._~-]+/events$"
)


def error_payload(error: OperatorSourceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        }
    }


def _error_response(error: OperatorSourceError) -> JSONResponse:
    return JSONResponse(error_payload(error), status_code=error.status_code)


def openapi_document() -> dict[str, Any]:
    text = (
        resources.files("beadhive")
        .joinpath("schemas", OPENAPI_CONTRACT)
        .read_text(encoding="utf-8")
    )
    value = json.loads(text)
    if not isinstance(value, dict):
        raise RuntimeError("operator OpenAPI root must be an object")
    return value


def _raw_parameter(request: Request, prefix: bytes, suffix: bytes) -> bytes:
    raw_path = request.scope.get("raw_path")
    if not isinstance(raw_path, bytes):
        raw_path = request.scope["path"].encode("utf-8")
    if not raw_path.startswith(prefix) or not raw_path.endswith(suffix):
        raise OperatorSourceError(
            "invalid_path_encoding",
            "The resource identity does not use its canonical path encoding.",
            status_code=400,
        )
    return raw_path[len(prefix) : len(raw_path) - len(suffix)]


def canonical_hive_parameter(request: Request, *, suffix: bytes = b"/snapshot") -> str:
    decoded = str(request.path_params["hive_id"])
    validate_canonical_identity(decoded)
    raw = _raw_parameter(request, b"/api/v1/hives/", suffix)
    expected = quote(decoded, safe="-._~").encode("ascii")
    if raw != expected:
        raise OperatorSourceError(
            "invalid_path_encoding",
            "Hive identity must use one uppercase percent-encoded canonical representation.",
            status_code=400,
        )
    return decoded


def canonical_run_parameter(request: Request) -> str:
    decoded = str(request.path_params["run_id"])
    raw = _raw_parameter(request, b"/api/v1/runs/", b"/activity")
    try:
        expected = quote(decoded, safe="-._~").encode("ascii")
    except UnicodeEncodeError as exc:
        raise OperatorSourceError(
            "invalid_run_id",
            "Run identity must be one path-safe outer run token.",
            status_code=400,
        ) from exc
    if raw != expected:
        raise OperatorSourceError(
            "invalid_path_encoding",
            "Run identity must use one canonical path representation.",
            status_code=400,
        )
    return decoded


def activity_cursor(request: Request) -> tuple[str, int] | None:
    values = request.query_params.getlist("after")
    if not values:
        return None
    if len(values) != 1:
        raise OperatorSourceError(
            "invalid_activity_cursor",
            "Activity requests accept at most one exact cursor.",
            status_code=400,
        )
    match = _CURSOR.fullmatch(values[0])
    if match is None:
        raise OperatorSourceError(
            "invalid_activity_cursor",
            "Activity cursor must be producerEpoch:sequence.",
            status_code=400,
        )
    return match.group(1), int(match.group(2))


class OperatorAPI:
    def __init__(
        self,
        *,
        sources: OperatorSources,
        feed: OperatorFeed,
        host_id: str,
        instance_id: str,
        ready: Callable[[], bool],
        events: Callable[[Request], Any] | None = None,
    ) -> None:
        self.sources = sources
        self.feed = feed
        self.host_id = host_id
        self.instance_id = instance_id
        self.ready = ready
        self.events = events

    async def factory(self, _request: Request) -> JSONResponse:
        try:
            hives = await asyncio.to_thread(self.sources.registered_hives)
            payload = operator_contract.factory_snapshot(
                [hive.entry for hive in hives],
                generated_at=time.time_ns() // 1_000_000,
                host_id=self.host_id,
                instance_id=self.instance_id,
                ready=self.ready(),
            )
            return JSONResponse(payload)
        except OperatorSourceError as exc:
            return _error_response(exc)
        except Exception:
            return _error_response(
                OperatorSourceError(
                    "factory_source_unavailable",
                    "The authoritative factory source is unavailable.",
                    status_code=503,
                    retryable=True,
                )
            )

    async def snapshot(self, request: Request) -> JSONResponse:
        try:
            identity = canonical_hive_parameter(request)
            payload = await asyncio.to_thread(self.feed.snapshot_with_cursor, identity)
            return JSONResponse(payload)
        except OperatorSourceError as exc:
            return _error_response(exc)
        except Exception:
            return _error_response(
                OperatorSourceError(
                    "snapshot_source_unavailable",
                    "The authoritative hive snapshot source is unavailable.",
                    status_code=503,
                    retryable=True,
                )
            )

    async def activity(self, request: Request) -> JSONResponse:
        try:
            run_id = canonical_run_parameter(request)
            after = activity_cursor(request)
            payload = await asyncio.to_thread(self.feed.activity_with_cursor, run_id, after=after)
            return JSONResponse(payload)
        except OperatorSourceError as exc:
            return _error_response(exc)
        except Exception:
            return _error_response(
                OperatorSourceError(
                    "activity_source_unavailable",
                    "The authoritative run activity source is unavailable.",
                    status_code=503,
                    retryable=True,
                )
            )

    async def openapi(self, _request: Request) -> JSONResponse:
        return JSONResponse(openapi_document())

    def routes(self) -> list[BaseRoute]:
        routes: list[BaseRoute] = [
            Route("/api/v1/factory", self.factory, methods=["GET"], name="operator_factory"),
            Route(
                "/api/v1/hives/{hive_id:path}/snapshot",
                self.snapshot,
                methods=["GET"],
                name="operator_hive_snapshot",
            ),
            Route(
                "/api/v1/runs/{run_id}/activity",
                self.activity,
                methods=["GET"],
                name="operator_run_activity",
            ),
        ]
        if self.events is not None:
            routes.append(
                Route(
                    "/api/v1/hives/{hive_id:path}/events",
                    self.events,
                    methods=["GET"],
                    name="operator_hive_events",
                )
            )
        routes.append(
            Route("/openapi.json", self.openapi, methods=["GET"], name="operator_openapi")
        )
        return routes


def _authority(host: str, port: int) -> str:
    address = ipaddress.ip_address(host)
    rendered = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{rendered}:{port}"


class LocalReadPolicyMiddleware:
    """Fail closed before sources for the unauthenticated loopback-only phase-one profile."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        listener_host: str,
        listener_port: int,
        allowed_origin: str | None = None,
    ) -> None:
        self.app = app
        self.authority = _authority(listener_host, listener_port)
        same_origin = f"http://{self.authority}"
        if allowed_origin is not None:
            parsed = urlsplit(allowed_origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowed UI origin must be one exact local HTTP(S) origin")
            try:
                origin_host_is_local = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                origin_host_is_local = parsed.hostname.lower() == "localhost"
            if not origin_host_is_local:
                raise ValueError("allowed UI origin must name a loopback host")
            allowed_origin = allowed_origin.removesuffix("/")
        self.allowed_origins = frozenset(
            origin for origin in (same_origin, allowed_origin) if origin is not None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header_values: dict[str, list[str]] = {}
        for key, value in scope.get("headers", ()):
            header_values.setdefault(key.decode("latin-1").lower(), []).append(
                value.decode("latin-1")
            )
        headers = {key: values[0] for key, values in header_values.items() if len(values) == 1}
        if (
            len(header_values.get("host", ())) != 1
            or headers.get("host", "").lower() != self.authority.lower()
        ):
            await _error_response(
                OperatorSourceError(
                    "invalid_host",
                    "The request Host does not match the loopback listener.",
                    status_code=400,
                )
            )(scope, receive, send)
            return
        client = scope.get("client")
        if client is None:
            peer = None
        else:
            try:
                peer = ipaddress.ip_address(client[0])
            except ValueError:
                peer = None
        if peer is None or not peer.is_loopback:
            await _error_response(
                OperatorSourceError(
                    "non_loopback_client",
                    "The phase-one operator profile accepts loopback clients only.",
                    status_code=403,
                )
            )(scope, receive, send)
            return
        if len(header_values.get("origin", ())) > 1:
            await _error_response(
                OperatorSourceError(
                    "invalid_origin",
                    "The request Origin is not allowed by the local operator profile.",
                    status_code=403,
                )
            )(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            await _error_response(
                OperatorSourceError(
                    "invalid_origin",
                    "The request Origin is not allowed by the local operator profile.",
                    status_code=403,
                )
            )(scope, receive, send)
            return

        if scope.get("method") == "OPTIONS":
            raw_path = scope.get("raw_path", b"")
            requested_method = header_values.get("access-control-request-method", ())
            requested_headers = header_values.get("access-control-request-headers", ())
            header_names = {
                value.strip().lower()
                for raw in requested_headers
                for value in raw.split(",")
                if value.strip()
            }
            if (
                origin is None
                or not isinstance(raw_path, bytes)
                or _EVENTS_RAW_PATH.fullmatch(raw_path) is None
                or requested_method != ["GET"]
                or len(requested_headers) > 1
                or not header_names.issubset({"last-event-id"})
            ):
                await _error_response(
                    OperatorSourceError(
                        "invalid_preflight",
                        "Only the exact local OperatorEvent GET preflight is allowed.",
                        status_code=403,
                    )
                )(scope, receive, send)
                return
            await Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET",
                    "Access-Control-Allow-Headers": "Last-Event-ID",
                    "Vary": "Origin",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )(scope, receive, send)
            return

        if scope.get("method") != "GET":
            await _error_response(
                OperatorSourceError(
                    "read_only_profile",
                    "The phase-one operator profile permits read-only GET requests only.",
                    status_code=405,
                )
            )(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", ()))
                if not any(key.lower() == b"cache-control" for key, _ in response_headers):
                    response_headers.append((b"cache-control", b"no-store"))
                response_headers.append((b"x-content-type-options", b"nosniff"))
                if origin is not None:
                    response_headers.extend(
                        [
                            (b"access-control-allow-origin", origin.encode("latin-1")),
                            (b"vary", b"Origin"),
                        ]
                    )
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, secure_send)


def route_paths(routes: Sequence[BaseRoute]) -> set[str]:
    return {str(route.path) for route in routes if hasattr(route, "path")}
