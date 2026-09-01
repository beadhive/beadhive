"""Import isolation and optional real-provider proof for the Herdr integration."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.metadata
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest


def test_herdr_integration_import_has_no_runtime_or_ambient_dependencies(monkeypatch) -> None:
    forbidden_imports = (
        "fastmcp",
        "herdr",
        "opentelemetry",
        "starlette",
        "typer",
        "beadhive.config",
        "beadhive.herdr_plugin",
        "beadhive.host_daemon",
        "beadhive.plugins",
        "beadhive.work",
        "beadhive.worktree",
    )
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.integrations.herdr", *forbidden_imports)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterLayer(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"Herdr integration imported forbidden dependency: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"Herdr integration attempted {action}")

        return fail

    finder = RefuseOuterLayer()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(Path, "read_text", forbidden("operator config read"))
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden("plugin discovery"))
    monkeypatch.setattr(socket, "create_connection", forbidden("network connection"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("process spawn"))
    monkeypatch.setattr(os, "fork", forbidden("process fork"))
    monkeypatch.setattr(threading.Thread, "start", forbidden("runtime thread start"))
    try:
        integration = importlib.import_module("beadhive.integrations.herdr")
        assert integration.HerdrAgentAdapter.__module__.endswith(".agent_adapter")
    finally:
        sys.meta_path.remove(finder)


@pytest.mark.integration
def test_real_herdr_protocol_smoke_is_explicitly_opt_in() -> None:
    """Probe a real provider only when the operator names an isolated test session."""

    session = os.environ.get("BH_TEST_HERDR_SESSION")
    if not session:
        pytest.skip("set BH_TEST_HERDR_SESSION to exercise an isolated real Herdr server")

    from beadhive.integrations.herdr import HerdrClient

    result = HerdrClient(session=session).command("status", timeout=5.0)
    assert result.is_ok, result.failure
