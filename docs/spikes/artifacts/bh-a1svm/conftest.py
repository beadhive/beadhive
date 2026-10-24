"""Shared fixtures for the spike's offline tests (see test_transport_spike.py)."""

from __future__ import annotations

import time

import pytest
from fake_github import FakeClock, FakeGitHub


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a real sleep was attempted")

    monkeypatch.setattr(time, "sleep", boom)
    import anyio  # a dependency of httpx, so always present

    monkeypatch.setattr(anyio, "sleep", boom)


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
