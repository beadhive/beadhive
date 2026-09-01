"""Import and side-effect independence sentinel for the hives module."""

from __future__ import annotations

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


def test_hives_module_import_has_no_outer_runtime_or_effects(monkeypatch) -> None:
    forbidden_imports = (
        "fastmcp",
        "typer",
        "beadhive.cli",
        "beadhive.mcp",
        "beadhive.config",
        "beadhive.gitworkspace",
        "beadhive.plugins",
        "beadhive.registry",
        "beadhive.retire",
        "beadhive.hive_ready",
    )
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.modules.hives", *forbidden_imports)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"hives module imported outer runtime: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"hives module attempted {action}")

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
        hives = importlib.import_module("beadhive.modules.hives")
        assert hives.HiveIdentity.__module__ == "beadhive.modules.hives.domain.models"
    finally:
        sys.meta_path.remove(finder)


def test_hive_dtos_exclude_transport_presentation_vocabulary() -> None:
    from beadhive.modules import hives

    dto_types = (
        hives.HiveListRequest,
        hives.HiveListResult,
        hives.HiveStatusResult,
        hives.OnboardHiveRequest,
        hives.OnboardHiveResult,
        hives.ReadinessRequest,
        hives.ReadinessResult,
        hives.RetireHiveRequest,
        hives.RetireHiveResult,
    )
    forbidden = {"machine", "as_json", "payload", "text", "stream", "exit_code", "render"}

    for dto_type in dto_types:
        names = {field.name for field in fields(dto_type)}
        assert names.isdisjoint(forbidden), f"{dto_type.__name__} leaks {names & forbidden}"
