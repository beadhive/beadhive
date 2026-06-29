""" — global error handling at the CLI + MCP boundaries.

Three surfaces:

1. The otel helpers (``count_error`` / ``record_exception`` / ``get_current_span``) — unit-tested
   against a mocked meter + span (mirrors test_otel_instrument.py), incl. the off-path no-ops.

2. The CLI boundary (``ws.cli.main`` / ``_handle_cli_error``) — an unhandled exception escaping a
   command is logged via structlog, recorded on the active span (ERROR), counted (``ws.errors``),
   and surfaced as a concise stderr line + non-zero exit — never a raw traceback. Control-flow
   exits (``SystemExit`` / typer.Exit codes) pass through untouched. A CliRunner test proves the
   dqw.2 invocation seam still tags outcome=error (no double-count, no swallow).

3. The MCP boundary (``_measured_tool`` guard) — driven via the in-memory FastMCP Client: a genuine
   unhandled exception is observed (log + span ERROR + ``ws.errors``) and mapped to a clean
   ``ToolError``; an already-mapped ToolError (the jnv contract) passes through unobserved.

Off-path (otel disabled) must still log via structlog but skip the span + counter cheaply.
"""

from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from ws import cli, log, otel
from ws import mcp as mcp_mod
from ws.cli import app


@pytest.fixture(autouse=True)
def _reset_otel():
    """Each test starts with otel off + an empty instrument cache; restore afterward so a
    forced-on test never leaks ``_initialized`` into the rest of the suite."""
    otel._initialized = False
    otel._instruments.clear()
    yield
    otel._initialized = False
    otel._instruments.clear()


def _force_otel_on(monkeypatch) -> MagicMock:
    """Force otel active with a fresh mocked meter; return it for assertions."""
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    monkeypatch.setattr(otel, "_instruments", {})
    return meter


def _recording_span(monkeypatch) -> MagicMock:
    """Force ``get_current_span`` to return an inspectable recording span."""
    span = MagicMock(name="span")
    span.is_recording.return_value = True
    monkeypatch.setattr(otel, "get_current_span", lambda: span)
    return span


def _log_buf() -> io.StringIO:
    """Point the structlog/stdlib pipeline at a buffer (JSON face) so we can parse emitted lines."""
    buf = io.StringIO()
    log.configure(fmt="json", stream=buf)
    return buf


def _find_event(buf: io.StringIO, event: str) -> dict | None:
    """Return the first parsed JSON log line whose ``event`` matches (ignoring foreign noise)."""
    for line in buf.getvalue().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == event:
            return record
    return None


# ---- otel helpers: count_error / record_exception / get_current_span ---------


def test_get_current_span_is_noop_when_off():
    assert otel.is_active() is False
    assert otel.get_current_span() is otel._NOOP_SPAN


def test_count_error_is_noop_when_off():
    otel.count_error("cli", "ValueError")
    assert otel._instruments == {}  # nothing cached on the off-path


def test_count_error_increments_counter_when_on(monkeypatch):
    meter = _force_otel_on(monkeypatch)
    otel.count_error("mcp", "RuntimeError", {"ws.mcp.tool": "plan_check"})
    assert meter.create_counter.call_args.args[0] == "ws.errors"
    meter.create_counter.return_value.add.assert_called_once_with(
        1,
        {
            "ws.error.boundary": "mcp",
            "ws.error.kind": "RuntimeError",
            "ws.mcp.tool": "plan_check",
        },
    )


def test_record_exception_is_noop_when_off(monkeypatch):
    # Off ⇒ returns before ever reaching for a span (and never imports opentelemetry).
    monkeypatch.setattr(
        otel, "get_current_span", MagicMock(side_effect=AssertionError("no span when off"))
    )
    otel.record_exception(ValueError("x"))  # must not raise


