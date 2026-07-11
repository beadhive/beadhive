""" — root ws.cli span: open/close lifecycle + parent/child nesting.

Two test surfaces:

1. Mock-based: ``_root`` opens a ``ws.cli {command}`` span via ``otel.span()``, enters it
   so it becomes the current span, and closes it (with outcome attribute) in the
   ``call_on_close`` hook.  No real OTel SDK required; the tracer is a MagicMock.

2. SDK nesting: with a real ``TracerProvider + InMemorySpanExporter``, verify that child
   spans (created inside the subcommand body) nest under the root ``ws.cli`` span.
   Gated by ``pytest.importorskip``; skipped when the ``ws[otel]`` extra is absent.

Off-path coverage: ``otel.is_active() == False`` → no span opened, exit codes unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from beadhive import cli, otel
from beadhive.cli import app


@pytest.fixture(autouse=True)
def _reset_otel():
    """Each test starts with otel off + empty instrument cache; restore afterward."""
    otel._initialized = False
    otel._instruments.clear()
    yield
    otel._initialized = False
    otel._instruments.clear()


def _activate_with_tracer(monkeypatch):
    """Force otel 'on' with inspectable mocked tracer + meter.

    Returns ``(tracer, span, span_cm, meter)`` for per-test assertions.
    """
    span = MagicMock(name="span")
    span_cm = MagicMock(name="span_cm")
    span_cm.__enter__.return_value = span
    span_cm.__exit__.return_value = False
    tracer = MagicMock(name="tracer")
    tracer.start_as_current_span.return_value = span_cm

    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    return tracer, span, span_cm, meter


def _stub_config(monkeypatch) -> None:
    monkeypatch.setattr(cli.config, "load", lambda: {})


# ---- span is NOT opened when otel is off ------------------------------------


def test_no_span_when_otel_off(monkeypatch):
    """Off-path: no tracer call, no span opened, exit code unchanged."""
    get_tracer = MagicMock(side_effect=AssertionError("get_tracer must not be called when off"))
    monkeypatch.setattr(otel, "get_tracer", get_tracer)
    _stub_config(monkeypatch)

    res = CliRunner().invoke(app, ["config", "path"])
    assert res.exit_code == 0
    get_tracer.assert_not_called()


# ---- span lifecycle: opened in _root, closed in call_on_close ---------------


def test_root_span_opened_with_correct_name_and_command_attribute(monkeypatch):
    """_root opens ws.cli {command} span with ws.cli.command attribute."""
    tracer, _span, _cm, _meter = _activate_with_tracer(monkeypatch)
    _stub_config(monkeypatch)

    CliRunner().invoke(app, ["config", "path"])

    tracer.start_as_current_span.assert_called_once()
    name = tracer.start_as_current_span.call_args.args[0]
    assert name == "bh.cli config"
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["bh.cli.command"] == "config"


def test_span_context_manager_entered_so_span_becomes_current(monkeypatch):
    """__enter__ is called on the span cm so the span is current during dispatch."""
    tracer, _span, span_cm, _meter = _activate_with_tracer(monkeypatch)
    _stub_config(monkeypatch)

    CliRunner().invoke(app, ["config", "path"])

    span_cm.__enter__.assert_called_once()


def test_outcome_ok_attribute_set_on_successful_command(monkeypatch):
    """A successful command tags the span with ws.cli.outcome=ok."""
    _tracer, span, _cm, _meter = _activate_with_tracer(monkeypatch)
    _stub_config(monkeypatch)

    res = CliRunner().invoke(app, ["config", "path"])
    assert res.exit_code == 0

    span.set_attribute.assert_called_with("bh.cli.outcome", "ok")


def test_outcome_error_attribute_set_on_failing_command(monkeypatch):
    """A command that exits non-zero tags the span with ws.cli.outcome=error."""
    _tracer, span, _cm, _meter = _activate_with_tracer(monkeypatch)
    _stub_config(monkeypatch)

    # ws worktree path with no ref arg exits 1
    res = CliRunner().invoke(app, ["worktree", "path"])
    assert res.exit_code != 0

    span.set_attribute.assert_called_with("bh.cli.outcome", "error")


def test_span_context_manager_exited_in_close_callback(monkeypatch):
    """__exit__ is called in call_on_close so the span ends after the subcommand."""
    _tracer, _span, span_cm, _meter = _activate_with_tracer(monkeypatch)
    _stub_config(monkeypatch)

    CliRunner().invoke(app, ["config", "path"])

    span_cm.__exit__.assert_called_once()


def test_ok_exit_passes_none_exc_to_span_exit(monkeypatch):
    """Exit(0) / clean exit must call __exit__(None, None, None) — not mark span ERROR."""
    _tracer, _span, span_cm, _meter = _activate_with_tracer(monkeypatch)
    _stub_config(monkeypatch)

    res = CliRunner().invoke(app, ["config", "path"])
    assert res.exit_code == 0

    span_cm.__exit__.assert_called_once_with(None, None, None)


def test_work_subcommand_name_used_for_span(monkeypatch):
    """ctx.invoked_subcommand drives the span name (work → ws.cli work)."""
    tracer, _span, _cm, _meter = _activate_with_tracer(monkeypatch)
    _stub_config(monkeypatch)

    CliRunner().invoke(app, ["work", "--help"])

    name = tracer.start_as_current_span.call_args.args[0]
    assert name == "bh.cli work"
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["bh.cli.command"] == "work"


def test_version_flag_skips_span(monkeypatch):
    """--version is eager and exits before _root body; no span is opened."""
    tracer = MagicMock(name="tracer")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    _stub_config(monkeypatch)

    res = CliRunner().invoke(app, ["--version"])
    assert res.exit_code == 0
    tracer.start_as_current_span.assert_not_called()


def test_exit_codes_unchanged_with_or_without_otel(monkeypatch):
    """Span wiring must never change exit codes."""
    _stub_config(monkeypatch)
    res_off = CliRunner().invoke(app, ["config", "path"])

    _activate_with_tracer(monkeypatch)
    res_on = CliRunner().invoke(app, ["config", "path"])

    assert res_off.exit_code == res_on.exit_code == 0


# ---- SDK nesting: child spans nest under ws.cli root (requires ws[otel] extra) ----------


def test_child_spans_nest_under_cli_root_span(monkeypatch):
    """Root ws.cli span is the parent of any child span opened inside the subcommand.

    Requires the real OTel SDK (``ws[otel]`` extra: ``uv sync --extra otel``).
    Skipped automatically when the extra is absent so ``just check`` stays green without it.
    """
    sdk_trace = pytest.importorskip(
        "opentelemetry.sdk.trace",
        reason="ws[otel] extra required (uv sync --extra otel)",
    )
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real_tracer = provider.get_tracer("ws")

    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: real_tracer)
    monkeypatch.setattr(cli.config, "load", lambda: {})

    # Inject a child span inside the config.path command body to prove nesting
    original_config_path = cli.config.config_path

    def _config_path_with_child_span():
        with otel.span("child.span"):
            return original_config_path()

    monkeypatch.setattr(cli.config, "config_path", _config_path_with_child_span)

    res = CliRunner().invoke(app, ["config", "path"])
    assert res.exit_code == 0

    finished = exporter.get_finished_spans()
    root = next((s for s in finished if s.name == "bh.cli config"), None)
    child = next((s for s in finished if s.name == "child.span"), None)

    assert root is not None, f"missing ws.cli config root span; got: {[s.name for s in finished]}"
    assert child is not None, f"missing child.span; got: {[s.name for s in finished]}"

    # The child span's parent must be the root CLI span.
    assert child.parent is not None, "child.span has no parent — nesting is broken"
    assert child.parent.span_id == root.context.span_id, (
        f"child parent span_id {child.parent.span_id:#x} != "
        f"root span_id {root.context.span_id:#x}"
    )

    # The root CLI span must carry the correct attributes.
    assert root.attributes.get("bh.cli.command") == "config"
    assert root.attributes.get("bh.cli.outcome") == "ok"

    # The root span itself must have no parent (it is the true root).
    assert root.parent is None, "bh.cli config span should be a root (no parent)"
