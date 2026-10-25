"""Import and side-effect independence sentinel for the tracker module and its testing kit.

The module and :mod:`beadhive.testing.tracker` load, and every conformance case runs against
``MemoryTracker``, with the CLI, work, plan, bd, config and plugin layers, every HTTP client and
every tracker SDK refused at import time, and with no file read, process, thread, socket or real
sleep allowed.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.metadata
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_FORBIDDEN_IMPORTS = (
    "beadhive.cli",
    "beadhive.mcp",
    "beadhive.bd",
    "beadhive.config",
    "beadhive.plan",
    "beadhive.plugins",
    "beadhive.run",
    "beadhive.work",
    "beadhive.modules.work",
    "beadhive.modules.intake",
    "beadhive.kernel",
    "beadhive_core",
    "fastmcp",
    "typer",
    "httpx",
    "githubkit",
    "gidgethub",
    "github",
)
_UNDER_TEST = ("beadhive.modules.tracker", "beadhive.testing.tracker")


def test_tracker_module_and_kit_run_without_higher_layers_or_effects(monkeypatch) -> None:
    for name in tuple(sys.modules):
        if name.startswith((*_UNDER_TEST, *_FORBIDDEN_IMPORTS)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseHigherLayers(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(_FORBIDDEN_IMPORTS):
                raise AssertionError(f"tracker module imported a higher layer: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"tracker module attempted {action}")

        return fail

    finder = RefuseHigherLayers()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(Path, "read_text", forbidden("filesystem read"))
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden("plugin discovery"))
    monkeypatch.setattr(socket, "create_connection", forbidden("network connection"))
    monkeypatch.setattr(socket.socket, "connect", forbidden("network connection"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("process spawn"))
    monkeypatch.setattr(os, "fork", forbidden("process fork"))
    monkeypatch.setattr(threading.Thread, "start", forbidden("runtime thread start"))
    monkeypatch.setattr(time, "sleep", forbidden("a real sleep"))
    try:
        contracts = importlib.import_module("beadhive.modules.tracker.contracts")
        importlib.import_module("beadhive.modules.tracker.application")
        kit = importlib.import_module("beadhive.testing.tracker")
        kit.assert_tracker_conforms(kit.memory_subject, subject="memory")
        assert isinstance(kit.memory_subject().adapter, contracts.TrackerAdapter)
        loaded = {name for name in sys.modules if name.startswith(_FORBIDDEN_IMPORTS)}
        assert not loaded, sorted(loaded)
    finally:
        sys.meta_path.remove(finder)
