"""Best-effort fences for the optional herdr plugin."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from typer.testing import CliRunner

from beadhive import herdr_plugin, plugins
from beadhive.cli import app

runner = CliRunner()


def _result(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_import_is_safe_without_herdr_on_path(monkeypatch):
    """Mirror orca's optional-tool fence: import never probes or requires the binary."""
    monkeypatch.setenv("PATH", "")
    importlib.reload(herdr_plugin)


def test_registry_includes_herdr():
    assert "herdr" in [plugin.name for plugin in plugins.registry()]


def test_enabled_requires_binary_and_live_server(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", lambda *a, **k: _result())
    assert herdr_plugin.PLUGIN.enabled({}, None) is True

    monkeypatch.setattr(herdr_plugin.run, "run", lambda *a, **k: _result(1))
    assert herdr_plugin.PLUGIN.enabled({}, None) is False


def test_enabled_is_false_when_binary_missing_without_subprocess(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)

    def boom(*args, **kwargs):
        raise AssertionError("missing herdr must not spawn a subprocess")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    assert herdr_plugin.PLUGIN.enabled({}, None) is False


def test_status_reports_server_and_integrations(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "status":
            return _result(stdout="server ready")
        return _result(stdout="claude: installed\ncodex: absent")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "status"])

    assert result.exit_code == 0, result.output
    assert "herdr: server=up" in result.output
    assert "claude: installed" in result.output
    assert calls == [["herdr", "status"], ["herdr", "integration", "status"]]


def test_status_fences_missing_binary_and_stopped_server(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)
    result = runner.invoke(app, ["plugin", "herdr", "status"])
    assert result.exit_code == 0, result.output
    assert "server=down" in result.output

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", lambda *a, **k: _result(1, stderr="stopped"))
    result = runner.invoke(app, ["plugin", "herdr", "status"])
    assert result.exit_code == 0, result.output
    assert "server=down" in result.output
    assert "integrations: unavailable" in result.output


def test_integrate_installs_requested_supported_kind_idempotently(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "agent", "start", "--help"]:
            return _result(stdout="--kind <KIND>  [possible values: claude, codex]")
        return _result(stdout="already installed")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    first = runner.invoke(app, ["plugin", "herdr", "integrate", "claude"])
    second = runner.invoke(app, ["plugin", "herdr", "integrate", "claude"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "integration installed for claude" in second.output
    assert calls == [
        ["herdr", "agent", "start", "--help"],
        ["herdr", "integration", "install", "claude"],
        ["herdr", "agent", "start", "--help"],
        ["herdr", "integration", "install", "claude"],
    ]


def test_integrate_rejects_unsupported_kind_with_discovered_list(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _result(stdout="Supported agent kinds: claude, codex")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "integrate", "gemini"])

    assert result.exit_code == 2
    assert "unsupported agent kind 'gemini'" in result.output
    assert "supported kinds: claude, codex" in result.output
    assert calls == [["herdr", "agent", "start", "--help"]]


def test_integrate_fences_missing_binary_without_subprocess(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)

    def boom(*args, **kwargs):
        raise AssertionError("missing herdr must not spawn a subprocess")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    result = runner.invoke(app, ["plugin", "herdr", "integrate", "claude"])

    assert result.exit_code == 1
    assert "herdr CLI not on PATH" in result.output
