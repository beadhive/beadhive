"""Offline guards for spike bh-gj1hb: any real sleep or socket connection fails the test."""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _offline_and_clockless(monkeypatch):
    def no_sleep(*_a, **_k):
        raise AssertionError("a real sleep was attempted")

    def no_socket(*_a, **_k):
        raise AssertionError("a real network connection was attempted")

    monkeypatch.setattr(time, "sleep", no_sleep)
    monkeypatch.setattr(socket.socket, "connect", no_socket)
