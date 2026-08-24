#!/usr/bin/env python3
"""Scratch-only unified-host transport proof for bh-u562.3.

This file deliberately lives under docs/spikes/proofs rather than src/.  It proves that the
existing FastMCP server can keep its stdio entry point while the same server is composed into a
Starlette app with operator HTTP/SSE and terminal websocket placeholders.  It is evidence, not
product code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from beadhive.mcp import build_server

ALL_SCOPES = {
    "mcp:control",
    "operator:read",
    "activity:publish",
    "terminal:attach",
}
TOKENS = {
    "mcp-token": {"mcp:control"},
    "operator-token": {"operator:read"},
    "publisher-token": {"activity:publish"},
    "terminal-token": {"terminal:attach"},
    "all-token": ALL_SCOPES,
}
EVENTS = (
    ("evt-2", "bead.delta", {"bead_id": "bh-proof", "revision": "bead-r2"}),
    ("evt-3", "run.activity", {"run_id": "run-proof", "sequence": 1}),
)


def _required_scope(scope: dict) -> str | None:
    path = scope.get("path", "")
    method = scope.get("method", "GET")
    if path == "/health":
        return None
    if path == "/mcp":
        return "mcp:control"
    if path == "/ws/terminal":
        return "terminal:attach"
    if path.startswith("/api/v1/runs/") and path.endswith("/activity") and method == "POST":
        return "activity:publish"
    if path == "/openapi.json" or path.startswith("/api/v1/"):
        return "operator:read"
    return None


class ScopeAuthMiddleware:
    """Tiny proof middleware: authenticate before entering any protected route."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        required = _required_scope(scope)
        if required is None or (scope["type"] == "http" and scope.get("method") == "OPTIONS"):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        granted = TOKENS.get(token)
        if granted is not None and required in granted:
            scope.setdefault("state", {})["auth_scopes"] = sorted(granted)
            await self.app(scope, receive, send)
            return

        status = 401 if not token or granted is None else 403
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            return
        response = JSONResponse(
            {"error": "unauthorized" if status == 401 else "forbidden", "required": required},
            status_code=status,
        )
        await response(scope, receive, send)


def _sse(event_id: str, event: str, payload: dict) -> bytes:
    return (
        f"id: {event_id}\nevent: {event}\ndata: {json.dumps(payload, sort_keys=True)}\n\n"
    ).encode()


async def health(request: Request):
    return JSONResponse(
        {
            "service": "bh-unified-host-proof",
            "status": "ok",
            "operator_stream_cancelled": request.app.state.operator_stream_cancelled,
        }
    )


async def factory(_request: Request):
    return JSONResponse({"schema": "bh.operator.factory/v1", "hives": []})


async def snapshot(request: Request):
    return JSONResponse(
        {
            "schema": "bh.operator.snapshot/v1",
            "hive_id": request.path_params["hive_id"],
            "revision": "bead-r1",
            "event_cursor": "evt-1",
            "beads": [],
        }
    )


async def events(request: Request):
    # Last-Event-ID is the reconnect authority. `after` exists for clients that cannot set it;
    # disagreement is rejected instead of guessing which cursor is newer.
    header_cursor = request.headers.get("last-event-id")
    query_cursor = request.query_params.get("after")
    if header_cursor and query_cursor and header_cursor != query_cursor:
        return JSONResponse({"error": "cursor_mismatch"}, status_code=400)
    cursor = header_cursor or query_cursor or "evt-1"
    known = {"evt-1": 0, "evt-2": 1, "evt-3": 2}
    if cursor not in known:
        return JSONResponse({"error": "cursor_expired", "action": "resnapshot"}, status_code=409)

    async def stream():
        try:
            for event_id, event_name, payload in EVENTS[known[cursor] :]:
                yield _sse(event_id, event_name, payload)
                await asyncio.sleep(0)
            if request.query_params.get("hold") == "1":
                await asyncio.Event().wait()
        finally:
            request.app.state.operator_stream_cancelled = True

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def activity(request: Request):
    if request.method == "POST":
        payload = await request.json()
        return JSONResponse(
            {
                "accepted": True,
                "run_id": request.path_params["run_id"],
                "sequence": payload.get("sequence"),
            },
            status_code=202,
        )
    return JSONResponse(
        {
            "schema": "bh.operator.run-activity/v1",
            "run_id": request.path_params["run_id"],
            "activities": [],
        }
    )


