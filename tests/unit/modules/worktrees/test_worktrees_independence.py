"""Import and dependency-direction sentinels for the worktrees module."""

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


def test_worktrees_module_import_has_no_outer_runtime_or_effects(monkeypatch) -> None:
    forbidden_imports = (
        "fastmcp",
        "typer",
        "beadhive.cli",
        "beadhive.mcp",
        "beadhive.config",
        "beadhive.plugins",
        "beadhive.registry",
        "beadhive.worktree",
        "beadhive.work",
    )
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.modules.worktrees", *forbidden_imports)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"worktrees module imported outer runtime: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"worktrees module attempted {action}")

        return fail

    finder = RefuseOuterRuntime()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(Path, "read_text", forbidden("filesystem read"))
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden("plugin discovery"))
    monkeypatch.setattr(socket, "create_connection", forbidden("network connection"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("process spawn"))
    monkeypatch.setattr(os, "fork", forbidden("process fork"))
    monkeypatch.setattr(threading.Thread, "start", forbidden("runtime thread start"))
    try:
        worktrees = importlib.import_module("beadhive.modules.worktrees")
        assert worktrees.WorktreeBinding.__module__.endswith("domain.models")
    finally:
        sys.meta_path.remove(finder)
