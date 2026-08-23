"""The optional repowise plugin remains inert unless explicitly enabled and installed."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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
            stdout="init options: --no-mcp-json --no-vscode", stderr=""
        ),
    )
    assert repowise_plugin.capabilities() == frozenset({"--no-mcp-json", "--no-vscode"})


def test_capability_error_is_actionable_for_stock_cli(monkeypatch):
    repowise_plugin.capabilities.cache_clear()
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: "/bin/repowise")
    monkeypatch.setattr(
        repowise_plugin.run,
        "run",
        lambda command, **kwargs: SimpleNamespace(stdout="init options: --mode", stderr=""),
    )
    assert "missing --no-mcp-json, --no-vscode" in repowise_plugin.capability_error()


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
        repowise_plugin, "capabilities", lambda: frozenset({"--no-mcp-json", "--no-vscode"})
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
    assert kwargs["env"] == {"REPOWISE_SKIP_EDITOR_SETUP": "1"}


def test_workspace_index_uses_exact_host_flags_without_no_workspace(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(repowise_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(
        repowise_plugin, "capabilities", lambda: frozenset({"--no-mcp-json", "--no-vscode"})
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
