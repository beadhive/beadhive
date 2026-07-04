"""Tests for `ws mcp install` — the claude mcp add convenience verb.

Covers:
  * _build_claude_mcp_add_cmd pure function: verifies the exact argv list for default
    and custom scopes (no subprocess, no I/O).
  * mcp_install command (via CliRunner):
    - absent `claude` binary: exits 1 + prints friendly error + manual one-liner.
    - present `claude` binary, subprocess succeeds: exits 0, prints success message.
    - present `claude` binary, subprocess fails (non-zero): exits with that code.
    - custom --scope flag flows through to the argv.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from ws.cli import MCP_DEFAULT_SCOPE, MCP_SERVER_NAME, _build_claude_mcp_add_cmd, app

# ---- _build_claude_mcp_add_cmd -----------------------------------------------


def test_build_cmd_default_scope():
    cmd = _build_claude_mcp_add_cmd()
    expected = [
        "claude", "mcp", "add", MCP_SERVER_NAME,
        "--scope", MCP_DEFAULT_SCOPE,
        "--", "ws", "mcp", "serve",
    ]
    assert cmd == expected


def test_build_cmd_user_scope_is_default():
    assert _build_claude_mcp_add_cmd() == _build_claude_mcp_add_cmd("user")


def test_build_cmd_local_scope():
    cmd = _build_claude_mcp_add_cmd("local")
    assert cmd[5] == "local"
    assert "--scope" in cmd
    assert "ws" in cmd
    assert "mcp" in cmd
    assert "serve" in cmd


def test_build_cmd_server_name_constant():
    cmd = _build_claude_mcp_add_cmd()
    assert MCP_SERVER_NAME in cmd
    assert cmd[3] == MCP_SERVER_NAME


# ---- mcp_install (CLI) -------------------------------------------------------


runner = CliRunner()


def test_install_absent_claude_exits_1(monkeypatch):
    monkeypatch.setattr("ws.cli.shutil.which", lambda _bin: None)

    result = runner.invoke(app, ["mcp", "install"])

    assert result.exit_code == 1
    assert "claude" in result.output.lower()
    assert "not found" in result.output.lower() or "install" in result.output.lower()


def test_install_absent_claude_prints_manual_command(monkeypatch):
    monkeypatch.setattr("ws.cli.shutil.which", lambda _bin: None)

    result = runner.invoke(app, ["mcp", "install"])

    # The manual fallback one-liner must appear in the error output
    assert "ws mcp serve" in result.output
    assert "claude mcp add" in result.output


def test_install_success(monkeypatch):
    monkeypatch.setattr("ws.cli.shutil.which", lambda _bin: "/usr/local/bin/claude")
    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        result = runner.invoke(app, ["mcp", "install"])

    assert result.exit_code == 0
    assert "registered" in result.output.lower()
    mock_run.assert_called_once()


def test_install_success_passes_correct_cmd(monkeypatch):
    monkeypatch.setattr("ws.cli.shutil.which", lambda _bin: "/usr/local/bin/claude")
    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        runner.invoke(app, ["mcp", "install"])

    called_cmd = mock_run.call_args[0][0]
    assert called_cmd == _build_claude_mcp_add_cmd("user")


def test_install_custom_scope(monkeypatch):
    monkeypatch.setattr("ws.cli.shutil.which", lambda _bin: "/usr/local/bin/claude")
    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        result = runner.invoke(app, ["mcp", "install", "--scope", "local"])

    assert result.exit_code == 0
    called_cmd = mock_run.call_args[0][0]
    assert called_cmd == _build_claude_mcp_add_cmd("local")
    assert "local" in result.output


def test_install_subprocess_failure(monkeypatch):
    monkeypatch.setattr("ws.cli.shutil.which", lambda _bin: "/usr/local/bin/claude")
    fake_proc = MagicMock()
    fake_proc.returncode = 2

    with patch("subprocess.run", return_value=fake_proc):
        result = runner.invoke(app, ["mcp", "install"])

    assert result.exit_code == 2
    assert "exited 2" in result.output or "exit" in result.output.lower()
