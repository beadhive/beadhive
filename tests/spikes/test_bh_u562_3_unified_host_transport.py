"""Executable evidence for the bh-u562.3 transport spike; not a product contract test."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[2]
PROOF = ROOT / "docs/spikes/proofs/bh_u562_3_unified_host_transport.py"
MCP_HEADERS = {"Authorization": "Bearer mcp-token"}
OPERATOR_HEADERS = {"Authorization": "Bearer operator-token"}

# Inert input for every current tool. The discovery assertion below makes this table fail closed
# when a new tool is added. Refusal is a valid parity result; transport machinery failure is not.
TOOL_ARGS = {
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def _http_server(tmp_path: Path, *, sessionful: bool = False):
    port = _free_port()
    marker = tmp_path / ("sessionful.shutdown" if sessionful else "stateless.shutdown")
    argv = [
        sys.executable,
        str(PROOF),
        "serve",
        "--port",
        str(port),
        "--shutdown-marker",
        str(marker),
    ]
    if sessionful:
        argv.append("--sessionful")
    proc = subprocess.Popen(
        argv,
        cwd=ROOT,
        env={**os.environ, "OTEL_SDK_DISABLED": "true"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate()
                    pytest.fail(
                        f"proof server exited early ({proc.returncode})\n{stdout}\n{stderr}"
                    )
                try:
                    if (await client.get(f"{base_url}/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("proof server did not become healthy")
        yield base_url, proc, marker
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("proof server did not shut down within 10 seconds")
        stdout, stderr = proc.communicate()
        # Popen inherits pytest's event loop; on this Linux host uvicorn completes its lifespan
        # and the parent still observes the native SIGTERM status. The marker is the stronger
        # graceful-shutdown assertion (and matches bh's native-signal exit convention).
        assert proc.returncode in (0, -signal.SIGTERM), (
            f"proof server exit={proc.returncode}\n{stdout}\n{stderr}"
        )
        assert marker.read_text() == "shutdown-complete\n"


async def _surface(transport) -> tuple[dict, dict]:
    async with Client(transport, timeout=20) as client:
        tools = await client.list_tools()
        schemas = {tool.name: tool.inputSchema for tool in tools}
        assert set(schemas) == set(TOOL_ARGS)
        outcomes = {}
        for name in sorted(schemas):
            try:
                result = await client.call_tool(name, TOOL_ARGS[name])
                outcomes[name] = {"ok": True, "data": result.data}
            except ToolError as exc:
                outcomes[name] = {"ok": False, "error": str(exc)}
        return schemas, outcomes


@pytest.mark.parametrize("sessionful", [False, True], ids=["stateless", "sessionful"])
def test_every_current_tool_has_real_stdio_and_streamable_http_parity(tmp_path, sessionful):
    async def run():
        stdio = StdioTransport(
            command=sys.executable,
            args=[str(PROOF), "stdio"],
            cwd=str(ROOT),
            env={**os.environ, "OTEL_SDK_DISABLED": "true"},
            keep_alive=False,
            log_file=tmp_path / "stdio.log",
        )
        stdio_surface = await _surface(stdio)
        async with _http_server(tmp_path, sessionful=sessionful) as (base_url, _proc, _marker):
            http_transport = StreamableHttpTransport(f"{base_url}/mcp", headers=MCP_HEADERS)
            http_surface = await _surface(http_transport)
            session_id = http_transport.get_session_id()
            assert (session_id is not None) is sessionful
        assert stdio_surface == http_surface

    asyncio.run(run())


def test_routes_auth_cors_sse_resume_terminal_cancellation_and_shutdown(tmp_path):
    async def run():
        async with _http_server(tmp_path) as (base_url, _proc, marker):
            async with httpx.AsyncClient(base_url=base_url) as client:
                assert (await client.get("/api/v1/factory")).status_code == 401
                forbidden = await client.get(
                    "/api/v1/factory", headers={"Authorization": "Bearer mcp-token"}
                )
                assert forbidden.status_code == 403
                assert forbidden.json()["required"] == "operator:read"

                factory_response = await client.get("/api/v1/factory", headers=OPERATOR_HEADERS)
                assert factory_response.status_code == 200
                assert factory_response.json()["schema"] == "bh.operator.factory/v1"

                snapshot_response = await client.get(
                    "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/snapshot",
                    headers=OPERATOR_HEADERS,
                )
                assert snapshot_response.status_code == 200
                cursor = snapshot_response.json()["event_cursor"]
                assert cursor == "evt-1"

                event_response = await client.get(
                    "/api/v1/hives/github%2Fbeadhive%2Fbeadhive/events",
                    headers={**OPERATOR_HEADERS, "Last-Event-ID": cursor},
                )
                assert event_response.status_code == 200
                assert event_response.headers["content-type"].startswith("text/event-stream")
                assert "id: evt-2\nevent: bead.delta\n" in event_response.text
                assert "id: evt-3\nevent: run.activity\n" in event_response.text
                assert "evt-1" not in event_response.text

                mismatch = await client.get(
                    "/api/v1/hives/h/events?after=evt-2",
                    headers={**OPERATOR_HEADERS, "Last-Event-ID": "evt-1"},
                )
                assert mismatch.status_code == 400
                expired = await client.get(
                    "/api/v1/hives/h/events?after=gone", headers=OPERATOR_HEADERS
                )
                assert expired.status_code == 409
                assert expired.json()["action"] == "resnapshot"

                assert (
                    await client.post(
                        "/api/v1/runs/run-1/activity",
                        headers=OPERATOR_HEADERS,
                        json={"sequence": 7},
                    )
                ).status_code == 403
                publish = await client.post(
                    "/api/v1/runs/run-1/activity",
                    headers={"Authorization": "Bearer publisher-token"},
                    json={"sequence": 7},
                )
                assert publish.status_code == 202
                assert publish.json()["sequence"] == 7

                allowed_cors = await client.get(
                    "/api/v1/factory",
                    headers={**OPERATOR_HEADERS, "Origin": "https://operator.example.invalid"},
                )
                assert allowed_cors.headers["access-control-allow-origin"] == (
                    "https://operator.example.invalid"
                )
                denied_cors = await client.get(
                    "/api/v1/factory",
                    headers={**OPERATOR_HEADERS, "Origin": "https://evil.example"},
                )
                assert "access-control-allow-origin" not in denied_cors.headers

                spec = (await client.get("/openapi.json", headers=OPERATOR_HEADERS)).json()
                assert spec["openapi"] == "3.1.0"
                for path in (
                    "/mcp",
                    "/api/v1/factory",
                    "/api/v1/hives/{hive_id}/snapshot",
                    "/api/v1/hives/{hive_id}/events",
                    "/api/v1/runs/{run_id}/activity",
                    "/ws/terminal",
                    "/openapi.json",
                    "/health",
                ):
                    assert path in spec["paths"]

            import websockets

            async with websockets.connect(
                base_url.replace("http", "ws") + "/ws/terminal",
                additional_headers={"Authorization": "Bearer terminal-token"},
                subprotocols=["bh-terminal.v1"],
            ) as websocket:
                assert websocket.subprotocol == "bh-terminal.v1"
                assert json.loads(await websocket.recv())["type"] == "terminal.placeholder"

            # Hold an operator stream open while an MCP request succeeds, then close the client.
            async with httpx.AsyncClient(base_url=base_url) as stream_client:
                async with stream_client.stream(
                    "GET",
                    "/api/v1/hives/h/events?hold=1",
                    headers=OPERATOR_HEADERS,
                ) as response:
                    chunks = response.aiter_bytes()
                    assert b"event: bead.delta" in await anext(chunks)
                    async with Client(
                        StreamableHttpTransport(f"{base_url}/mcp", headers=MCP_HEADERS)
                    ) as mcp_client:
                        names = {tool.name for tool in await mcp_client.list_tools()}
                        assert names == set(TOOL_ARGS)

            deadline = time.monotonic() + 5
            async with httpx.AsyncClient() as health_client:
                while time.monotonic() < deadline:
                    health_response = await health_client.get(f"{base_url}/health")
                    if health_response.json()["operator_stream_cancelled"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("operator SSE generator did not observe client cancellation")
            assert not marker.exists()
        assert marker.exists()

    asyncio.run(run())
