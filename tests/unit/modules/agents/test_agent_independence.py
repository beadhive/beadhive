"""Import and runtime independence sentinel for the agents module."""

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


def test_agents_module_import_has_no_framework_provider_or_ambient_dependencies(monkeypatch):
    forbidden_imports = (
        "fastmcp",
        "herdr",
        "opentelemetry",
        "starlette",
        "typer",
        "beadhive.config",
        "beadhive.herdr",
        "beadhive.host_daemon",
        "beadhive.plugins",
    )
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.modules.agents", *forbidden_imports)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterLayer(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"agents module imported forbidden dependency: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"agents module attempted {action}")

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
        agents = importlib.import_module("beadhive.modules.agents")
        assert agents.SeatContract.__module__ == "beadhive.modules.agents.domain.seat"
    finally:
        sys.meta_path.remove(finder)
