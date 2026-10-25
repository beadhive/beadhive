"""Offline guards for spike bh-6bkun: any real sleep, socket connection or subprocess fails."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "bh-rktrh"))  # compose/stub/credentials prototypes (reused)
sys.path.insert(0, str(HERE.parent / "bh-gj1hb"))  # the port contract prototype (reused)


@pytest.fixture(autouse=True)
def _offline_clockless_subprocessless(monkeypatch):
    # BeadhiveConfig is a pydantic-settings class (env_prefix BH_): keep the host env out of it.
    for name in [n for n in os.environ if n.startswith("BH_")]:
        monkeypatch.delenv(name)

    def refuse(what):
        def inner(*_a, **_k):
            raise AssertionError(f"{what} was attempted")

        return inner

    monkeypatch.setattr(time, "sleep", refuse("a real sleep"))
    monkeypatch.setattr(socket.socket, "connect", refuse("a real network connection"))
    monkeypatch.setattr(subprocess, "Popen", refuse("a subprocess"))
