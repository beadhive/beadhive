"""Import and side-effect independence sentinel for the work module."""

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
from dataclasses import fields
from pathlib import Path


def test_work_module_import_has_no_outer_runtime_or_effects(monkeypatch) -> None:
    forbidden_imports = (
        "fastmcp",
        "typer",
        "beadhive.cli",
        "beadhive.mcp",
        "beadhive.bd",
        "beadhive.config",
        "beadhive.plugins",
        "beadhive.work",
        "beadhive.work_services",
    )
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.modules.work", *forbidden_imports)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"work module imported outer runtime: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"work module attempted {action}")

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
        module = importlib.import_module("beadhive.modules.work")
        assert module.WorkLifecycleService.__module__.startswith("beadhive.modules.work")
    finally:
        sys.meta_path.remove(finder)


def test_work_dtos_and_public_module_exclude_transport_vocabulary() -> None:
    from beadhive.modules import work

    dto_types = (
        work.AssignmentRequest,
        work.ClaimRequest,
        work.ScheduleRequest,
        work.CheckRequest,
        work.SubmissionRequest,
        work.ReviewRequest,
        work.MergeRequest,
        work.ResumeRequest,
        work.AbandonRequest,
    )
    forbidden = {"as_json", "payload", "text", "stream", "exit_code", "render"}
    for dto_type in dto_types:
        names = {field.name for field in fields(dto_type)}
        assert names.isdisjoint(forbidden), f"{dto_type.__name__} leaks {names & forbidden}"

    module_root = Path(__file__).parents[4] / "src" / "beadhive" / "modules" / "work"
    forbidden_dependencies = {"fastmcp", "typer", "beadhive.bd", "beadhive.work"}
    for path in sorted(module_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        leaked = {
            dependency
            for dependency in forbidden_dependencies
            if any(name == dependency or name.startswith(f"{dependency}.") for name in imports)
        }
        assert not leaked, f"{path.name} imports outer dependency {sorted(leaked)}"
