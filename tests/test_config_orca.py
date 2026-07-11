"""config.orca_* accessors — resolution order + git-workspace gate + data path.

Mirrors test_config_observaloop.py. The orca gate is ``git_workspace.enabled`` (the flag
lives at ``cfg['git_workspace']['enabled']``), the analogue of observaloop's otel gate.
"""

from __future__ import annotations

from pathlib import Path

from beadhive import config

_GW_ON = {"git_workspace": {"enabled": True}}


# ---- orca_enabled -----------------------------------------------------------


def test_enabled_false_by_default():
    assert config.orca_enabled({}) is False


def test_enabled_false_when_git_workspace_disabled_global_flag_set():
    # git-workspace off → orca must be False regardless of its own flag
    cfg = {"git_workspace": {"enabled": False}, "orca": {"enabled": True}}
    assert config.orca_enabled(cfg) is False


def test_enabled_false_when_git_workspace_disabled_rig_flag_set():
    cfg = {"git_workspace": {"enabled": False}}
    entry = {"orca": {"enabled": True}}
    assert config.orca_enabled(cfg, entry) is False


def test_enabled_false_when_git_workspace_enabled_but_flag_absent():
    assert config.orca_enabled(_GW_ON) is False


def test_enabled_true_when_git_workspace_and_global_flag_set():
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True}}
    assert config.orca_enabled(cfg) is True


def test_enabled_true_when_git_workspace_and_rig_flag_set():
    cfg = {"git_workspace": {"enabled": True}}
    entry = {"orca": {"enabled": True}}
    assert config.orca_enabled(cfg, entry) is True


def test_rig_entry_overrides_global_false():
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": False}}
    entry = {"orca": {"enabled": True}}
    assert config.orca_enabled(cfg, entry) is True


def test_rig_entry_overrides_global_true():
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True}}
    entry = {"orca": {"enabled": False}}
    assert config.orca_enabled(cfg, entry) is False


def test_rig_entry_without_orca_key_falls_back_to_global():
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True}}
    assert config.orca_enabled(cfg, {}) is True


def test_rig_entry_with_empty_orca_section_falls_back_to_global():
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True}}
    assert config.orca_enabled(cfg, {"orca": {}}) is True


# ---- orca_cfg ---------------------------------------------------------------


def test_orca_cfg_defaults_empty():
    assert config.orca_cfg({}) == {}


def test_orca_cfg_returns_section():
    cfg = {"orca": {"enabled": True, "data_path": "/x/y.json"}}
    assert config.orca_cfg(cfg)["data_path"] == "/x/y.json"


# ---- orca_data_path ---------------------------------------------------------


def test_data_path_default_is_platform_config_home_darwin(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    expected = Path("~/Library/Application Support/orca/orca-data.json").expanduser()
    assert config.orca_data_path({}) == expected


def test_data_path_default_is_dot_config_elsewhere(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert config.orca_data_path({}) == Path("~/.config/orca/orca-data.json").expanduser()


def test_data_path_override_expanduser():
    cfg = {"orca": {"data_path": "~/custom/orca.json"}}
    assert config.orca_data_path(cfg) == Path("~/custom/orca.json").expanduser()


# ---- orca_worktrees_enabled --------------------------------------------------


def test_worktrees_disabled_by_default():
    assert config.orca_worktrees_enabled(_GW_ON) is False


def test_worktrees_off_when_orca_enabled_false():
    # orca itself off (git-workspace disabled) → worktrees False even if the flag is set
    cfg = {"git_workspace": {"enabled": False}, "orca": {"enabled": True, "worktrees": True}}
    assert config.orca_worktrees_enabled(cfg) is False


def test_worktrees_true_when_global_flag_set():
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True, "worktrees": True}}
    assert config.orca_worktrees_enabled(cfg) is True


def test_worktrees_true_when_global_flag_is_enabled_mapping():
    cfg = {
        "git_workspace": {"enabled": True},
        "orca": {"enabled": True, "worktrees": {"enabled": True}},
    }
    assert config.orca_worktrees_enabled(cfg) is True


def test_worktrees_rig_entry_overrides_global_true():
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True, "worktrees": True}}
    entry = {"orca": {"enabled": True, "worktrees": False}}
    assert config.orca_worktrees_enabled(cfg, entry) is False


def test_worktrees_rig_entry_overrides_global_false():
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True, "worktrees": False}}
    entry = {"orca": {"enabled": True, "worktrees": True}}
    assert config.orca_worktrees_enabled(cfg, entry) is True


# ---- orca_worktrees_fallback --------------------------------------------------


def test_worktrees_fallback_default_false():
    assert config.orca_worktrees_fallback({}) is False


def test_worktrees_fallback_true_when_set():
    cfg = {"orca": {"worktrees": {"fallback": True}}}
    assert config.orca_worktrees_fallback(cfg) is True


def test_worktrees_fallback_false_when_worktrees_is_bare_bool():
    cfg = {"orca": {"worktrees": True}}
    assert config.orca_worktrees_fallback(cfg) is False
