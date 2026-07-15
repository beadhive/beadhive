"""gitworkspace_plugin.py — promotes git-workspace to a bh Plugin (bh-4y0r.4).

Mirrors test_orca.py / test_plugin_cli.py's style: hermetic $GIT_WORKSPACE fixtures + the
in-process Typer CliRunner (not the installed bh binary).
"""

from __future__ import annotations

from typer.testing import CliRunner

from beadhive import gitworkspace, gitworkspace_plugin, orca, plugins, rig_ready
from beadhive.cli import app

runner = CliRunner()


# ---- plugins.registry() -------------------------------------------------------


def test_registry_includes_git_workspace_then_orca():
    reg = plugins.registry()
    names = [p.name for p in reg]
    assert names == ["git-workspace", "orca"]


def test_plugin_is_gated_on_gitworkspace_enabled():
    assert gitworkspace_plugin.PLUGIN.enabled({"git_workspace": {"enabled": False}}, None) is False
    assert gitworkspace_plugin.PLUGIN.enabled({"git_workspace": {"enabled": True}}, None) is True


# ---- readiness -----------------------------------------------------------------


def test_readiness_warns_when_git_workspace_env_unset(monkeypatch):
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    state, detail = gitworkspace_plugin._readiness({}, None)
    assert state == "warn"
    assert "GIT_WORKSPACE" in detail


def test_readiness_missing_when_no_workspace_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    state, detail = gitworkspace_plugin._readiness({}, None)
    assert state == "missing"


def test_readiness_warns_when_no_lockfile(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    )
    state, detail = gitworkspace_plugin._readiness({}, None)
    assert state == "warn"
    assert "workspace-lock.toml" in detail


def test_readiness_ok_when_fully_set_up(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    )
    (tmp_path / "workspace-lock.toml").write_text("")
    state, detail = gitworkspace_plugin._readiness({}, None)
    assert state == "ok"
    assert "1 repo groups" in detail


def test_rig_ready_plugin_checks_includes_git_workspace_line(monkeypatch):
    entry = {"provider": "github", "org": "acme", "repo": "api", "prefix": "a-api"}
    monkeypatch.setattr(gitworkspace, "enabled", lambda cfg: False)
    checks = rig_ready._plugin_checks({}, entry)
    line = next(c for c in checks if c.label == "git-workspace")
    assert line.state == "na"


# ---- bh plugin git-workspace groups -------------------------------------------


def test_plugin_groups_cmd_lists_repo_groups(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "contrib"\n'
        "skip_forks = true\n"
    )
    result = runner.invoke(app, ["plugin", "git-workspace", "groups"])
    assert result.exit_code == 0, result.output
    assert "contrib" in result.output
    assert "provider=github" in result.output
    assert "skip_forks" in result.output


def test_plugin_groups_cmd_empty_message(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    result = runner.invoke(app, ["plugin", "git-workspace", "groups"])
    assert result.exit_code == 0, result.output
    assert "no repo groups found" in result.output


def test_plugin_tree_help_lists_git_workspace():
    result = runner.invoke(app, ["plugin", "--help"])
    assert result.exit_code == 0
    assert "git-workspace" in result.output


# ---- orca AND-gate preserved (regression guard alongside test_config_orca.py) --


def test_orca_still_and_gates_on_git_workspace_enabled():
    from beadhive import config

    cfg = {"git_workspace": {"enabled": False}, "orca": {"enabled": True}}
    assert config.orca_enabled(cfg) is False
    assert orca.PLUGIN.enabled(cfg, None) is False
