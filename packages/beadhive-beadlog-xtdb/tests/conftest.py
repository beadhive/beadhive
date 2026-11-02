"""Shared fixtures for the XTDB store plugin's tests: offline by construction.

No test here reaches a node: every connection, request and socket goes through an injected
fake (:mod:`xtdb_plugin_fakes`), and real sockets are refused so a missed injection fails
loudly. The real node is exercised only by root's integration-marked
``tests/test_backfill_store_xtdb_int.py``.
"""

from __future__ import annotations

import socket

import pytest


class NetworkRefused(AssertionError):
    """A test tried to reach the network; inject a fake probe or connector instead."""


def _refuse(*_args, **_kwargs):
    raise NetworkRefused("no network in the beadlog-xtdb plugin tests: inject a fake")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
