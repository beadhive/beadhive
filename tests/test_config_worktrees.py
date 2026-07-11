"""worktrees_root / worktrees_ephemeral resolution — the money path: ephemeral default,
the temp-dir landing, the path override gated to persistent mode, and $BH_WORKTREES winning."""

from __future__ import annotations

import tempfile
from pathlib import Path

from beadhive import config


def test_ephemeral_default_true_when_omitted():
    assert config.worktrees_ephemeral({}) is True
    assert config.worktrees_ephemeral({"worktrees": {}}) is True


def test_ephemeral_root_is_os_temp_and_ignores_path(monkeypatch):
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)
    cfg = {"worktrees": {"ephemeral": True, "path": "/should/be/ignored"}}
    root = config.worktrees_root(cfg)
    assert root == Path(tempfile.gettempdir()) / "bh-worktrees"


def test_persistent_uses_path_then_default(monkeypatch):
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)
    assert config.worktrees_root({"worktrees": {"ephemeral": False, "path": "/srv/wt"}}) == Path(
        "/srv/wt"
    )
    monkeypatch.setenv("BH_HOME", "/tmp/wshome")
    assert config.worktrees_root({"worktrees": {"ephemeral": False}}) == Path(
        "/tmp/wshome/worktrees"
    )


def test_bh_worktrees_env_overrides_both_modes(monkeypatch):
    monkeypatch.setenv("BH_WORKTREES", "/explicit/override")
    for ephemeral in (True, False):
        assert config.worktrees_root({"worktrees": {"ephemeral": ephemeral}}) == Path(
            "/explicit/override"
        )
