"""Shared pytest fixtures + markers for the AGF harness."""

from __future__ import annotations

import os

import pytest

from beadhive import otel
from harness.world import World


@pytest.fixture(autouse=True)
def _sandbox_bh_home(tmp_path_factory, monkeypatch):
    """Every test gets an isolated `BH_HOME` so `config.home()` (and its one-time
    `~/.ws` -> `~/.beadhive` migration,) can NEVER resolve to — or mutate —
    the operator's real home directory. A test that merely imports `beadhive.config`, or
    invokes the CLI via `CliRunner`, must not be able to touch real state on the machine
    running the suite. Runs before every other fixture (defined first in this module).

    Uses `tmp_path_factory` (its own tmp root) rather than the test's own `tmp_path` — several
    tests scan/assert on the exact contents of their `tmp_path` (e.g. a directory-listing test),
    and a `bh-home` subdir nested inside it would show up as unexpected stray content.

    Also seeds a minimal `config.yaml` at the default path: a handful of tests call into a
    verb that loads config without setting up their own isolation (previously harmless only
    because it silently fell through to the *real* ~/.ws/config.yaml on a dev machine that
    happens to have one — exactly the kind of hidden real-state dependency this fixture exists
    to close off). Tests that need specific config content still set their own `BH_CONFIG` /
    `config.config_path` override, which simply wins over this default."""
    home = tmp_path_factory.mktemp("bh-home")
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.delenv("WS_HOME", raising=False)
    (home / "config.yaml").write_text(
        "providers: [github]\n"
        "managed_repos: []\n"
        "exclude:\n"
        "  orgs: []\n"
        "  repos: []\n"
        "otel:\n"
        "  enabled: false\n"
        "  protocol: grpc\n"
    )


@pytest.fixture(autouse=True)
def _telemetry_neutral_env(monkeypatch):
    """Scrub telemetry config from the process env for every test so results never depend on — nor
    are skewed by — the operator's otel setup. Without this, a parent ``ws`` running the suite as
    its clean-checkout validation leaks ``OTEL_EXPORTER_OTLP_ENDPOINT`` (the worktree overlay /
    self-heal endpoint) into the child, and any test reading the otel endpoint (e.g. doctor's
    observability section) would see the ambient value instead of its expected default/config one.
    Suite-wide hermeticity replaces per-test ``delenv`` scrubbing; tests that need a telemetry var
    set it explicitly via ``monkeypatch`` (which runs after this autouse fixture).

    Also reset otel's process-global ``_initialized`` state: a test that calls ``otel.init()``
    without tearing down would otherwise leak ``_initialized=True`` into later tests, making the
    otel-off no-op tests (which assume the default off state) fail only in the full suite."""
    for key in list(os.environ):
        if key.startswith("OTEL_") or key in ("WS_OBSERVALOOP_PROFILE", "BH_OBSERVALOOP_PROFILE"):
            monkeypatch.delenv(key, raising=False)
    otel.shutdown()  # reset any _initialized state leaked from a prior test
    # Bypass the setup gate for all tests unless they explicitly clear this env var.
    # test_setup.py tests that exercise the gate use monkeypatch.delenv to remove it.
    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.delenv("WS_SKIP_SETUP_CHECK", raising=False)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


@pytest.fixture
def fake_plugin(tmp_path, monkeypatch):
    """BH_PLUGIN_DIR → a minimal plugin tree (skills/ + agents/). The bh plugin is no longer
    vendored in this repo (beadhive/claude-plugin is canonical), so tests that need a real
    skills/agents source supply their own."""
    root = tmp_path / "fake-plugin"
    (root / "skills" / "demo-skill").mkdir(parents=True)
    (root / "skills" / "demo-skill" / "SKILL.md").write_text("skill\n")
    (root / "agents").mkdir()
    (root / "agents" / "developer.md").write_text("agent\n")
    monkeypatch.setenv("BH_PLUGIN_DIR", str(root))
    return root
