"""ws observaloop command group — acceptance tests for.

All tests fake the observaloop adapter (no live MCP server / docker) and stub
``worktree._resolve_entry`` so a real managed rig is not required.  Verified:

- ``ws observaloop status`` shows enabled/available + profile name + state + endpoint
- ``ws observaloop status`` prints a clear message + exits 0 when disabled or unavailable
- ``ws observaloop down`` calls ``observaloop.down(name)`` and reports success
- ``ws observaloop down`` prints a clear message + exits 0 when disabled or unavailable
- ``worktree.prune`` never calls ``observaloop.down`` (Mode 1: shared profile persists)
"""

from __future__ import annotations

from typer.testing import CliRunner

import beadhive.observaloop as obs_mod
import beadhive.worktree as wt_mod
from beadhive import config
from beadhive.cli import app

# ---- shared fixtures / helpers ----------------------------------------------

_FAKE_ENTRY = {"prefix": "acme-api", "provider": "github", "org": "acme", "repo": "api"}
_FAKE_PROFILE = "acme-api"  # sanitize("acme-api") == "acme-api"

_CFG_ENABLED = {"otel": {"enabled": True}, "observaloop": {"enabled": True}}
_CFG_OBS_OFF = {"otel": {"enabled": True}, "observaloop": {"enabled": False}}
_CFG_OTEL_OFF = {"otel": {"enabled": False}, "observaloop": {"enabled": True}}


def _stub_entry(monkeypatch, entry=_FAKE_ENTRY):
    """Stub ``worktree._resolve_entry`` so commands don't need a real managed rig."""
    monkeypatch.setattr(wt_mod, "_resolve_entry", lambda cfg, hive: entry)


def _stub_available(monkeypatch, available=True):
    monkeypatch.setattr(obs_mod, "is_available", lambda cfg=None: available)


def _stub_profile_status(monkeypatch, status):
    monkeypatch.setattr(obs_mod, "profile_status", lambda name, cfg=None: status)


def _stub_endpoint(monkeypatch, endpoint):
    monkeypatch.setattr(obs_mod, "endpoint_for", lambda name, protocol, cfg=None: endpoint)


def _stub_down(monkeypatch, result):
    calls: list[str] = []
    monkeypatch.setattr(obs_mod, "down", lambda name, cfg=None: calls.append(name) or result)
    return calls


# ---- ws observaloop status --------------------------------------------------


def test_status_disabled_observaloop_flag(monkeypatch):
    """When observaloop.enabled is False, status prints 'enabled=no' and exits 0."""
    monkeypatch.setattr(config, "load", lambda: _CFG_OBS_OFF)
    _stub_entry(monkeypatch)

    res = CliRunner().invoke(app, ["observaloop", "status"])

    assert res.exit_code == 0
    assert "enabled=no" in res.output


def test_status_disabled_otel_off(monkeypatch):
    """When otel.enabled is False, observaloop_enabled is False → 'enabled=no'."""
    monkeypatch.setattr(config, "load", lambda: _CFG_OTEL_OFF)
    _stub_entry(monkeypatch)

    res = CliRunner().invoke(app, ["observaloop", "status"])

    assert res.exit_code == 0
    assert "enabled=no" in res.output


def test_status_unavailable(monkeypatch):
    """When enabled but the MCP server is unreachable, status prints 'available=no' and exits 0."""
    monkeypatch.setattr(config, "load", lambda: _CFG_ENABLED)
    _stub_entry(monkeypatch)
    _stub_available(monkeypatch, available=False)

    res = CliRunner().invoke(app, ["observaloop", "status"])

    assert res.exit_code == 0
    assert "available=no" in res.output
    assert _FAKE_PROFILE in res.output


def test_status_available_shows_profile_and_endpoint(monkeypatch):
    """Happy path: status shows profile name, state=up, and the OTLP endpoint."""
    monkeypatch.setattr(config, "load", lambda: _CFG_ENABLED)
    _stub_entry(monkeypatch)
    _stub_available(monkeypatch, available=True)
    _stub_profile_status(monkeypatch, {"manifest": {"otlp_http_port": 4318}})
    _stub_endpoint(monkeypatch, "http://localhost:4318")

    res = CliRunner().invoke(app, ["observaloop", "status"])

    assert res.exit_code == 0
    assert "enabled=yes" in res.output
    assert "available=yes" in res.output
    assert _FAKE_PROFILE in res.output
    assert "state:       up" in res.output
    assert "http://localhost:4318" in res.output


