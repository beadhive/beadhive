""" — CLI invocation counter + latency histogram.

Two test surfaces:

1. ``ws.otel.record_cli_invocation`` — unit-tested directly against a mocked meter
   (mirrors test_otel_instrument.py's pattern).

2. The ``_root`` call_on_close wiring — driven via Typer's CliRunner with a mocked meter +
   ``otel._initialized = True`` (mirrors test_otel_cli.py's pattern) so the full dispatch path
   is exercised without a real OTel SDK or collector.

Off-path coverage: ``otel.is_active() == False`` (the default) must emit nothing and leave
behavior + exit codes unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from beadhive import cli, otel
from beadhive.cli import _outcome_from_exc, app


@pytest.fixture(autouse=True)
def _reset_otel():
    """Each test starts with otel off + empty instrument cache; restore afterward."""
    otel._initialized = False
    otel._instruments.clear()
    yield
    otel._initialized = False
    otel._instruments.clear()


def _activate(monkeypatch) -> MagicMock:
    """Force otel 'on' with a mocked meter + tracer; return meter for assertions.

    The tracer mock is needed because _root now opens a root CLI span via otel.span()
    (which calls get_tracer()) before registering the call_on_close metric hook.
    """
    meter = MagicMock(name="meter")
    tracer = MagicMock(name="tracer")
    span_cm = MagicMock(name="span_cm")
    span_cm.__enter__.return_value = MagicMock(name="span")
    span_cm.__exit__.return_value = False
    tracer.start_as_current_span.return_value = span_cm
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    return meter


def _stub_config(monkeypatch) -> None:
    """Prevent real config loading in _root (otel stays at whatever _initialized is set to)."""
    monkeypatch.setattr(cli.config, "load", lambda: {})


# ---- record_cli_invocation: unit tests (mocked meter) -----------------------


def test_helper_emits_counter_and_histogram_when_on(monkeypatch):
    meter = _activate(monkeypatch)
    otel.record_cli_invocation("work", "ok", 0.5)

    meter.create_counter.assert_called_once()
    assert meter.create_counter.call_args.args[0] == "bh.cli.invocations"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"bh.cli.command": "work", "bh.cli.outcome": "ok"}
    )
    meter.create_histogram.assert_called_once()
    assert meter.create_histogram.call_args.args[0] == "bh.cli.duration"
    meter.create_histogram.return_value.record.assert_called_once_with(
        0.5, {"bh.cli.command": "work", "bh.cli.outcome": "ok"}
    )


def test_helper_noop_when_otel_off():
    otel.record_cli_invocation("work", "ok", 0.5)
    assert otel._instruments == {}  # no instrument created on the off-path


def test_helper_tags_error_outcome(monkeypatch):
    meter = _activate(monkeypatch)
    otel.record_cli_invocation("config", "error", 1.2)
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"bh.cli.command": "config", "bh.cli.outcome": "error"}
    )
    meter.create_histogram.return_value.record.assert_called_once_with(
        1.2, {"bh.cli.command": "config", "bh.cli.outcome": "error"}
    )


def test_instruments_cached_across_invocations(monkeypatch):
    meter = _activate(monkeypatch)
    otel.record_cli_invocation("work", "ok", 0.1)
    otel.record_cli_invocation("plan", "error", 0.2)
    # same counter + histogram reused for both invocations
    assert meter.create_counter.call_count == 1
    assert meter.create_histogram.call_count == 1


# ---- _outcome_from_exc: maps the active ctx.call_on_close exception ---------


def test_outcome_none_is_ok():
    # standalone_mode=False success path: no exception active
    assert _outcome_from_exc(None) == "ok"


def test_outcome_system_exit_zero_is_ok():
    assert _outcome_from_exc(SystemExit(0)) == "ok"
    assert _outcome_from_exc(SystemExit(None)) == "ok"


def test_outcome_system_exit_nonzero_is_error():
    assert _outcome_from_exc(SystemExit(1)) == "error"
    assert _outcome_from_exc(SystemExit(2)) == "error"


def test_outcome_exit_zero_is_ok():
    import typer

    assert _outcome_from_exc(typer.Exit(0)) == "ok"


def test_outcome_exit_nonzero_is_error():
    import typer

    assert _outcome_from_exc(typer.Exit(1)) == "error"


def test_outcome_abort_is_error():
    import typer

    assert _outcome_from_exc(typer.Abort()) == "error"


def test_outcome_generic_exception_is_error():
    assert _outcome_from_exc(ValueError("oops")) == "error"


# ---- CliRunner wiring: counter + histogram emitted on real command dispatch --


def test_runner_records_ok_invocation(monkeypatch):
    """A successful command emits counter(ok) + histogram via call_on_close."""
    meter = _activate(monkeypatch)
    _stub_config(monkeypatch)

    res = CliRunner().invoke(app, ["config", "path"])

    assert res.exit_code == 0
    # counter
    meter.create_counter.assert_called_once()
    add_args = meter.create_counter.return_value.add.call_args.args
    assert add_args[0] == 1
    attrs = add_args[1]
    assert attrs["bh.cli.command"] == "config"
    assert attrs["bh.cli.outcome"] == "ok"
    # histogram
    meter.create_histogram.assert_called_once()
    rec_args = meter.create_histogram.return_value.record.call_args.args
    assert rec_args[1] == {"bh.cli.command": "config", "bh.cli.outcome": "ok"}
    assert rec_args[0] >= 0  # non-negative duration


def test_runner_records_error_invocation(monkeypatch):
    """A command that exits non-zero emits counter(error)."""
    meter = _activate(monkeypatch)
    _stub_config(monkeypatch)

    # ws worktree path with no ref arg exits 1
    res = CliRunner().invoke(app, ["worktree", "path"])

    assert res.exit_code != 0
    add_args = meter.create_counter.return_value.add.call_args.args
    attrs = add_args[1]
    assert attrs["bh.cli.command"] == "worktree"
    assert attrs["bh.cli.outcome"] == "error"


def test_runner_skips_recording_when_otel_off(monkeypatch):
    """Off-path: no counter/histogram creation, exit code unchanged."""
    _stub_config(monkeypatch)

    res = CliRunner().invoke(app, ["config", "path"])

    assert res.exit_code == 0
    assert otel._instruments == {}


def test_runner_exit_code_unchanged_when_otel_on(monkeypatch):
    """Telemetry must never alter exit codes."""
    _stub_config(monkeypatch)

    res_off = CliRunner().invoke(app, ["config", "path"])
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: MagicMock())
    res_on = CliRunner().invoke(app, ["config", "path"])

    assert res_off.exit_code == res_on.exit_code == 0


def test_runner_version_flag_skips_recording(monkeypatch):
    """--version is eager and exits before _root body runs; nothing is emitted."""
    _activate(monkeypatch)
    _stub_config(monkeypatch)

    res = CliRunner().invoke(app, ["--version"])

    assert res.exit_code == 0
    assert otel._instruments == {}


def test_runner_command_name_uses_invoked_subcommand(monkeypatch):
    """The command attribute uses ctx.invoked_subcommand (top-level group name)."""
    meter = _activate(monkeypatch)
    _stub_config(monkeypatch)

    CliRunner().invoke(app, ["work", "--help"])

    add_args = meter.create_counter.return_value.add.call_args.args
    assert add_args[1]["bh.cli.command"] == "work"
