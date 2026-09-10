"""Independence sentinel for the pure operation-catalog module."""

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


def test_pure_scope_failure_names_the_missing_explicit_dependency(request):
    if "runtime_test_scope" in request.fixturenames:
        result = subprocess.run(
            [sys.executable, "-I", "-c", "assert 6 * 7 == 42"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return
    with pytest.raises(AssertionError, match="request runtime_test_scope"):
        subprocess.run(["forbidden-in-pure-scope"], check=False)


@pytest.mark.usefixtures("runtime_test_scope")
def test_named_runtime_scope_enables_its_transitive_config_dependency():
    from beadhive import config, log

    assert config.home().name.startswith("bh-home")
    assert log.get_logger("explicit-runtime-scope") is not None


def test_operation_catalog_import_has_no_ambient_or_runtime_dependencies(monkeypatch):
    """Import and use a pure contract without constructing any outer-layer capability."""
    forbidden_imports = (
        "beadhive.config",
        "beadhive.dolt",
        "beadhive.dolt_health",
        "beadhive.plugins",
        "beadhive.otel",
        "beadhive.run",
        "beadhive.kernel.telemetry.instrumentation",
        "beadhive.kernel.telemetry.sinks",
    )
    for name in tuple(sys.modules):
        if name == "beadhive.operation_catalog" or name.startswith(
            ("beadhive.kernel.operations", "beadhive.kernel.telemetry", *forbidden_imports)
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterLayer(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"pure module imported outer-layer dependency: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"pure module attempted {action}")

        return fail

    finder = RefuseOuterLayer()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(Path, "read_text", forbidden("operator config read"))
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden("plugin discovery"))
    monkeypatch.setattr(socket, "create_connection", forbidden("Dolt/network connection"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("process spawn"))
    monkeypatch.setattr(os, "fork", forbidden("process fork"))
    monkeypatch.setattr(threading.Thread, "start", forbidden("runtime thread start"))
    try:
        catalog = importlib.import_module("beadhive.operation_catalog")
        spec = catalog.OperationSpec(
            name="sentinel",
            parameters=(),
            result_schema="urn:sentinel",
            kind="query",
            privilege="read",
            constraints={},
            surfaces={},
        )
        assert spec.name == "sentinel"
    finally:
        sys.meta_path.remove(finder)
