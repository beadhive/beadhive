"""Import and effect independence sentinel for the planning module."""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.metadata
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path


def test_planning_module_import_has_no_outer_runtime_or_effects(monkeypatch) -> None:
    forbidden_imports = (
        "fastmcp",
        "typer",
        "beadhive.bd",
        "beadhive.cli",
        "beadhive.mcp",
        "beadhive.plan",
        "beadhive.plan_repair",
        "beadhive.planning_services",
    )
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.modules.planning", *forbidden_imports)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"planning module imported outer runtime: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"planning module attempted {action}")

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
        module = importlib.import_module("beadhive.modules.planning")
        assert module.PlanningService.__module__.startswith("beadhive.modules.planning")
    finally:
        sys.meta_path.remove(finder)


def test_planning_module_has_no_transport_storage_or_legacy_imports() -> None:
    module_root = Path(__file__).parents[4] / "src" / "beadhive" / "modules" / "planning"
    forbidden = {"fastmcp", "typer", "beadhive.bd", "beadhive.plan", "beadhive.state"}
    for path in sorted(module_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        leaked = {
            item
            for item in forbidden
            if any(name == item or name.startswith(f"{item}.") for name in names)
        }
        assert not leaked, f"{path.name} imports outer dependency {sorted(leaked)}"
