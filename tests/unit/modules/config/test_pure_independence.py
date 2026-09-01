from __future__ import annotations

import builtins
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

from harness import processes

ROOT = Path(__file__).resolve().parents[4]


def _deny_pure_effects(monkeypatch):
    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"pure config attempted {action}")

        return fail

    def guarded_os(name):
        original = getattr(os, name)

        def guard(*args, **kwargs):
            caller = sys._getframe(1)
            caller_file = caller.f_code.co_filename
            frame = caller
            loading = False
            while frame is not None:
                module_name = frame.f_globals.get("__name__", "")
                if module_name.startswith("importlib") or frame.f_code.co_filename.startswith(
                    "<frozen importlib"
                ):
                    loading = True
                    break
                frame = frame.f_back
            if loading and not caller_file.startswith(str(ROOT)):
                return original(*args, **kwargs)
            raise AssertionError(f"pure config attempted os.{name}")

        return guard

    monkeypatch.setattr(builtins, "open", forbidden("builtins.open filesystem access"))
    for name in (
        "open",
        "read_bytes",
        "read_text",
        "write_bytes",
        "write_text",
        "iterdir",
        "glob",
        "rglob",
        "mkdir",
        "touch",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "chmod",
        "stat",
        "lstat",
    ):
        monkeypatch.setattr(Path, name, forbidden(f"Path.{name} filesystem access"))
    for name in (
        "open",
        "stat",
        "lstat",
        "listdir",
        "scandir",
        "mkdir",
        "makedirs",
        "remove",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "removedirs",
        "link",
        "symlink",
        "readlink",
        "chmod",
        "utime",
        "truncate",
        "walk",
        "chdir",
    ):
        monkeypatch.setattr(os, name, guarded_os(name))
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden("plugin discovery"))
    monkeypatch.setattr(socket, "create_connection", forbidden("network or Dolt connection"))
    monkeypatch.setattr(socket.socket, "connect", forbidden("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden("socket.connect_ex"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("subprocess spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("subprocess spawn"))
    monkeypatch.setattr(os, "system", forbidden("os.system process spawn"))
    monkeypatch.setattr(os, "popen", forbidden("os.popen process spawn"))
    monkeypatch.setattr(threading.Thread, "start", forbidden("runtime thread"))
    monkeypatch.setattr(
        processes.multiprocessing.process.BaseProcess,
        "start",
        forbidden("runtime process"),
    )


def _connect_with(method):
    client = socket.socket.__new__(socket.socket)
    getattr(client, method)(("127.0.0.1", 9))


@pytest.mark.parametrize(
    "attempt",
    [
        lambda: Path("pyproject.toml").open(),
        lambda: builtins.open("pyproject.toml"),
        lambda: os.open("pyproject.toml", os.O_RDONLY),
        lambda: os.stat("pyproject.toml"),
        lambda: _connect_with("connect"),
        lambda: _connect_with("connect_ex"),
        lambda: subprocess.run(["false"]),
        lambda: threading.Thread(target=lambda: None).start(),
        lambda: processes.process_context().Process(target=lambda: None).start(),
    ],
    ids=(
        "path-open",
        "builtins-open",
        "os-open",
        "os-stat",
        "socket-connect",
        "socket-connect-ex",
        "subprocess",
        "thread",
        "process",
    ),
)
def test_pure_effect_sentinel_rejects_supported_bypass_entry_points(monkeypatch, attempt):
    with monkeypatch.context() as effects:
        _deny_pure_effects(effects)

        with pytest.raises(AssertionError, match="pure config attempted"):
            attempt()


def test_pure_config_models_and_resolution_need_no_outer_runtime(monkeypatch):
    forbidden_imports = (
        "beadhive.config",
        "beadhive.config_store",
        "beadhive.dolt",
        "beadhive.plugins",
        "beadhive.otel",
        "beadhive.cli",
        "beadhive.mcp",
    )
    for name in tuple(sys.modules):
        if name.startswith("beadhive.modules.config") or name.startswith(forbidden_imports):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"pure config imported outer runtime: {fullname}")
            return None

    finder = RefuseOuterRuntime()
    sys.meta_path.insert(0, finder)
    try:
        with monkeypatch.context() as effects:
            _deny_pure_effects(effects)
            resolution = importlib.import_module("beadhive.modules.config.application.resolution")
            contracts = importlib.import_module("beadhive.modules.config.contracts")
            resolved = resolution.resolve_config(
                resolution.ResolutionInputs(
                    fleet={"work": {"runtime": "local"}},
                    host={},
                    hive={},
                    environment={},
                    runtime={},
                )
            )
            assert isinstance(resolved.settings, contracts.BeadhiveConfig)
            assert resolved.settings.work.runtime == "local"
            assert resolved.provenance["work.runtime"].layer is resolution.SourceLayer.FLEET
    finally:
        sys.meta_path.remove(finder)
