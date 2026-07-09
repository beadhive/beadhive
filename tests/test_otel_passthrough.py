""" — passthrough fallback counter (ws.passthrough.invocations).

Two test surfaces (mirroring test_otel_cli_instrument.py):

1. ``ws.otel.count_passthrough`` — unit-tested directly against a mocked meter, covering the
   allowed + gated-off attribute values and the off-path no-op.

2. The ``bd_passthrough`` / ``git_passthrough`` entrypoint wiring — driven via Typer's
   CliRunner with ``otel.count_passthrough`` spied, so the gate-on (allowed=True) and gate-off
   (allowed=False) branches are both exercised for each surface without a real OTel SDK.
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


def _activate(monkeypatch) -> MagicMock:
    """Force otel 'on' with a mocked meter; return it for assertions."""
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    return meter


# ---- count_passthrough: unit tests (mocked meter) ---------------------------


def test_count_passthrough_allowed_tags_surface_and_allowed_true(monkeypatch):
    meter = _activate(monkeypatch)
    otel.count_passthrough("bd", allowed=True)

    meter.create_counter.assert_called_once()
    assert meter.create_counter.call_args.args[0] == "ws.passthrough.invocations"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.passthrough.surface": "bd", "ws.passthrough.allowed": True}
    )


def test_count_passthrough_gated_tags_allowed_false(monkeypatch):
    meter = _activate(monkeypatch)
    otel.count_passthrough("git", allowed=False)
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.passthrough.surface": "git", "ws.passthrough.allowed": False}
    )


def test_count_passthrough_noop_when_otel_off():
    otel.count_passthrough("bd", allowed=True)
    otel.count_passthrough("git", allowed=False)
    assert otel._instruments == {}  # no instrument created on the off-path


def test_count_passthrough_instrument_cached_across_calls(monkeypatch):
    meter = _activate(monkeypatch)
    otel.count_passthrough("bd", allowed=True)
    otel.count_passthrough("git", allowed=False)
    assert meter.create_counter.call_count == 1  # one counter reused for both samples


# ---- entrypoint wiring: allowed vs gated-off per surface --------------------


def _spy_count(monkeypatch) -> MagicMock:
    """Replace otel.count_passthrough with a spy and stub config.load out of _root."""
    spy = MagicMock(name="count_passthrough")
    monkeypatch.setattr(cli.otel, "count_passthrough", spy)
    monkeypatch.setattr(cli.config, "load", lambda: {})
    return spy


def test_bd_passthrough_allowed_records_true(monkeypatch):
    spy = _spy_count(monkeypatch)
    monkeypatch.setattr(cli.config, "bd_pass_enabled", lambda *a, **k: True)
    ran = MagicMock(name="bd_passthrough_run")
    monkeypatch.setattr(cli.bd_mod, "passthrough", ran)

    res = CliRunner().invoke(app, ["bd", "ready"])

    assert res.exit_code == 0
    ran.assert_called_once()
    spy.assert_called_once_with("bd", allowed=True)


def test_bd_passthrough_gated_records_false_and_exits(monkeypatch):
    spy = _spy_count(monkeypatch)
    monkeypatch.setattr(cli.config, "bd_pass_enabled", lambda *a, **k: False)
    ran = MagicMock(name="bd_passthrough_run")
    monkeypatch.setattr(cli.bd_mod, "passthrough", ran)

    res = CliRunner().invoke(app, ["bd", "ready"])

    assert res.exit_code == 1
    ran.assert_not_called()  # gate blocked the underlying passthrough
    spy.assert_called_once_with("bd", allowed=False)


def test_git_passthrough_allowed_records_true(monkeypatch):
    spy = _spy_count(monkeypatch)
    monkeypatch.setattr(cli.config, "git_pass_enabled", lambda *a, **k: True)
    from beadhive import git as git_mod

    ran = MagicMock(name="git_passthrough_run")
    monkeypatch.setattr(git_mod, "passthrough", ran)

    res = CliRunner().invoke(app, ["git", "status"])

    assert res.exit_code == 0
    ran.assert_called_once()
    spy.assert_called_once_with("git", allowed=True)


def test_git_passthrough_gated_records_false_and_exits(monkeypatch):
    spy = _spy_count(monkeypatch)
    monkeypatch.setattr(cli.config, "git_pass_enabled", lambda *a, **k: False)
    from beadhive import git as git_mod

    ran = MagicMock(name="git_passthrough_run")
    monkeypatch.setattr(git_mod, "passthrough", ran)

    res = CliRunner().invoke(app, ["git", "status"])

    assert res.exit_code == 1
    ran.assert_not_called()
    spy.assert_called_once_with("git", allowed=False)
