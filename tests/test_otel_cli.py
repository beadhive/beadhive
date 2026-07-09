"""ws.cli — the root callback (`_root`) eagerly wires OTel before any subcommand runs.

This is the acceptance gap bead closes: `otel.init` was never called on a
real `ws` command path (only the unit tests called it directly), so `otel.is_active()` was
permanently False and every emitter inert. These tests drive the *CLI* (via Typer's CliRunner),
not `otel.init()` directly, to prove a real command path activates telemetry when enabled and
stays a zero-cost no-op when off.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from beadhive import cli, otel
from beadhive.cli import app


@pytest.fixture(autouse=True)
def _reset():
    """init() stamps a module-global guard; reset it around each test so order can't leak."""
    otel._initialized = False
    yield
    otel._initialized = False


def test_root_callback_initializes_otel_on_a_real_command(monkeypatch):
    # A real `ws` command path must drive otel.init() with the loaded config — not just a unit
    # test calling init() directly. `ws config path` is a trivial command that still runs _root.
    seen = {}
    monkeypatch.setattr(cli.config, "load", lambda: {"otel": {"enabled": True}})
    monkeypatch.setattr(cli.otel, "init", lambda cfg=None: seen.setdefault("cfg", cfg) or True)

    res = CliRunner().invoke(app, ["config", "path"])

    assert res.exit_code == 0
    assert seen["cfg"] == {"otel": {"enabled": True}}  # callback passed the loaded config


def test_root_callback_noops_when_disabled(monkeypatch):
    # Default (otel disabled): the callback still calls the REAL init(), which short-circuits
    # before importing opentelemetry — is_active() stays False and the CLI never crashes.
    monkeypatch.setattr(cli.config, "load", lambda: {})  # otel.enabled defaults false

    res = CliRunner().invoke(app, ["config", "path"])

    assert res.exit_code == 0
    assert otel.is_active() is False  # disabled ⇒ inert, no providers wired


def test_root_callback_survives_missing_config(monkeypatch):
    # Telemetry is best-effort: a missing config (e.g. before `ws config init`) must degrade to
    # telemetry-off, never block the command.
    def _missing():
        raise FileNotFoundError("ws config not found")

    monkeypatch.setattr(cli.config, "load", _missing)

    res = CliRunner().invoke(app, ["config", "path"])

    assert res.exit_code == 0
    assert otel.is_active() is False
