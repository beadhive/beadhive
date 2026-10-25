"""Offline guards for spike bh-rktrh: any real sleep, socket connection or subprocess fails."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _offline_clockless_subprocessless(monkeypatch):
    def refuse(what):
        def inner(*_a, **_k):
            raise AssertionError(f"{what} was attempted")

        return inner

    monkeypatch.setattr(time, "sleep", refuse("a real sleep"))
    monkeypatch.setattr(socket.socket, "connect", refuse("a real network connection"))
    monkeypatch.setattr(subprocess, "Popen", refuse("a subprocess"))
