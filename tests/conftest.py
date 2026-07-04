"""Shared pytest fixtures + markers for the AGF harness."""

from __future__ import annotations

import os

import pytest

from harness.world import World
from ws import otel


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
        if key.startswith("OTEL_") or key == "WS_OBSERVALOOP_PROFILE":
            monkeypatch.delenv(key, raising=False)
    otel.shutdown()  # reset any _initialized state leaked from a prior test
    # Bypass the setup gate for all tests unless they explicitly clear this env var.
    # test_setup.py tests that exercise the gate use monkeypatch.delenv to remove it.
    monkeypatch.setenv("WS_SKIP_SETUP_CHECK", "1")


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)
