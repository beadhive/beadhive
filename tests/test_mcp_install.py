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

from beadhive.cli import MCP_DEFAULT_SCOPE, MCP_SERVER_NAME, _build_claude_mcp_add_cmd, app

# ---- _build_claude_mcp_add_cmd -----------------------------------------------


def test_build_cmd_default_scope():
    cmd = _build_claude_mcp_add_cmd()
    expected = [
        "claude",
        "mcp",
        "add",
        MCP_SERVER_NAME,
        "--scope",
        MCP_DEFAULT_SCOPE,
        "--",
        "bh",
        "mcp",
        "serve",
    ]
    assert cmd == expected


def test_build_cmd_user_scope_is_default():
    assert _build_claude_mcp_add_cmd() == _build_claude_mcp_add_cmd("user")


def test_build_cmd_local_scope():
    cmd = _build_claude_mcp_add_cmd("local")
    assert cmd[5] == "local"
    assert "--scope" in cmd
    assert "bh" in cmd
    assert "mcp" in cmd
    assert "serve" in cmd


def test_build_cmd_server_name_constant():
    cmd = _build_claude_mcp_add_cmd()
    assert MCP_SERVER_NAME in cmd
    assert cmd[3] == MCP_SERVER_NAME


# ---- mcp_install (CLI) -------------------------------------------------------


runner = CliRunner()


def _claude_add_calls(mock_run) -> list[list[str]]:
    """Every `claude mcp add ...` argv recorded on a GLOBAL `subprocess.run` mock.

    These tests patch `subprocess.run` process-wide, so `mock_run.call_args` (the LAST call)
    and `assert_called_once()` (the TOTAL count) both silently depend on whether anything
    else in the process shelled out. Under randomized ordering that is not this test's to
    control — bh-712wt, where leaked config made `mcp install` enumerate hives and run `git
    log` against a sibling test's workspace. Filtering to the call under test removes the
    coupling without weakening the assertion."""
    return [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["claude", "mcp", "add"]
    ]


def test_install_absent_claude_exits_1(monkeypatch):
    monkeypatch.setattr("beadhive.cli.shutil.which", lambda _bin: None)

    result = runner.invoke(app, ["mcp", "install"])

    assert result.exit_code == 1
    assert "claude" in result.output.lower()
    assert "not found" in result.output.lower() or "install" in result.output.lower()


def test_install_absent_claude_prints_manual_command(monkeypatch):
    monkeypatch.setattr("beadhive.cli.shutil.which", lambda _bin: None)

    result = runner.invoke(app, ["mcp", "install"])

    # The manual fallback one-liner must appear in the error output
    assert "bh mcp serve" in result.output
    assert "claude mcp add" in result.output


def test_install_success(monkeypatch):
    monkeypatch.setattr("beadhive.cli.shutil.which", lambda _bin: "/usr/local/bin/claude")
    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        result = runner.invoke(app, ["mcp", "install"])

    assert result.exit_code == 0
    assert "registered" in result.output.lower()

    # Assert on the call THIS test causes, not on the process-wide subprocess count
    # (bh-712wt). `patch("subprocess.run")` is global, so `assert_called_once()` was really
    # asserting "nothing else in this process shelled out" — which depends on ambient state
    # the test does not control. It failed once in five full-suite runs, with a `git log`
    # against a SIBLING test's workspace path recorded on the mock: leaked config made
    # `mcp install` enumerate hives and shell out to git. That leak is a real and separate
    # problem, but it is not this test's subject, and gating the MERGE on it meant a red run
    # stopped carrying information.
    add_cmd = _build_claude_mcp_add_cmd()
    matching = [c for c in mock_run.call_args_list if c.args and c.args[0] == add_cmd]
    assert len(matching) == 1, (
        f"expected exactly one `claude mcp add` call, got {len(matching)} — "
        f"all recorded calls: {mock_run.call_args_list}"
    )


def test_install_success_passes_correct_cmd(monkeypatch):
    monkeypatch.setattr("beadhive.cli.shutil.which", lambda _bin: "/usr/local/bin/claude")
    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        runner.invoke(app, ["mcp", "install"])

    assert _claude_add_calls(mock_run) == [_build_claude_mcp_add_cmd("user")]


def test_install_custom_scope(monkeypatch):
    monkeypatch.setattr("beadhive.cli.shutil.which", lambda _bin: "/usr/local/bin/claude")
    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        result = runner.invoke(app, ["mcp", "install", "--scope", "local"])

    assert result.exit_code == 0
    assert _claude_add_calls(mock_run) == [_build_claude_mcp_add_cmd("local")]
    assert "local" in result.output


def test_install_subprocess_failure(monkeypatch):
    monkeypatch.setattr("beadhive.cli.shutil.which", lambda _bin: "/usr/local/bin/claude")
    fake_proc = MagicMock()
    fake_proc.returncode = 2

    with patch("subprocess.run", return_value=fake_proc):
        result = runner.invoke(app, ["mcp", "install"])

    assert result.exit_code == 2
    assert "exited 2" in result.output or "exit" in result.output.lower()
