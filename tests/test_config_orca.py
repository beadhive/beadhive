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


def test_data_path_default_is_config_home():
    assert config.orca_data_path({}) == Path("~/.config/orca/orca-data.json").expanduser()


def test_data_path_override_expanduser():
    cfg = {"orca": {"data_path": "~/custom/orca.json"}}
    assert config.orca_data_path(cfg) == Path("~/custom/orca.json").expanduser()
