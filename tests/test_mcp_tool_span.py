""" — MCP tool span: each tool call emits an execute_tool span.

``_measured_tool`` now wraps each FastMCP tool body in ``otel.span("execute_tool {tool}")``,
so:
  * subprocess spans started inside the tool body nest under the tool span;
  * an unhandled exception marks the span ERROR via the existing ``_observe_mcp_error`` /
    ``otel.record_exception`` path (which now has a recording current span to land on);
  * when otel is off the span is the shared no-op — zero cost, no opentelemetry import.

Tests use an in-memory OTel tracer built on the ``opentelemetry-api`` (always present in
this env) — no SDK required.  FastMCP tests are gated on ``pytest.importorskip("fastmcp")``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from opentelemetry import context as _ctx
from opentelemetry.trace import INVALID_SPAN_CONTEXT, Span, set_span_in_context
from opentelemetry.trace import get_current_span as _api_get_current_span

from beadhive import mcp as mcp_mod
from beadhive import otel as otel_mod

# ---- minimal in-memory OTel tracer (opentelemetry-api only) ------------------


class _InMemSpan(Span):
    """Recording span that tracks status + exceptions; propagates via OTel API context."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.parent: _InMemSpan | None = None
        self.status = None
        self.exceptions: list = []
        self._token = None

    # Span ABC
    def get_span_context(self):
        return INVALID_SPAN_CONTEXT

    def is_recording(self) -> bool:
        return True

    def set_attributes(self, a) -> None:
        pass

    def set_attribute(self, k, v) -> None:
        pass

    def add_event(self, *a, **k) -> None:
        pass

    def update_name(self, n) -> None:
        pass

    def set_status(self, status, description=None) -> None:
        self.status = status

    def record_exception(self, exc, *a, **k) -> None:
        self.exceptions.append(exc)

    def end(self, *a, **k) -> None:
        pass

    # Context-manager: attach to OTel context so get_current_span() returns self
    def __enter__(self) -> _InMemSpan:
        parent = _api_get_current_span()
        if parent.is_recording():
            self.parent = parent
        self._token = _ctx.attach(set_span_in_context(self))
        return self

    def __exit__(self, *exc_info) -> bool:
        _ctx.detach(self._token)
        return False


class _MemTracer:
    """Tracer that records every span in ``spans`` for post-call assertions."""

    def __init__(self) -> None:
        self.spans: list[_InMemSpan] = []

    def start_as_current_span(self, name: str, **kwargs) -> _InMemSpan:
        span = _InMemSpan(name)
        self.spans.append(span)
        return span


# ---- fixtures + helpers ------------------------------------------------------

_VALID_SPEC = {
    "epic": {"title": "Demo epic"},
    "issues": [{"handle": "a", "title": "do a thing", "acceptance": "it works"}],
}


@pytest.fixture(autouse=True)
def _reset_otel():
    otel_mod._initialized = False
    otel_mod._instruments.clear()
    yield
    otel_mod._initialized = False
    otel_mod._instruments.clear()


def _install_tracer(monkeypatch, tracer: _MemTracer) -> None:
    """Force otel active with the in-memory tracer and a no-op meter."""
    monkeypatch.setattr(otel_mod, "_initialized", True)
    monkeypatch.setattr(otel_mod, "_instruments", {})
    monkeypatch.setattr(otel_mod, "get_tracer", lambda *a, **k: tracer)
    monkeypatch.setattr(otel_mod, "get_meter", lambda *a, **k: MagicMock())


async def _call(server, tool: str, args: dict):
    from fastmcp import Client

    async with Client(server) as client:
        return await client.call_tool(tool, args)


# ---- tests -------------------------------------------------------------------


def test_tool_emits_execute_tool_span(monkeypatch):
    """Each tool call opens one ``execute_tool {tool}`` span."""
    pytest.importorskip("fastmcp")
    tracer = _MemTracer()
    _install_tracer(monkeypatch, tracer)

    asyncio.run(_call(mcp_mod.build_server(), "plan_check", {"spec": _VALID_SPEC}))

    assert len(tracer.spans) == 1
    assert tracer.spans[0].name == "execute_tool plan_check"


def test_tool_span_is_current_during_body(monkeypatch):
    """The execute_tool span is the current span during the tool body.

    Any span started inside (e.g. a ws.run subprocess span) sees the tool span as its
    parent — that is what 'nesting' means in this context.
    """
    pytest.importorskip("fastmcp")
    tracer = _MemTracer()
    _install_tracer(monkeypatch, tracer)

    seen: list = []

    def _capture(spec, cfg):
        # Record what the current span is during the tool's execution
        seen.append(_api_get_current_span())
        return []

    monkeypatch.setattr(mcp_mod.molecule, "validate_spec", _capture)

    asyncio.run(_call(mcp_mod.build_server(), "plan_check", {"spec": _VALID_SPEC}))

    assert len(tracer.spans) == 1, "exactly one tool span"
    tool_span = tracer.spans[0]
    assert len(seen) == 1
    # The body sees the tool span — a subprocess span started here would nest under it
    assert seen[0] is tool_span


def test_error_marks_span_error(monkeypatch):
    """An unhandled exception in the tool body marks the execute_tool span ERROR."""
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError
    from opentelemetry.trace import StatusCode

    tracer = _MemTracer()
    _install_tracer(monkeypatch, tracer)

    def _boom(*_a, **_k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(mcp_mod.molecule, "validate_spec", _boom)

    with pytest.raises(ToolError):
        asyncio.run(_call(mcp_mod.build_server(), "plan_check", {"spec": _VALID_SPEC}))

    assert len(tracer.spans) == 1
    tool_span = tracer.spans[0]
    assert tool_span.name == "execute_tool plan_check"
    # _observe_mcp_error → otel.record_exception landed on the current tool span
    assert tool_span.status is not None
    assert tool_span.status.status_code == StatusCode.ERROR
    assert any(isinstance(e, RuntimeError) for e in tool_span.exceptions)


def test_no_span_when_otel_off():
    """Otel-off: the span wrapper is a zero-cost no-op."""
    pytest.importorskip("fastmcp")

    assert not otel_mod.is_active()  # off by default

    asyncio.run(_call(mcp_mod.build_server(), "plan_check", {"spec": _VALID_SPEC}))

    # Nothing instrumented — instrument cache untouched
    assert otel_mod._instruments == {}
