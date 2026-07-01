"""Tests for the plugin-mode claude installer (_install_plugin_claude, _do_claude branching,
_do_skills guard, CLI --claude --skills conflict)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from ws import rig

# ---- _install_plugin_claude: subprocess calls ----


def test_install_plugin_claude_runs_two_claude_cmds(capsys):
    """Plugin install runs marketplace add then plugin install."""
    cfg = {"claude": {"marketplace": ".", "plugin": "agf", "scope": "user"}}
    with patch("ws.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)

    assert mock_run.call_count == 2
    first_cmd = mock_run.call_args_list[0][0][0]
    second_cmd = mock_run.call_args_list[1][0][0]
    assert first_cmd == ["claude", "plugin", "marketplace", "add", "."]
    assert second_cmd == ["claude", "plugin", "install", "agf@.", "--scope", "user"]


def test_install_plugin_claude_uses_configured_plugin_and_scope(capsys):
    mp = "https://example.com"
    cfg = {"claude": {"marketplace": mp, "plugin": "myagf", "scope": "project"}}
    with patch("ws.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)

    cmds = [c[0][0] for c in mock_run.call_args_list]
    assert cmds[0] == ["claude", "plugin", "marketplace", "add", mp]
    assert cmds[1] == ["claude", "plugin", "install", f"myagf@{mp}", "--scope", "project"]


def test_install_plugin_claude_idempotent(capsys):
    """Running twice calls the same two commands (idempotent from the marketplace side)."""
    cfg = {"claude": {"marketplace": ".", "plugin": "agf", "scope": "user"}}
    with patch("ws.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)
        rig._install_plugin_claude(cfg)

    assert mock_run.call_count == 4  # two commands × two calls


# ---- _install_plugin_claude writes NOTHING to disk ----


def test_install_plugin_claude_writes_no_agent_files(tmp_path):
    cfg = {"claude": {"marketplace": ".", "plugin": "agf", "scope": "user"}}
    with patch("ws.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)

    # No .claude/agents/ directory should have been created
    assert not (tmp_path / ".claude" / "agents").exists()


# ---- _do_claude branching via onboard._do_claude ----


def test_do_claude_plugin_mode_calls_plugin_installer(tmp_path):
    """In plugin mode, _do_claude must call _install_plugin_claude (not _install_agents_claude)."""
    from ws import onboard

    class FakeCtx:
        base = tmp_path
        cfg = {"claude": {"source": "plugin"}}
        force = False
        provider = "github"
        org = "acme"
        repo = "api"

    ctx = FakeCtx()
    with (
        patch("ws.rig._install_claude_settings"),
        patch("ws.rig._install_plugin_claude") as mock_plugin,
        patch("ws.rig._install_agents_claude") as mock_agents,
        patch("ws.rig._install_sandbox_grant"),
        patch("ws.rig._ensure_agf_hint"),
    ):
        onboard._do_claude(ctx)

    mock_plugin.assert_called_once()
    mock_agents.assert_not_called()


def test_do_claude_copy_mode_calls_agents_installer(tmp_path):
    """In copy mode, _do_claude must call _install_agents_claude (not _install_plugin_claude)."""
    from ws import onboard

    class FakeCtx:
        base = tmp_path
        cfg = {"claude": {"source": "copy"}}
        force = False
        provider = "github"
        org = "acme"
        repo = "api"

    ctx = FakeCtx()
    with (
        patch("ws.rig._install_claude_settings"),
        patch("ws.rig._install_plugin_claude") as mock_plugin,
        patch("ws.rig._install_agents_claude") as mock_agents,
        patch("ws.rig._install_sandbox_grant"),
        patch("ws.rig._ensure_agf_hint"),
    ):
        onboard._do_claude(ctx)

    mock_agents.assert_called_once()
    mock_plugin.assert_not_called()


# ---- _do_skills guard: skip local copy in plugin mode ----


def test_do_skills_skipped_in_plugin_mode_with_claude():
    """--skills is a no-op (with warning) when source==plugin and ctx.claude==True."""
    from ws import onboard

    class FakeCtx:
        base = None  # type: ignore
        cfg = {"claude": {"source": "plugin"}}
        force = False
        claude = True

    ctx = FakeCtx()
    with (
        patch("ws.rig._install_skills") as mock_skills,
        patch("ws.rig._link_skills_claude") as mock_link,
    ):
        onboard._do_skills(ctx)

    mock_skills.assert_not_called()
    mock_link.assert_not_called()


def test_do_skills_runs_in_copy_mode():
    """In copy mode, _do_skills writes skills + link normally."""
    from ws import onboard

    class FakeCtx:
        base = None  # type: ignore
        cfg = {"claude": {"source": "copy"}}
        force = False
        claude = True

    ctx = FakeCtx()
    with (
        patch("ws.rig._install_skills") as mock_skills,
        patch("ws.rig._link_skills_claude") as mock_link,
    ):
        onboard._do_skills(ctx)

    mock_skills.assert_called_once()
    mock_link.assert_called_once()


def test_do_skills_runs_without_claude_flag():
    """--skills alone (no --claude) always runs regardless of source."""
    from ws import onboard

    class FakeCtx:
        base = None  # type: ignore
        cfg = {"claude": {"source": "plugin"}}
        force = False
        claude = False  # --skills without --claude

    ctx = FakeCtx()
    with (
        patch("ws.rig._install_skills") as mock_skills,
        patch("ws.rig._link_skills_claude") as mock_link,
    ):
        onboard._do_skills(ctx)

    mock_skills.assert_called_once()
    mock_link.assert_not_called()  # no --claude means no symlink


# ---- CLI flag conflict: --claude --skills in plugin mode ----


def test_cli_rig_init_rejects_claude_and_skills_in_plugin_mode():
    """ws rig init --claude --skills exits non-zero with a clear error in plugin mode."""
    from typer.testing import CliRunner

    from ws.cli import app

    cli_runner = CliRunner()
    with (
        patch("ws.config.load", return_value={"claude": {"source": "plugin"}}),
        patch("ws.config.claude_source", return_value="plugin"),
    ):
        result = cli_runner.invoke(app, ["rig", "init", "--claude", "--skills"])

    assert result.exit_code != 0
    combined = result.output.lower()
    assert "plugin" in combined or "skills" in combined
