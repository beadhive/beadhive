"""gitworkspace_plugin.py — promotes git-workspace to a bh Plugin (bh-4y0r.4).

Mirrors test_orca.py / test_plugin_cli.py's style: hermetic $GIT_WORKSPACE fixtures + the
in-process Typer CliRunner (not the installed bh binary).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import gitworkspace, gitworkspace_plugin, hive_ready, identity, orca, plugins
from beadhive.cli import app

runner = CliRunner()


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    """Fresh-install baseline for `_readiness` (bh-cgcg.2), independent of the real machine's
    ambient `~/workspace` — mirrors test_workspace_root.py's `_isolated` fixture: no
    `$GIT_WORKSPACE`, and the legacy `~/workspace` stand-in monkeypatched to an as-yet
    nonexistent tmp dir so the legacy-populated guard can never accidentally fire against real
    content on the machine running the suite (it does on a dev box with a populated
    `~/workspace`, exactly the ambient state this fixture must not depend on). `BH_HOME` is
    already isolated per-test by the autouse `_sandbox_bh_home` fixture in conftest.py."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    legacy = tmp_path / "home-workspace"
    monkeypatch.setattr(identity, "_legacy_root", lambda: legacy)
    return legacy


# ---- plugins.registry() -------------------------------------------------------


def test_registry_includes_git_workspace_then_orca():
    reg = plugins.registry()
    names = [p.name for p in reg]
    assert names == ["git-workspace", "orca", "observaloop"]


def test_plugin_is_gated_on_gitworkspace_enabled():
    assert gitworkspace_plugin.PLUGIN.enabled({"git_workspace": {"enabled": False}}, None) is False
    assert gitworkspace_plugin.PLUGIN.enabled({"git_workspace": {"enabled": True}}, None) is True


# ---- readiness -----------------------------------------------------------------
#
# Internal mode (the default, bh-cgcg.2) owns its root — GIT_WORKSPACE-unset is no longer a
# warning there, since there's nothing for the user to set; the check instead is whether the
# managed root has been created + seeded. External mode (explicit config, or the
# legacy-populated guard) keeps the original env-var-aware warning.


def test_readiness_internal_mode_missing_when_root_unseeded(_isolated):
    """Fresh install (internal mode, the default): the managed root doesn't exist/isn't
    seeded yet — reported as 'missing' with a pointer at `bh doctor`, NOT the old (now-wrong
    in this mode) GIT_WORKSPACE warning."""
    state, detail = gitworkspace_plugin._readiness({}, None)
    assert state == "missing"
    assert "GIT_WORKSPACE" not in detail
    assert "bh doctor" in detail


def test_readiness_internal_mode_ok_once_seeded_and_locked(_isolated):
    root = Path(identity.workspace_root())
    gitworkspace.ensure_seeded(root)
    (root / "workspace-lock.toml").write_text("")
    state, detail = gitworkspace_plugin._readiness({}, None)
    assert state == "ok"


def test_readiness_external_mode_warns_when_git_workspace_env_unset(_isolated):
    """External mode via the legacy-populated guard (bh-cgcg's factory-orca case:
    GIT_WORKSPACE unset but an existing populated ~/workspace) keeps the original
    env-var-aware warning — that root really is something the user could point elsewhere."""
    (_isolated / "github" / "acme" / "api" / ".git").mkdir(parents=True)
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


def test_hive_ready_plugin_checks_includes_git_workspace_line(monkeypatch):
    entry = {"provider": "github", "org": "acme", "repo": "api", "prefix": "a-api"}
    monkeypatch.setattr(gitworkspace, "enabled", lambda cfg: False)
    checks = hive_ready._plugin_checks({}, entry)
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
