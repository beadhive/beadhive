"""The optional repowise plugin remains inert unless explicitly enabled and installed."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import repowise_plugin
from beadhive.cli import app

runner = CliRunner()


def test_disabled_by_default_and_binary_absence_is_inert(monkeypatch):
    monkeypatch.setattr(repowise_plugin.shutil, "which", lambda name: None)

    assert repowise_plugin.enabled({}, {}) is False
    assert repowise_plugin.enabled({"repowise": {"enabled": True}}, {}) is False
    assert "repowise" in repowise_plugin.config.KNOWN_SECTIONS


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
