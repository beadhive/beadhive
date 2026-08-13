"""hive_ready checks in plugin mode — _has_bundled_skill and _has_bundled_agent accept
the bh plugin install in lieu of local files; copy-mode checks remain unchanged."""

from __future__ import annotations

import json
from unittest.mock import patch

from beadhive import hive_ready

# ---- _is_plugin_installed ----
#
# Redirected with $BH_CLAUDE_HOME rather than `patch.object(Path, "home")` (bh-nvv66). The seam
# is now the supported one, and it is also the honest one: three of the four cases below assert
# FALSE, which an unpatched real home would usually produce too, so a redirect that silently
# stopped working would leave them green while testing nothing. Pointing the env var at a
# directory this test builds means each case fails if the reader stops reading what it claims to.


def _claude_home(monkeypatch, tmp_path):
    """Point config.claude_home() at *tmp_path* and return its plugins dir (not yet created)."""
    monkeypatch.setenv("BH_CLAUDE_HOME", str(tmp_path))
    return tmp_path / "plugins"


def test_is_plugin_installed_true_when_key_present(tmp_path, monkeypatch):
    plugins_dir = _claude_home(monkeypatch, tmp_path)
    plugins_dir.mkdir(parents=True)
    installed = {"version": 2, "plugins": {"bh@workspace": [{"scope": "user"}]}}
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(installed))

    assert hive_ready._is_plugin_installed("bh") is True


def test_is_plugin_installed_false_when_key_absent(tmp_path, monkeypatch):
    plugins_dir = _claude_home(monkeypatch, tmp_path)
    plugins_dir.mkdir(parents=True)
    installed = {"version": 2, "plugins": {"other@mp": [{"scope": "user"}]}}
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(installed))

    assert hive_ready._is_plugin_installed("bh") is False


def test_is_plugin_installed_false_when_file_absent(tmp_path, monkeypatch):
    _claude_home(monkeypatch, tmp_path)

    assert hive_ready._is_plugin_installed("bh") is False


def test_is_plugin_installed_false_on_malformed_json(tmp_path, monkeypatch):
    plugins_dir = _claude_home(monkeypatch, tmp_path)
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text("not-json{")

    assert hive_ready._is_plugin_installed("bh") is False


# ---- _has_bundled_skill: plugin mode ----


def test_has_bundled_skill_true_when_plugin_installed():
    cfg = {"claude": {"source": "plugin", "plugin": "bh"}}
    with patch("beadhive.hive_ready._is_plugin_installed", return_value=True):
        assert hive_ready._has_bundled_skill(cfg, None) is True


def test_has_bundled_skill_false_when_plugin_not_installed_and_no_local(tmp_path):
    cfg = {"claude": {"source": "plugin", "plugin": "bh"}}
    with (
        patch("beadhive.hive_ready._is_plugin_installed", return_value=False),
        patch("pathlib.Path.is_dir", return_value=False),
    ):
        assert hive_ready._has_bundled_skill(cfg, None) is False


def test_has_bundled_skill_copy_mode_checks_local_dir(tmp_path, monkeypatch, fake_plugin):
    """In copy mode, the check uses local skills/ directory (original behaviour)."""
    from beadhive import config

    cfg = {"claude": {"source": "copy"}}
    monkeypatch.chdir(tmp_path)
    # No local skills/ dir
    assert hive_ready._has_bundled_skill(cfg, None) is False
    # Create a matching skill dir
    skill_name = next(p.name for p in config.skills_src().iterdir() if p.is_dir())
    (tmp_path / "skills" / skill_name).mkdir(parents=True)
    assert hive_ready._has_bundled_skill(cfg, None) is True


# ---- _has_bundled_agent: plugin mode ----


def test_has_bundled_agent_true_when_plugin_installed():
    cfg = {"claude": {"source": "plugin", "plugin": "bh"}}
    with patch("beadhive.hive_ready._is_plugin_installed", return_value=True):
        assert hive_ready._has_bundled_agent(cfg, None) is True


def test_has_bundled_agent_false_when_plugin_not_installed_and_no_local(tmp_path, monkeypatch):
    cfg = {"claude": {"source": "plugin", "plugin": "bh"}}
    monkeypatch.chdir(tmp_path)
    with patch("beadhive.hive_ready._is_plugin_installed", return_value=False):
        assert hive_ready._has_bundled_agent(cfg, None) is False


def test_has_bundled_agent_copy_mode_checks_local_dir(tmp_path, monkeypatch, fake_plugin):
    """In copy mode, the check uses local .claude/agents/ (original behaviour)."""
    cfg = {"claude": {"source": "copy"}}
    monkeypatch.chdir(tmp_path)
    assert hive_ready._has_bundled_agent(cfg, None) is False
    # Create a matching agent file
    from beadhive import config

    agent_name = next(p.name for p in config.agents_src().iterdir() if p.suffix == ".md")
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / agent_name).write_text("agent\n")
    assert hive_ready._has_bundled_agent(cfg, None) is True
