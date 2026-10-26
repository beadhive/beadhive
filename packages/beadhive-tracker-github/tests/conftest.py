"""Shared fixtures for the GitHub adapter's tests: offline by construction.

Every test in this package runs with the real network transport and outbound sockets disabled,
so a test that forgot to inject an ``httpx.MockTransport`` fails loudly instead of reaching
GitHub. Nothing here reads a clock or sleeps: the adapter's ``sleep`` and ``clock`` are the fake
world's (:class:`tracker_github_fake.FakeClock`).
"""

from __future__ import annotations

import socket

import httpx
import pytest


class NetworkRefused(AssertionError):
    """A test tried to reach the network; every request must go through a MockTransport."""


def _refuse(*_args, **_kwargs):
    raise NetworkRefused("no network in the tracker-github tests: inject an httpx.MockTransport")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
