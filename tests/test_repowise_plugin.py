"""The optional repowise plugin remains inert unless explicitly enabled and installed."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from beadhive import repowise_plugin
from beadhive.cli import app

runner = CliRunner()


def test_disabled_by_default_and_binary_absence_is_inert(monkeypatch):
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: None)

    assert repowise_plugin.enabled({}, {}) is False
    assert repowise_plugin.enabled({"repowise": {"enabled": True}}, {}) is False
    assert "repowise" in repowise_plugin.config.KNOWN_SECTIONS


def test_capabilities_probe_init_help_not_version(monkeypatch):
    repowise_plugin.capabilities.cache_clear()
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: "/bin/repowise")
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            stdout=(
                "init options: --mode --no-prose --no-claude-md --no-codex "
                "--no-mcp-json --no-vscode --no-workspace --all --yes"
            ),
            stderr="",
        ),
    )
    assert repowise_plugin.capabilities() == repowise_plugin._REQUIRED_INIT_FLAGS


def test_capability_error_is_actionable_for_stock_cli(monkeypatch):
    repowise_plugin.capabilities.cache_clear()
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: "/bin/repowise")
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda command, **kwargs: SimpleNamespace(stdout="init options: --mode", stderr=""),
    )
    error = repowise_plugin.capability_error()
    assert error is not None
    assert "missing" in error
    assert "--no-codex" in error
    assert "--no-mcp-json" in error
    assert "--no-workspace" in error


def test_plugin_cli_help_is_registered(world):
    result = runner.invoke(app, ["plugin", "repowise", "--help"])

    assert result.exit_code == 0, result.output
    assert "index" in result.output
    assert "status" in result.output


def test_readiness_reports_index_drift_and_size(tmp_path, monkeypatch):
    clone = tmp_path / "github" / "acme" / "widget"
    state = clone / ".repowise" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"last_sync_commit": "abc"}))
    (state.parent / "graph.bin").write_bytes(b"0123456789")
    entry = {"provider": "github", "org": "acme", "repo": "widget"}
    monkeypatch.setattr(repowise_plugin.registry, "hive_dir", lambda value: clone)
    monkeypatch.setattr(repowise_plugin.run, "out", lambda command: "3\n")

    state, detail = repowise_plugin.readiness({}, entry)

    assert state == "warn"
    assert "3 commits behind" in detail
    assert "MiB" in detail


def test_readiness_returns_none_without_clone_path():
    assert repowise_plugin.readiness({}, {}) is None


def test_single_repo_index_uses_no_workspace_and_skips_editor_setup(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(repowise_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(
        repowise_plugin, "capabilities", lambda: repowise_plugin._REQUIRED_INIT_FLAGS
    )
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0),
    )

    assert repowise_plugin._index(tmp_path, workspace=False) == 0

    argv, kwargs = calls[0]
    assert "--no-workspace" in argv
    assert "--all" not in argv
    assert "--no-mcp-json" in argv
    assert "--no-vscode" in argv
    assert kwargs["env"] == {"REPOWISE_SKIP_EDITOR_SETUP": "1"}


def test_workspace_index_uses_exact_host_flags_without_no_workspace(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(repowise_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(
        repowise_plugin, "capabilities", lambda: repowise_plugin._REQUIRED_INIT_FLAGS
    )
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0),
    )

    assert repowise_plugin._index(tmp_path, workspace=True) == 0

    argv, kwargs = calls[0]
    assert argv == [
        "repowise",
        "init",
        str(tmp_path),
        "--mode",
        "fast",
        "--no-prose",
        "--no-claude-md",
        "--no-codex",
        "--no-mcp-json",
        "--no-vscode",
        "--all",
        "-y",
    ]
    assert "--no-workspace" not in argv
    assert kwargs["env"] == {"REPOWISE_SKIP_EDITOR_SETUP": "1"}


def test_onboard_indexes_base_checkout_and_missing_binary_skips(monkeypatch, tmp_path):
    ctx = SimpleNamespace(base=tmp_path)
    monkeypatch.setattr(repowise_plugin, "_has_cli", lambda: True)
    calls = []
    monkeypatch.setattr(
        repowise_plugin,
        "_index",
        lambda path, *, workspace: calls.append((path, workspace)) or 0,
    )

    repowise_plugin._on_onboard(ctx)

    assert calls == [(Path(tmp_path), False)]


def test_workspace_root_uses_selected_gitworkspace_config(monkeypatch, tmp_path):
    config_path = tmp_path / "workspace.toml"
    monkeypatch.setattr(repowise_plugin.gitworkspace, "config_paths", lambda cfg: [config_path])

    assert repowise_plugin._workspace({}) == tmp_path


def test_refresh_base_runs_before_create_only_when_stale(monkeypatch, tmp_path):
    calls = []
    (tmp_path / ".repowise").mkdir()
    (tmp_path / ".repowise" / "state.json").write_text(json.dumps({"last_sync_commit": "old"}))
    config_path = tmp_path / ".repowise" / "config.yaml"
    config_path.write_text("editor_files:\n  claude_md: false\n")
    monkeypatch.setattr(repowise_plugin, "capability_error", lambda: None)
    monkeypatch.setattr(repowise_plugin, "_branch_point", lambda main, start: "new")
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0),
    )

    repowise_plugin._refresh_base(
        {}, {}, main=tmp_path, branch="wt/x", target=tmp_path / "wt", start_point="base"
    )

    assert calls[0][0] == [
        "repowise",
        "update",
        str(tmp_path),
        "--index-only",
        "--no-workspace",
    ]
    assert calls[0][1]["env"] == {"REPOWISE_SKIP_EDITOR_SETUP": "1"}
    assert YAML().load(config_path.read_text())["editor_files"]["vscode"] is False


def test_backfill_vscode_config_preserves_existing_editor_settings(tmp_path):
    config_path = tmp_path / ".repowise" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("editor_files:\n  claude_md: false\n")

    repowise_plugin._backfill_vscode_config(tmp_path, workspace=False)

    config = YAML().load(config_path.read_text())
    assert config["editor_files"] == {"claude_md": False, "vscode": False}


def test_workspace_index_backfills_each_existing_base_config(monkeypatch, tmp_path):
    configs = [
        tmp_path / "github" / "acme" / "one" / ".repowise" / "config.yaml",
        tmp_path / "gitlab" / "acme" / "two" / ".repowise" / "config.yaml",
    ]
    for config_path in configs:
        config_path.parent.mkdir(parents=True)
        config_path.write_text("editor_files:\n  claude_md: false\n")
    monkeypatch.setattr(repowise_plugin, "capability_error", lambda: None)
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0),
    )

    assert repowise_plugin._index(tmp_path, workspace=True) == 0

    for config_path in configs:
        config = YAML().load(config_path.read_text())
        assert config["editor_files"]["vscode"] is False


def test_refresh_base_skips_missing_and_current_indexes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(repowise_plugin.run, "run", lambda *args, **kwargs: calls.append(args))
    kwargs = dict(main=tmp_path, branch="wt/x", target=tmp_path / "wt", start_point="")
    repowise_plugin._refresh_base({}, {}, **kwargs)

    (tmp_path / ".repowise").mkdir()
    (tmp_path / ".repowise" / "state.json").write_text(json.dumps({"last_sync_commit": "same"}))
    monkeypatch.setattr(repowise_plugin, "_branch_point", lambda main, start: "same")
    repowise_plugin._refresh_base({}, {}, **kwargs)
    assert calls == []


def test_seed_uses_auto_detection_and_installs_overlay(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    target = tmp_path / "worktree"
    repo = workspace / "github" / "acme" / "widget"
    overlay = workspace / ".repowise-workspace"
    target.mkdir()
    repo.mkdir(parents=True)
    overlay.mkdir()
    (overlay / "system_graph.json").write_text("{}")
    (workspace / ".repowise-workspace.yaml").write_text(
        "version: 1\nrepos:\n- path: github/acme/widget\n  alias: widget\n"
    )
    calls = []
    monkeypatch.setattr(repowise_plugin, "capability_error", lambda: None)
    monkeypatch.setattr(repowise_plugin, "_workspace", lambda cfg: workspace)
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0),
    )

    repowise_plugin._seed_worktree({}, {}, main=repo, branch="wt/x", target=target)

    argv, kwargs = calls[0]
    assert argv == ["repowise", "init", *repowise_plugin._BASE_ARGS, "-y"]
    assert "--seed-from" not in argv
    assert str(target) not in argv
    assert kwargs["cwd"] == target
    assert kwargs["env"] == {"REPOWISE_SKIP_EDITOR_SETUP": "1"}
    assert (target / ".repowise-workspace").resolve() == overlay.resolve()
    manifest = YAML().load((target / ".repowise-workspace.yaml").read_text())
    assert manifest["repos"][0]["path"] == str(repo.resolve())


def test_seed_refuses_unsupported_cli_before_invocation(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        repowise_plugin,
        "capability_error",
        lambda: "repowise is present but missing --no-codex",
    )
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="missing --no-codex"):
        repowise_plugin._seed_worktree({}, {}, main=tmp_path, branch="wt/x", target=tmp_path)

    assert calls == []


def test_seed_cleanly_skips_absent_host_overlay(monkeypatch, tmp_path):
    target = tmp_path / "worktree"
    target.mkdir()
    monkeypatch.setattr(repowise_plugin, "capability_error", lambda: None)
    monkeypatch.setattr(repowise_plugin, "_workspace", lambda cfg: tmp_path / "missing")
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0),
    )

    repowise_plugin._seed_worktree({}, {}, main=tmp_path, branch="wt/x", target=target)

    assert not (target / ".repowise-workspace.yaml").exists()
