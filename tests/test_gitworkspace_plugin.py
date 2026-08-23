"""gitworkspace_plugin.py — git-workspace's `bh plugin`-shaped CLI + readiness surface.

bh-hsus.4: git-workspace moved from `plugins.registry()` (an optional integration, gated on
the now-deleted `git_workspace.enabled` flag) to `deps.py` (a required dep, `required=ALWAYS`).
It is no longer a `plugins.Plugin` — `gitworkspace_plugin.cli` is mounted directly by `cli.py`
and `gitworkspace_plugin.readiness` is called directly by `hive_ready.py`, both explicitly
rather than through the generic plugin loop. Mirrors test_orca.py / test_plugin_cli.py's style:
hermetic $GIT_WORKSPACE fixtures + the in-process Typer CliRunner (not the installed bh binary).
"""

from __future__ import annotations

from typer.testing import CliRunner

from beadhive import deps, gitworkspace_plugin, hive_ready, plugins
from beadhive.cli import app

runner = CliRunner()


# ---- git-workspace is a dep, not a plugin ---------------------------------------


def test_git_workspace_is_not_in_the_plugin_registry():
    names = [p.name for p in plugins.registry()]
    assert names == ["orca", "observaloop", "hitch", "herdr"]
    assert "git-workspace" not in names


def test_git_workspace_is_a_required_dep():
    dep = deps.by_name("git-workspace")
    assert dep.required == deps.ALWAYS


# ---- readiness -----------------------------------------------------------------


def test_readiness_warns_when_git_workspace_env_unset(monkeypatch):
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    state, detail = gitworkspace_plugin.readiness({}, None)
    assert state == "warn"
    assert "GIT_WORKSPACE" in detail


def test_readiness_missing_when_no_workspace_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    state, detail = gitworkspace_plugin.readiness({}, None)
    assert state == "missing"


def test_readiness_warns_when_no_lockfile(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    )
    state, detail = gitworkspace_plugin.readiness({}, None)
    assert state == "warn"
    assert "workspace-lock.toml" in detail


def test_readiness_ok_when_fully_set_up(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    )
    (tmp_path / "workspace-lock.toml").write_text("")
    state, detail = gitworkspace_plugin.readiness({}, None)
    assert state == "ok"
    assert "1 repo groups" in detail


def test_hive_ready_scan_includes_git_workspace_line(monkeypatch):
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    check = hive_ready._git_workspace_check({}, None)
    assert check.label == "git-workspace"
    assert check.state == "warn"  # GIT_WORKSPACE unset in this hermetic test


# ---- bh plugin git-workspace groups -------------------------------------------


def test_plugin_groups_cmd_lists_repo_groups(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "contrib"\nskip_forks = true\n'
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


# ---- orca no longer AND-gates on git-workspace (bh-hsus.4) --------------------


def test_orca_no_longer_and_gates_on_git_workspace():
    from beadhive import config

    cfg = {"orca": {"enabled": True}}
    assert config.orca_enabled(cfg) is True
