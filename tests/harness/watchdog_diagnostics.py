"""Secret-safe pytest worker state and signal-triggered stack diagnostics."""

from __future__ import annotations

import faulthandler
import json
import os
import signal
from pathlib import Path
from typing import TextIO

import pytest

_state_path: Path | None = None
_stack_file: TextIO | None = None
_signal_registered = False


def _safe_nodeid(nodeid: str) -> str:
    """Identify the test without exposing parameter values or an absolute path."""
    path, *parts = nodeid.split("::")
    safe_parts = [part.split("[", maxsplit=1)[0] for part in parts]
    return "::".join([Path(path).name, *safe_parts])


def _write_state(nodeid: str | None) -> None:
    if _state_path is None:
        return
    value = {
        "pid": os.getpid(),
        "worker": os.environ.get("PYTEST_XDIST_WORKER", "controller"),
        "nodeid": _safe_nodeid(nodeid) if nodeid else None,
        "signal_ready": _signal_registered,
    }
    staged = _state_path.with_suffix(".tmp")
    staged.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(staged, _state_path)


def pytest_configure(config) -> None:
    del config
    global _signal_registered, _stack_file, _state_path
    root_value = os.environ.get("BH_TEST_ACTIVE_DIR")
    if not root_value:
        return
    root = Path(root_value)
    root.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    _state_path = root / f"{pid}.json"
    _stack_file = (root / f"{pid}.stack").open("a", encoding="utf-8")
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, file=_stack_file, all_threads=True, chain=False)
        _signal_registered = True
    _write_state(None)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    del nextitem
    _write_state(item.nodeid)
    try:
        yield
    finally:
        _write_state(None)


def pytest_unconfigure(config) -> None:
    del config
    global _signal_registered, _stack_file
    if _signal_registered and hasattr(signal, "SIGUSR1"):
        faulthandler.unregister(signal.SIGUSR1)
        _signal_registered = False
    if _stack_file is not None:
        _stack_file.close()
        _stack_file = None