def test_status_shows_down_when_no_endpoint(monkeypatch):
    """When the profile exists (status not None) but endpoint can't be resolved → state=down."""
    monkeypatch.setattr(config, "load", lambda: _CFG_ENABLED)
    _stub_entry(monkeypatch)
    _stub_available(monkeypatch, available=True)
    _stub_profile_status(monkeypatch, {"manifest": {}})  # profile exists, no port
    _stub_endpoint(monkeypatch, None)

    res = CliRunner().invoke(app, ["observaloop", "status"])

    assert res.exit_code == 0
    assert "state:       down" in res.output
    assert "endpoint:    (none)" in res.output


def test_status_shows_unknown_when_status_none(monkeypatch):
    """When profile_status returns None (adapter call failed) → state=unknown."""
    monkeypatch.setattr(config, "load", lambda: _CFG_ENABLED)
    _stub_entry(monkeypatch)
    _stub_available(monkeypatch, available=True)
    _stub_profile_status(monkeypatch, None)
    _stub_endpoint(monkeypatch, None)

    res = CliRunner().invoke(app, ["observaloop", "status"])

    assert res.exit_code == 0
    assert "state:       unknown" in res.output


def test_status_disabled_message_includes_config_hint(monkeypatch):
    """The disabled message tells the user which config keys to set."""
    monkeypatch.setattr(config, "load", lambda: _CFG_OBS_OFF)
    _stub_entry(monkeypatch)

    res = CliRunner().invoke(app, ["observaloop", "status"])

    assert res.exit_code == 0
    assert "observaloop.enabled" in res.output or "otel.enabled" in res.output


# ---- ws observaloop down ----------------------------------------------------


def test_down_disabled(monkeypatch):
    """When disabled, down prints 'disabled' + exits 0 without calling the adapter."""
    monkeypatch.setattr(config, "load", lambda: _CFG_OBS_OFF)
    _stub_entry(monkeypatch)
    calls = _stub_down(monkeypatch, {"stopped": True})

    res = CliRunner().invoke(app, ["observaloop", "down"])

    assert res.exit_code == 0
    assert "disabled" in res.output
    assert calls == [], "adapter must not be called when disabled"


def test_down_unavailable(monkeypatch):
    """When unavailable, down prints 'unavailable' + exits 0 without calling the adapter."""
    monkeypatch.setattr(config, "load", lambda: _CFG_ENABLED)
    _stub_entry(monkeypatch)
    _stub_available(monkeypatch, available=False)
    calls = _stub_down(monkeypatch, {"stopped": True})

    res = CliRunner().invoke(app, ["observaloop", "down"])

    assert res.exit_code == 0
    assert "unavailable" in res.output
    assert calls == [], "adapter must not be called when unavailable"


def test_down_calls_adapter_with_profile_name(monkeypatch):
    """Happy path: down calls observaloop.down(profile_name) and reports success."""
    monkeypatch.setattr(config, "load", lambda: _CFG_ENABLED)
    _stub_entry(monkeypatch)
    _stub_available(monkeypatch, available=True)
    calls = _stub_down(monkeypatch, {"stopped": True})

    res = CliRunner().invoke(app, ["observaloop", "down"])

    assert res.exit_code == 0
    assert calls == [_FAKE_PROFILE], "down must call observaloop.down with the hive profile name"
    assert "stopped" in res.output


def test_down_warns_when_adapter_returns_none(monkeypatch):
    """When observaloop.down returns None (best-effort miss), down warns but exits 0."""
    monkeypatch.setattr(config, "load", lambda: _CFG_ENABLED)
    _stub_entry(monkeypatch)
    _stub_available(monkeypatch, available=True)
    _stub_down(monkeypatch, None)

    res = CliRunner().invoke(app, ["observaloop", "down"])

    # exits non-zero is ok here (we do nothing special), but never raises
    assert "could not stop" in (res.output + (res.stderr if hasattr(res, "stderr") else ""))


# ---- prune does not tear down the shared rig profile (Mode 1) ---------------


def test_prune_does_not_call_observaloop_down(monkeypatch):
    """worktree.prune removes worktrees but NEVER calls observaloop.down (Mode 1)."""
    # Track any call to the observaloop.down seam
    teardown_calls: list = []
    monkeypatch.setattr(obs_mod, "down", lambda *a, **k: teardown_calls.append(a))

    # Stub config + managed() so prune has nothing to actually remove
    monkeypatch.setattr(config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(wt_mod, "managed", lambda cfg: [])

    res = CliRunner().invoke(app, ["worktree", "prune"])

    assert res.exit_code == 0
    assert teardown_calls == [], "prune must not call observaloop.down (Mode 1: profile persists)"