async def terminal(websocket: WebSocket):
    await websocket.accept(subprotocol="bh-terminal.v1")
    await websocket.send_json(
        {
            "type": "terminal.placeholder",
            "terminal_id": "term-proof",
            "container_exec": False,
        }
    )
    await websocket.close(code=1000)


async def openapi(_request: Request):
    paths = {
        "/health": {"get": {"responses": {"200": {"description": "healthy"}}}},
        "/api/v1/factory": {"get": {"responses": {"200": {"description": "factory"}}}},
        "/api/v1/hives/{hive_id}/snapshot": {
            "get": {"responses": {"200": {"description": "snapshot"}}}
        },
        "/api/v1/hives/{hive_id}/events": {
            "get": {"responses": {"200": {"description": "SSE event stream"}}}
        },
        "/api/v1/runs/{run_id}/activity": {
            "get": {"responses": {"200": {"description": "activity snapshot"}}},
            "post": {"responses": {"202": {"description": "activity accepted"}}},
        },
        "/ws/terminal": {"get": {"responses": {"101": {"description": "websocket"}}}},
        "/mcp": {"post": {"responses": {"200": {"description": "MCP Streamable HTTP"}}}},
        "/openapi.json": {"get": {"responses": {"200": {"description": "this OpenAPI document"}}}},
    }
    return JSONResponse(
        {
            "openapi": "3.1.0",
            "info": {"title": "Beadhive operator proof", "version": "0.0.0-spike"},
            "paths": paths,
        }
    )


def build_app(*, stateless_http: bool, shutdown_marker: Path | None = None) -> Starlette:
    """Compose existing MCP and proof-only browser routes under one lifespan owner."""
    mcp = build_server()
    mcp_app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=stateless_http,
        json_response=False,
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.operator_stream_cancelled = False
        async with mcp_app.lifespan(app):
            yield
        if shutdown_marker is not None:
            shutdown_marker.write_text("shutdown-complete\n")

    # The MCP route objects are composed directly. This avoids a root Mount swallowing sibling
    # paths and makes one Starlette lifespan unambiguously own both FastMCP and operator routes.
    routes = [
        Route("/health", health),
        Route("/api/v1/factory", factory),
        Route("/api/v1/hives/{hive_id:path}/snapshot", snapshot),
        Route("/api/v1/hives/{hive_id:path}/events", events),
        Route("/api/v1/runs/{run_id}/activity", activity, methods=["GET", "POST"]),
        WebSocketRoute("/ws/terminal", terminal),
        Route("/openapi.json", openapi),
        *mcp_app.routes,
    ]
    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["https://operator.example.invalid"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
            allow_credentials=False,
        ),
        Middleware(ScopeAuthMiddleware),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("stdio", help="run the unchanged bh MCP server over stdio")
    serve_parser = subparsers.add_parser("serve", help="run the scratch unified ASGI app")
    serve_parser.add_argument("--port", type=int, required=True)
    serve_parser.add_argument("--sessionful", action="store_true")
    serve_parser.add_argument("--shutdown-marker", type=Path)
    args = parser.parse_args()

    if args.command == "stdio":
        build_server().run(transport="stdio", show_banner=False)
        return 0

    import uvicorn

    # This proof is intentionally loopback-only. A real non-loopback listener must require TLS
    # at this boundary (directly or via a trusted reverse proxy).
    uvicorn.run(
        build_app(
            stateless_http=not args.sessionful,
            shutdown_marker=args.shutdown_marker,
        ),
        host=os.environ.get("BH_PROOF_HOST", "127.0.0.1"),
        port=args.port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
