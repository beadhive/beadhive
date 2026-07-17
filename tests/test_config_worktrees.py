"""worktrees_root / worktrees_ephemeral resolution — the money path: ephemeral default,
the temp-dir landing, the path override gated to persistent mode, and $BH_WORKTREES winning.
Plus the shipped init-rule defaults (verify flags, bh-7k1p)."""

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


def test_config_example_init_defaults_flag_verify():
    """The shipped template parses with the verify flag, and the defaults draw the line the
    verify-environment contract demands (bh-7k1p): dependency sync ('uv sync') and trust stamps
    ('mise trust') are verify: true (they run per clean-checkout validation); heavy seat
    provisioning (the probe-guarded 'just setup' rule, bh-17n4) stays unflagged."""
    data = config._yaml.load(config.template("config.example.yaml").read_text())
    rules = {r["run"]: dict(r) for r in data["worktrees"]["init"]}
    assert rules["mise trust"].get("verify") is True
    assert rules["uv sync"].get("verify") is True
    (just_rule,) = [r for run, r in rules.items() if "just setup" in run]
    assert "verify" not in just_rule
