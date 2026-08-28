"""The optional repowise plugin remains inert unless explicitly enabled and installed."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from beadhive import repowise_plugin
from beadhive.cli import app

runner = CliRunner()


def _option_help(flags):
    return "Options:\n" + "\n".join(f"  {flag:<30} supported" for flag in flags)


def test_disabled_by_default_and_binary_absence_is_inert(monkeypatch):
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: None)

    assert repowise_plugin.enabled({}, {}) is False
    assert repowise_plugin.enabled({"repowise": {"enabled": True}}, {}) is False
    assert "repowise" in repowise_plugin.config.KNOWN_SECTIONS


def test_capabilities_probe_init_help_not_version(monkeypatch):
    repowise_plugin.capabilities.cache_clear()
    calls = []
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: "/bin/repowise")
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs))
            or SimpleNamespace(
                returncode=0, stdout=_option_help(repowise_plugin._REQUIRED_INIT_FLAGS), stderr=""
            )
        ),
    )
    assert repowise_plugin.capabilities("init") == repowise_plugin._REQUIRED_INIT_FLAGS
    assert calls == [
        (
            ["repowise", "init", "--help"],
            {
                "check": False,
                "capture": True,
                "env": repowise_plugin._repowise_env(),
                "exact_env": True,
            },
        )
    ]


def test_capabilities_parse_exact_tokens_and_reject_failed_help(monkeypatch):
    repowise_plugin.capabilities.cache_clear()
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: "/bin/repowise")
    results = {
        "init": SimpleNamespace(
            returncode=0,
            stdout=_option_help({"--no-codex-config", "--mode-extra", "-y-extra"}),
            stderr="",
        ),
        "update": SimpleNamespace(returncode=2, stdout="options: --index-only", stderr="boom"),
    }
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda command, **kwargs: results[command[1]],
    )

    assert repowise_plugin.capabilities("init") == frozenset()
    assert repowise_plugin.capabilities("update") is None
    assert "update --help capability probe failed" in repowise_plugin.capability_error()


def test_capabilities_ignore_removed_flags_mentioned_only_in_help_prose(monkeypatch, tmp_path):
    repowise_plugin.capabilities.cache_clear()
    calls = []
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: "/bin/repowise")
    results = {
        "init": SimpleNamespace(
            returncode=0,
            stdout=_option_help(repowise_plugin._REQUIRED_INIT_FLAGS - {"--no-codex"})
            + "\nNotes:\n  --no-codex  removed; use editor defaults instead.\n",
            stderr="",
        ),
        "update": SimpleNamespace(
            returncode=0,
            stdout=_option_help(repowise_plugin._REQUIRED_UPDATE_FLAGS - {"--index-only"})
            + "\nDeprecated:\n  --index-only was removed.  Use the default update mode.\n",
            stderr="",
        ),
    }
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda command, **kwargs: calls.append(command) or results[command[1]],
    )

    assert "--no-codex" not in repowise_plugin.capabilities("init")
    assert "--index-only" not in repowise_plugin.capabilities("update")
    error = repowise_plugin.capability_error()
    assert "init missing --no-codex" in error
    assert "update missing --index-only" in error

    with pytest.raises(RuntimeError, match=r"init missing --no-codex"):
        repowise_plugin._seed_worktree({}, {}, main=tmp_path, branch="wt/x", target=tmp_path)

    (tmp_path / ".repowise").mkdir()
    (tmp_path / ".repowise" / "state.json").write_text(json.dumps({"last_sync_commit": "old"}))
    monkeypatch.setattr(repowise_plugin, "_branch_point", lambda main, start: "new")
    with pytest.raises(RuntimeError, match=r"update missing --index-only"):
        repowise_plugin._refresh_base(
            {}, {}, main=tmp_path, branch="wt/x", target=tmp_path, start_point="base"
        )

    assert calls == [["repowise", "init", "--help"], ["repowise", "update", "--help"]]


def test_capability_error_is_actionable_for_stock_cli(monkeypatch):
    repowise_plugin.capabilities.cache_clear()
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: "/bin/repowise")
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=0, stdout=_option_help({"--mode"}), stderr=""
        ),
    )
    error = repowise_plugin.capability_error()
    assert error is not None
    assert "missing" in error
    assert "--no-codex" in error
    assert "--no-mcp-json" in error
    assert "--no-workspace" in error


@pytest.mark.skipif(shutil.which("repowise") is None, reason="repowise not installed")
def test_repowise_guard_environment_executes_the_installed_binary():
    result = repowise_plugin.run.run(
        ["repowise", "--version"],
        check=False,
        capture=True,
        env=repowise_plugin._repowise_env(),
        exact_env=True,
    )

    assert result.returncode == 0, result.stderr
    assert "repowise" in result.stdout


def test_repowise_exact_environment_reaches_the_final_real_child_unchanged(monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", "/operator/workspace")
    intended = repowise_plugin._repowise_env()
    env_bin = shutil.which("env")
    assert env_bin is not None
    result = repowise_plugin.run.run(
        [env_bin],
        check=True,
        capture=True,
        env=intended,
        exact_env=True,
    )

    observed = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert observed == intended
    assert "GIT_WORKSPACE" not in observed


def test_repowise_guard_environment_preserves_path_but_scrubs_routing_and_secrets(monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"])
    monkeypatch.setenv("GIT_DIR", "/operator/repo.git")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/operator/repo.git/objects")
    monkeypatch.setenv("GIT_WORK_TREE", "/operator/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/operator/hooks")

    env = repowise_plugin._repowise_env()

    assert env["PATH"] == os.environ["PATH"]
    assert env["REPOWISE_SKIP_EDITOR_SETUP"] == "1"
    assert "GIT_DIR" not in env
    assert "GIT_OBJECT_DIRECTORY" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GITHUB_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env


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
        repowise_plugin, "capabilities", lambda command: repowise_plugin._REQUIRED_INIT_FLAGS
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
    assert kwargs["env"] == repowise_plugin._repowise_env()
    assert kwargs["exact_env"] is True


def test_workspace_index_uses_exact_host_flags_without_no_workspace(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(repowise_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(
        repowise_plugin, "capabilities", lambda command: repowise_plugin._REQUIRED_INIT_FLAGS
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
    assert kwargs["env"] == repowise_plugin._repowise_env()
    assert kwargs["exact_env"] is True


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
    monkeypatch.setattr(repowise_plugin, "capability_error", lambda command=None: None)
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
    assert calls[0][1]["env"] == repowise_plugin._repowise_env()
    assert calls[0][1]["exact_env"] is True
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
    monkeypatch.setattr(repowise_plugin, "capability_error", lambda command=None: None)
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
    monkeypatch.setattr(repowise_plugin, "capability_error", lambda command=None: None)
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
    assert kwargs["env"] == repowise_plugin._repowise_env()
    assert kwargs["exact_env"] is True
    assert (target / ".repowise-workspace").resolve() == overlay.resolve()
    manifest = YAML().load((target / ".repowise-workspace.yaml").read_text())
    assert manifest["repos"][0]["path"] == str(repo.resolve())


def test_seed_refuses_unsupported_cli_before_invocation(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        repowise_plugin,
        "capability_error",
        lambda command=None: "repowise is present but missing --no-codex",
    )
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="missing --no-codex"):
        repowise_plugin._seed_worktree({}, {}, main=tmp_path, branch="wt/x", target=tmp_path)

    assert calls == []


def test_seed_rejects_failed_help_probe_before_init_spawn(monkeypatch, tmp_path):
    calls = []
    repowise_plugin.capabilities.cache_clear()
    monkeypatch.setattr(repowise_plugin, "_has_cli", lambda: True)

    def failed_help(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=2, stdout="", stderr="broken help")

    monkeypatch.setattr(repowise_plugin.run, "run", failed_help)

    with pytest.raises(RuntimeError, match=r"init --help capability probe failed"):
        repowise_plugin._seed_worktree({}, {}, main=tmp_path, branch="wt/x", target=tmp_path)

    assert calls == [["repowise", "init", "--help"]]


def test_refresh_refuses_missing_update_flag_before_update_spawn(monkeypatch, tmp_path):
    calls = []
    (tmp_path / ".repowise").mkdir()
    (tmp_path / ".repowise" / "state.json").write_text(json.dumps({"last_sync_commit": "old"}))
    monkeypatch.setattr(repowise_plugin, "_branch_point", lambda main, start: "new")
    # This is a capability-contract unit test: binary presence must not depend on whether the
    # invoking user's optional Repowise install remains reachable through a hermetic HOME/PATH.
    monkeypatch.setattr(repowise_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(
        repowise_plugin,
        "capabilities",
        lambda command: (
            repowise_plugin._REQUIRED_INIT_FLAGS
            if command == "init"
            else repowise_plugin._REQUIRED_UPDATE_FLAGS - {"--index-only"}
        ),
    )
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match=r"update.*--index-only"):
        repowise_plugin._refresh_base(
            {}, {}, main=tmp_path, branch="wt/x", target=tmp_path, start_point="base"
        )

    assert calls == []


def test_seed_cleanly_skips_absent_host_overlay(monkeypatch, tmp_path):
    target = tmp_path / "worktree"
    target.mkdir()
    monkeypatch.setattr(repowise_plugin, "capability_error", lambda command=None: None)
    monkeypatch.setattr(repowise_plugin, "_workspace", lambda cfg: tmp_path / "missing")
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0),
    )

    repowise_plugin._seed_worktree({}, {}, main=tmp_path, branch="wt/x", target=target)

    assert not (target / ".repowise-workspace.yaml").exists()
