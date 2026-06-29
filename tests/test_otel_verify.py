"""OTel live-verification harness — opt-in; skipped by default in CI.

PURPOSE
-------
Lets an operator running their own OTLP collector confirm that telemetry actually flows
from ws to the collector over the configured transport.  This is NOT a mocked unit test;
it drives the real OTel SDK against a real endpoint so the operator can observe spans,
metrics, and logs in their collector (e.g. grafana/otel-lgtm, a local Jaeger, Honeycomb).

PREREQUISITES
-------------
Install the otel + mcp extras (needed for SDK init and the in-memory MCP client):

    uv sync --extra otel --extra mcp
    # or: pip install 'ws[otel,mcp]'

HOW TO RUN
----------
1. Start a collector, e.g.:

       docker run --rm -p 3000:3000 -p 4317:4317 -p 4318:4318 grafana/otel-lgtm

2. Run the harness (default endpoint: gRPC on 4317):

       just otel-verify
       # or explicitly:
       WS_OTEL_VERIFY=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \\
           uv run pytest tests/test_otel_verify.py -v -s

   HTTP/protobuf transport (port 4318):

       WS_OTEL_VERIFY=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \\
           WS_OTEL_PROTOCOL=http/protobuf \\
           uv run pytest tests/test_otel_verify.py -v -s

WHAT TO SEE IN YOUR COLLECTOR
------------------------------
  Traces   — spans: "cli.verify", "mcp.verify", "cli.error.verify", "mcp.error.verify"
             service.name=ws, service.version=<installed>
  Metrics  — ws.cli.invocations  {ws.cli.command=verify, ws.cli.outcome=ok}
             ws.cli.duration      (same tags, unit=s)
             ws.mcp.tool.invocations  {ws.mcp.tool=plan_check, ws.mcp.outcome=ok}
             ws.mcp.tool.duration     (same tags, unit=s)
             ws.errors  {ws.error.boundary=cli, ws.error.kind=RuntimeError}
             ws.errors  {ws.error.boundary=mcp, ws.error.kind=RuntimeError}
  Logs     — "otel_initialized" + "mcp_tool_error" records bridged via LoggingHandler

FLUSH
-----
otel.init() registers an atexit hook (provider.shutdown()) to force-flush the batch
processors.  The module fixture also calls otel.shutdown() on teardown so data reaches
the collector before the pytest session moves on, not just at process exit.

GATING
------
WS_OTEL_VERIFY and OTEL_EXPORTER_OTLP_ENDPOINT must both be set; absent either, every
test in this module is skipped cleanly so `just check` (CI default) needs no collector.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from ws import config, otel
from ws import mcp as mcp_mod

_SKIP_REASON = (
    "live-otel verification skipped — "
    "set WS_OTEL_VERIFY=1 and OTEL_EXPORTER_OTLP_ENDPOINT to run against a real collector"
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("WS_OTEL_VERIFY") and os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")),
    reason=_SKIP_REASON,
)


@pytest.fixture(scope="module", autouse=True)
def _live_otel():
    """Initialize the real OTel SDK against the configured OTLP endpoint (once per module).

    Tears down with otel.shutdown() to force-flush BatchSpanProcessor /
    PeriodicExportingMetricReader / BatchLogRecordProcessor so all buffered
    telemetry reaches the collector before the test session continues.
    """
    # Guard: if collected but env is absent (e.g. --collect-only), do nothing.
    if not (os.getenv("WS_OTEL_VERIFY") and os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")):
        yield
        return

    # Reset any leftover init state from earlier test modules.
    otel.shutdown()

    protocol = os.getenv("WS_OTEL_PROTOCOL", config.OTEL_PROTOCOL_GRPC)
    cfg = {"otel": {"enabled": True, "protocol": protocol}}
    initialized = otel.init(cfg)
    if not initialized:
        pytest.skip(
            "otel.init() returned False — install the ws[otel] extra "
            "(uv sync --extra otel) and verify OTEL_EXPORTER_OTLP_ENDPOINT is reachable"
        )

    yield

    # Explicit flush: provider.shutdown() drains the batch processors so telemetry
    # reaches the collector here, before atexit (which is the fallback safety net).
    otel.shutdown()


# ---------------------------------------------------------------------------
# (a) CLI invocation seam
# ---------------------------------------------------------------------------


def test_cli_invocation():
    """CLI seam: emit ws.cli.invocations + ws.cli.duration inside a named span.

    Mirrors what cli._root's call_on_close hook does after a real subcommand returns.
    In your collector: a "cli.verify" span + ws.cli.invocations{command=verify, outcome=ok}.
    """
    with otel.span("cli.verify", {"ws.verify.step": "cli_invocation"}):
        t0 = time.monotonic()
        otel.record_cli_invocation("verify", "ok", time.monotonic() - t0)


# ---------------------------------------------------------------------------
# (b) MCP tool invocation seam
# ---------------------------------------------------------------------------


def test_mcp_tool_invocation():
    """MCP seam: call plan_check via the in-memory FastMCP Client.

    Exercises _measured_tool → otel.record_mcp_invocation, emitting
    ws.mcp.tool.invocations + ws.mcp.tool.duration tagged tool=plan_check, outcome=ok.
    plan_check runs molecule validation in-process; no bd or git calls are needed.
    In your collector: a "mcp.verify" span + ws.mcp.tool.invocations{tool=plan_check}.
    """
    pytest.importorskip("fastmcp", reason="ws[mcp] extra required for MCP seam verification")
    from fastmcp import Client

    server = mcp_mod.build_server()
    spec = {
        "epic": {"title": "otel-verify-epic"},
        "issues": [
            {
                "handle": "v",
                "title": "verify telemetry flows",
                "acceptance": "spans and metrics arrive in the collector",
            }
        ],
    }

    async def _call():
        async with Client(server) as client:
            # Wrap in a span so the MCP metric is correlated to a parent trace.
            with otel.span("mcp.verify", {"ws.verify.step": "mcp_tool_invocation"}):
                return await client.call_tool("plan_check", {"spec": spec})

    result = asyncio.run(_call())
    assert result.data["valid"] is True


# ---------------------------------------------------------------------------
# (c) Error boundary — CLI
# ---------------------------------------------------------------------------


def test_cli_error_boundary():
    """CLI error boundary: record_exception + count_error inside an active span.

    Mirrors what cli._handle_cli_error does on an unhandled exception escaping a command:
    otel.record_exception(exc) sets the span status to ERROR and attaches the exception
    event; otel.count_error bumps ws.errors{boundary=cli, kind=RuntimeError}.
    In your collector: a "cli.error.verify" span with status=ERROR + ws.errors counter.
    """
    exc = RuntimeError("verify-induced-cli-error")
    with otel.span("cli.error.verify", {"ws.verify.step": "cli_error_boundary"}):
        otel.record_exception(exc)
        otel.count_error("cli", type(exc).__name__)


# ---------------------------------------------------------------------------
# (c) Error boundary — MCP
# ---------------------------------------------------------------------------


def test_mcp_error_boundary():
    """MCP error boundary: _observe_mcp_error inside an active span.

    Mirrors what _measured_tool does when a tool raises an unexpected exception: calls
    _observe_mcp_error which logs via structlog (bridged to OTel logs), calls
    otel.record_exception (span ERROR), and bumps ws.errors{boundary=mcp}.
    In your collector: a "mcp.error.verify" span with status=ERROR + ws.errors counter.
    """
    exc = RuntimeError("verify-induced-mcp-error")
    with otel.span("mcp.error.verify", {"ws.verify.step": "mcp_error_boundary"}):
        mcp_mod._observe_mcp_error("verify_tool", exc)
