"""config.orca_* accessors — resolution order + data path.

Mirrors test_config_observaloop.py. bh-hsus.4: `orca_enabled` used to AND-gate on
`git_workspace.enabled` (the flag lived at `cfg['git_workspace']['enabled']`); that flag was
deleted (git-workspace is now a required dep, always present), so the gate is gone and orca's
own flag is the only thing this resolves.
"""

from __future__ import annotations

from pathlib import Path

from beadhive import config

# ---- orca_enabled -----------------------------------------------------------


def test_enabled_false_by_default():
    assert config.orca_enabled({}) is False


def test_enabled_true_when_global_flag_set():
    cfg = {"orca": {"enabled": True}}
    assert config.orca_enabled(cfg) is True


def test_enabled_true_when_hive_flag_set():
    entry = {"orca": {"enabled": True}}
    assert config.orca_enabled({}, entry) is True


def test_hive_entry_overrides_global_false():
    cfg = {"orca": {"enabled": False}}
    entry = {"orca": {"enabled": True}}
    assert config.orca_enabled(cfg, entry) is True


def test_hive_entry_overrides_global_true():
    cfg = {"orca": {"enabled": True}}
    entry = {"orca": {"enabled": False}}
    assert config.orca_enabled(cfg, entry) is False


def test_hive_entry_without_orca_key_falls_back_to_global():
    cfg = {"orca": {"enabled": True}}
    assert config.orca_enabled(cfg, {}) is True


def test_hive_entry_with_empty_orca_section_falls_back_to_global():
    cfg = {"orca": {"enabled": True}}
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
    assert config.orca_worktrees_enabled({}) is False


def test_worktrees_off_when_orca_enabled_false():
    cfg = {"orca": {"enabled": False, "worktrees": True}}
    assert config.orca_worktrees_enabled(cfg) is False


def test_worktrees_true_when_global_flag_set():
    cfg = {"orca": {"enabled": True, "worktrees": True}}
    assert config.orca_worktrees_enabled(cfg) is True


def test_worktrees_true_when_global_flag_is_enabled_mapping():
    cfg = {"orca": {"enabled": True, "worktrees": {"enabled": True}}}
    assert config.orca_worktrees_enabled(cfg) is True


def test_worktrees_hive_entry_overrides_global_true():
    cfg = {"orca": {"enabled": True, "worktrees": True}}
    entry = {"orca": {"enabled": True, "worktrees": False}}
    assert config.orca_worktrees_enabled(cfg, entry) is False


def test_worktrees_hive_entry_overrides_global_false():
    cfg = {"orca": {"enabled": True, "worktrees": False}}
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


# ---- retired orca worktree delegation (bh-055ot.1) ---------------------------


def test_retired_scopes_empty_when_the_flag_is_off():
    assert config.orca_worktrees_retired_scopes({"orca": {"enabled": True}}) == []


def test_retired_scopes_name_the_global_flag():
    cfg = {"orca": {"enabled": True, "worktrees": {"enabled": True}}}
    assert config.orca_worktrees_retired_scopes(cfg) == ["orca.worktrees"]


def test_retired_scopes_name_a_per_hive_override():
    entry = {"prefix": "api", "orca": {"enabled": True, "worktrees": True}}
    cfg = {"managed_repos": [entry]}
    assert config.orca_worktrees_retired_scopes(cfg) == ["managed_repos[api].orca.worktrees"]


def test_retired_message_is_clear_and_actionable():
    message = config.orca_worktrees_retired_message(["orca.worktrees"])
    assert "orca.worktrees" in message
    assert "retired" in message
    assert "worktrees.manager" in message
    assert "Remove orca.worktrees" in message


def test_warn_retired_orca_worktrees_emits_one_config_load_warning(monkeypatch):
    from structlog.testing import capture_logs

    from beadhive import log

    cfg = {"orca": {"enabled": True, "worktrees": {"enabled": True, "fallback": True}}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    log.get_logger("warmup")
    with capture_logs() as captured:
        config.warn_retired_orca_worktrees_if_needed()

    warnings = [e for e in captured if e["event"] == config.ORCA_WORKTREES_RETIRED_EVENT]
    assert len(warnings) == 1
    assert warnings[0]["scopes"] == ["orca.worktrees"]
    assert "retired" in warnings[0]["hint"]


def test_warn_retired_orca_worktrees_is_silent_without_the_flag(monkeypatch):
    from structlog.testing import capture_logs

    from beadhive import log

    monkeypatch.setattr(config, "load", lambda: {"orca": {"enabled": True}})
    log.get_logger("warmup")
    with capture_logs() as captured:
        config.warn_retired_orca_worktrees_if_needed()

    assert not [e for e in captured if e["event"] == config.ORCA_WORKTREES_RETIRED_EVENT]