def test_record_exception_skips_non_recording_span(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    span = MagicMock(name="span")
    span.is_recording.return_value = False
    monkeypatch.setattr(otel, "get_current_span", lambda: span)
    otel.record_exception(ValueError("x"))
    span.record_exception.assert_not_called()
    span.set_status.assert_not_called()


def test_record_exception_marks_active_span_error(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    span = _recording_span(monkeypatch)
    exc = ValueError("boom")
    otel.record_exception(exc)
    span.record_exception.assert_called_once_with(exc)
    span.set_status.assert_called_once()  # ERROR status set on the active span


# ---- CLI boundary: ws.cli.main / _handle_cli_error ---------------------------


def _stub_app(exc):
    """A stand-in for ``cli.app`` that raises ``exc`` when called (simulates what escapes app())."""

    def _raise():
        raise exc

    return _raise


def test_main_observes_and_surfaces_unhandled_exception(monkeypatch, capsys):
    buf = _log_buf()
    meter = _force_otel_on(monkeypatch)
    span = _recording_span(monkeypatch)
    monkeypatch.setattr(cli, "app", _stub_app(ValueError("boom")))
    monkeypatch.setattr(cli.sys, "argv", ["ws", "doctor", "--flag"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1  # non-zero exit preserved
    err = capsys.readouterr().err
    assert "✗ ValueError: boom" in err  # concise clean line ...
    assert "Traceback" not in err  # ... not a raw traceback

    record = _find_event(buf, "cli_command_error")
    assert record is not None
    assert record["command"] == "doctor"  # context: command name (best-effort from argv)
    assert record["error_type"] == "ValueError"
    assert record["error"] == "boom"

    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.error.boundary": "cli", "ws.error.kind": "ValueError"}
    )
    span.record_exception.assert_called_once()
    span.set_status.assert_called_once()


def test_main_passes_through_systemexit_without_observing(monkeypatch, capsys):
    # Control-flow exit (typer.Exit codes surface as SystemExit out of app()): preserve the code,
    # don't log/count/surface it as an error.
    buf = _log_buf()
    meter = _force_otel_on(monkeypatch)
    monkeypatch.setattr(cli, "app", _stub_app(SystemExit(2)))

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    meter.create_counter.assert_not_called()  # no error counter
    assert _find_event(buf, "cli_command_error") is None  # not logged as an error
    assert "✗" not in capsys.readouterr().err  # no surface line


def test_main_off_path_logs_and_surfaces_without_span_or_counter(monkeypatch, capsys):
    # Off-path: still logs via structlog + surfaces cleanly, but skips span/counter cheaply.
    buf = _log_buf()
    assert otel.is_active() is False
    monkeypatch.setattr(cli, "app", _stub_app(RuntimeError("nope")))
    monkeypatch.setattr(cli.sys, "argv", ["ws", "sync"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert "✗ RuntimeError: nope" in capsys.readouterr().err
    record = _find_event(buf, "cli_command_error")
    assert record is not None and record["command"] == "sync"
    assert otel._instruments == {}  # no instrument created off-path


def test_main_success_path_unchanged(monkeypatch):
    # The happy path must be untouched: app() returns, main() returns None, no surface/exit.
    monkeypatch.setattr(cli, "app", lambda: None)
    assert cli.main() is None


def test_cli_runner_unhandled_exception_still_tags_error_outcome(monkeypatch):
    # Under CliRunner the exception is captured by the runner (main() isn't involved), but the
    # dqw.2 call_on_close seam must still tag outcome=error — proving we didn't disturb it and the
    # ws.errors counter (added at the main() boundary) doesn't double-count the invocation here.
    meter = _force_otel_on(monkeypatch)
    monkeypatch.setattr(cli.config, "load", lambda: {})

    def _boom():
        raise ValueError("kaboom")

    monkeypatch.setattr(cli.config, "config_path", _boom)

    res = CliRunner().invoke(app, ["config", "path"])

    assert res.exit_code != 0
    attrs = meter.create_counter.return_value.add.call_args.args[1]
    assert attrs == {"ws.cli.command": "config", "ws.cli.outcome": "error"}
    # only the dqw.2 invocation counter fired (ws.cli.invocations) — no ws.errors here
    assert meter.create_counter.call_args.args[0] == "ws.cli.invocations"


# ---- MCP boundary: the _measured_tool guard (in-memory FastMCP Client) -------

_VALID_SPEC = {
    "epic": {"title": "Demo epic"},
    "issues": [{"handle": "a", "title": "do a thing", "acceptance": "it works"}],
}


def _call_tool(server, name, arguments):
    """Drive one tool call over the in-memory transport, returning the structured result."""
    from fastmcp import Client

    async def _run():
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(_run())


def test_mcp_unhandled_exception_observed_and_mapped_to_toolerror(monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    buf = _log_buf()
    meter = _force_otel_on(monkeypatch)
    span = _recording_span(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(mcp_mod.molecule, "validate_spec", _boom)
    server = mcp_mod.build_server()

    with pytest.raises(ToolError) as excinfo:
        _call_tool(server, "plan_check", {"spec": _VALID_SPEC})

    msg = str(excinfo.value)
    assert "plan_check failed" in msg  # clean, tool-named surface ...
    assert "RuntimeError" in msg and "disk gone" in msg  # ... carrying the cause

    record = _find_event(buf, "mcp_tool_error")
    assert record is not None
    assert record["tool"] == "plan_check"
    assert record["error_type"] == "RuntimeError"
    assert record["error"] == "disk gone"

    # span ERROR + BOTH counters fired (ws.errors AND the dqw.3 invocation counter) — no overlap.
    span.record_exception.assert_called_once()
    span.set_status.assert_called_once()
    adds = [c.args for c in meter.create_counter.return_value.add.call_args_list]
    assert (1, {"ws.error.boundary": "mcp", "ws.error.kind": "RuntimeError"}) in adds
    assert (1, {"ws.mcp.tool": "plan_check", "ws.mcp.outcome": "error"}) in adds


def test_mcp_already_mapped_toolerror_passes_through_unobserved(monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    buf = _log_buf()
    meter = _force_otel_on(monkeypatch)
    span = _recording_span(monkeypatch)
    server = mcp_mod.build_server()
    # Missing 'acceptance' → MoleculeError → the tool body already maps it to a ToolError.
    bad = {"epic": {"title": "E"}, "issues": [{"handle": "a", "title": "no acceptance"}]}

    with pytest.raises(ToolError) as excinfo:
        _call_tool(server, "plan_file", {"spec": bad})

    msg = str(excinfo.value).lower()
    assert "invalid molecule spec" in msg  # the jnv contract message, preserved ...
    assert "plan_file failed" not in msg  # ... and NOT re-wrapped by the boundary

    assert _find_event(buf, "mcp_tool_error") is None  # expected error → not observed
    span.record_exception.assert_not_called()
    adds = [c.args for c in meter.create_counter.return_value.add.call_args_list]
    assert all(a[1].get("ws.error.boundary") != "mcp" for a in adds)  # no ws.errors bump
    # ... but the dqw.3 invocation counter still tags outcome=error (untouched).
    assert any(a[1].get("ws.mcp.outcome") == "error" for a in adds)


def test_mcp_off_path_observes_via_log_only(monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    buf = _log_buf()
    assert otel.is_active() is False

    def _boom(*_a, **_k):
        raise RuntimeError("io")

    monkeypatch.setattr(mcp_mod.molecule, "validate_spec", _boom)
    server = mcp_mod.build_server()

    with pytest.raises(ToolError):
        _call_tool(server, "plan_check", {"spec": _VALID_SPEC})

    assert _find_event(buf, "mcp_tool_error") is not None  # logged even otel-off
    assert otel._instruments == {}  # no span/counter work on the off-path
