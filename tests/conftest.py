"""Shared pytest fixtures + markers for the AGF harness."""

from __future__ import annotations

import pytest

from harness.world import World


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)
