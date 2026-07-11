"""Tests for the plugin-mode claude installer (_install_plugin_claude, _do_claude branching,
_do_skills guard, CLI --claude --skills conflict)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from beadhive import rig

# Captured before the autouse fixture below patches it, so the real reader stays testable.
_real_known_marketplace_path = rig._known_marketplace_path


@pytest.fixture(autouse=True)
def _no_known_marketplaces(monkeypatch):
    """Hermetic: never read this machine's real ~/.claude/plugins/known_marketplaces.json.
    Guard tests override this with a conflicting path."""
    monkeypatch.setattr(rig, "_known_marketplace_path", lambda name: "")


# ---- _install_plugin_claude: subprocess calls ----


def test_install_plugin_claude_runs_two_claude_cmds(capsys, tmp_path):
    """Plugin install runs marketplace add (absolute path) then plugin install
    addressed by the marketplace *name* from the local manifest — the Claude CLI
    rejects bare '.' and resolves install refs by name, not path."""
    mp_dir = tmp_path / "mp"
    (mp_dir / ".claude-plugin").mkdir(parents=True)
    (mp_dir / ".claude-plugin" / "marketplace.json").write_text('{"name": "testmp"}')
    cfg = {"claude": {"marketplace": str(mp_dir), "plugin": "bh", "scope": "user"}}
    with patch("beadhive.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)

    assert mock_run.call_count == 2
    first_cmd = mock_run.call_args_list[0][0][0]
    second_cmd = mock_run.call_args_list[1][0][0]
    assert first_cmd == ["claude", "plugin", "marketplace", "add", str(mp_dir.resolve())]
    assert second_cmd == ["claude", "plugin", "install", "bh@testmp", "--scope", "user"]


def test_install_plugin_claude_default_marketplace_is_absolute(capsys):
    """Regression: with the default marketplace ('.'), the shelled-out add must get an
    absolute path (cwd-independent) and install must use the manifest name."""
    with patch("beadhive.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude({})

    added = mock_run.call_args_list[0][0][0][-1]
    assert Path(added).is_absolute()
    installed = mock_run.call_args_list[1][0][0][3]
    assert not installed.endswith("@.")


def test_install_plugin_claude_uses_configured_plugin_and_scope(capsys):
    mp = "https://example.com"
    cfg = {"claude": {"marketplace": mp, "plugin": "custom", "scope": "project"}}
    with patch("beadhive.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)

    cmds = [c[0][0] for c in mock_run.call_args_list]
    assert cmds[0] == ["claude", "plugin", "marketplace", "add", mp]
    assert cmds[1] == ["claude", "plugin", "install", f"custom@{mp}", "--scope", "project"]


def test_install_plugin_claude_idempotent(capsys):
    """Running twice calls the same two commands (idempotent from the marketplace side)."""
    cfg = {"claude": {"marketplace": ".", "plugin": "bh", "scope": "user"}}
    with patch("beadhive.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)
        rig._install_plugin_claude(cfg)

    assert mock_run.call_count == 4  # two commands × two calls


# ---- re-point guard ----


def _mk_marketplace(tmp_path):
    mp_dir = tmp_path / "mp"
    (mp_dir / ".claude-plugin").mkdir(parents=True)
    (mp_dir / ".claude-plugin" / "marketplace.json").write_text('{"name": "testmp"}')
    return mp_dir


def test_install_plugin_claude_refuses_repoint_of_existing_marketplace(
    tmp_path, monkeypatch, capsys
):
    """Regression: `claude plugin marketplace add` is last-writer-wins
    by manifest name — when the name is already registered at a DIFFERENT path, refuse the
    add (no silent hijack), warn, and still install from the existing registration."""
    mp_dir = _mk_marketplace(tmp_path)
    monkeypatch.setattr(
        rig, "_known_marketplace_path", lambda name: str(tmp_path / "elsewhere")
    )
    cfg = {"claude": {"marketplace": str(mp_dir), "plugin": "bh", "scope": "user"}}
    with patch("beadhive.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)

    cmds = [c[0][0] for c in mock_run.call_args_list]
    assert cmds == [["claude", "plugin", "install", "bh@testmp", "--scope", "user"]]
    err = capsys.readouterr().err
    assert "refusing to re-point" in err
    assert "testmp" in err


def test_install_plugin_claude_readds_when_existing_registration_matches(tmp_path, monkeypatch):
    """Same name already registered at the SAME path is the idempotent re-add: no warning,
    the add proceeds."""
    mp_dir = _mk_marketplace(tmp_path)
    monkeypatch.setattr(rig, "_known_marketplace_path", lambda name: str(mp_dir))
    cfg = {"claude": {"marketplace": str(mp_dir), "plugin": "bh", "scope": "user"}}
    with patch("beadhive.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)

    cmds = [c[0][0] for c in mock_run.call_args_list]
    assert cmds[0] == ["claude", "plugin", "marketplace", "add", str(mp_dir.resolve())]
    assert len(cmds) == 2


def test_known_marketplace_path_reads_directory_sources(tmp_path, monkeypatch):
    """The reader returns the local path for directory-sourced marketplaces only —
    remote (github) registrations and unknown names return ''."""
    monkeypatch.setenv("HOME", str(tmp_path))
    registry_file = tmp_path / ".claude" / "plugins" / "known_marketplaces.json"
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(
        json.dumps(
            {
                "ws": {"source": {"source": "directory", "path": "/x/y"}},
                "remote": {"source": {"source": "github", "repo": "a/b"}},
            }
        )
    )
    assert _real_known_marketplace_path("ws") == "/x/y"
    assert _real_known_marketplace_path("remote") == ""
    assert _real_known_marketplace_path("missing") == ""


# ---- _install_plugin_claude writes NOTHING to disk ----


def test_install_plugin_claude_writes_no_agent_files(tmp_path):
    cfg = {"claude": {"marketplace": ".", "plugin": "bh", "scope": "user"}}
    with patch("beadhive.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        rig._install_plugin_claude(cfg)

    # No .claude/agents/ directory should have been created
    assert not (tmp_path / ".claude" / "agents").exists()


# ---- _do_claude branching via onboard._do_claude ----


def test_do_claude_plugin_mode_calls_plugin_installer(tmp_path):
    """In plugin mode, _do_claude must call _install_plugin_claude (not _install_agents_claude)."""
    from beadhive import onboard

    class FakeCtx:
        base = tmp_path
        cfg = {"claude": {"source": "plugin"}}
        force = False
        provider = "github"
        org = "acme"
        repo = "api"

    ctx = FakeCtx()
    with (
        patch("beadhive.rig._install_claude_settings"),
        patch("beadhive.rig._install_plugin_claude") as mock_plugin,
        patch("beadhive.rig._install_agents_claude") as mock_agents,
        patch("beadhive.rig._install_sandbox_grant"),
        patch("beadhive.rig._ensure_agf_hint"),
    ):
        onboard._do_claude(ctx)

    mock_plugin.assert_called_once()
    mock_agents.assert_not_called()


def test_do_claude_copy_mode_calls_agents_installer(tmp_path):
    """In copy mode, _do_claude must call _install_agents_claude (not _install_plugin_claude)."""
    from beadhive import onboard

    class FakeCtx:
        base = tmp_path
        cfg = {"claude": {"source": "copy"}}
        force = False
        provider = "github"
        org = "acme"
        repo = "api"

    ctx = FakeCtx()
    with (
        patch("beadhive.rig._install_claude_settings"),
        patch("beadhive.rig._install_plugin_claude") as mock_plugin,
        patch("beadhive.rig._install_agents_claude") as mock_agents,
        patch("beadhive.rig._install_sandbox_grant"),
        patch("beadhive.rig._ensure_agf_hint"),
    ):
        onboard._do_claude(ctx)

    mock_agents.assert_called_once()
    mock_plugin.assert_not_called()


def test_do_claude_local_steps_land_when_plugin_install_fails(tmp_path):
    """Regression: the settings, sandbox grant, and CLAUDE.md AGF hint
    are local + idempotent, so they must land BEFORE the fallible external `claude` CLI
    plugin install — and a plain re-run after the failure must converge."""
    from beadhive import onboard

    class FakeCtx:
        base = tmp_path
        cfg = {
            "claude": {"source": "plugin"},
            "worktrees": {"ephemeral": False, "path": str(tmp_path / "wt")},
        }
        force = False
        provider = "github"
        org = "acme"
        repo = "api"

    ctx = FakeCtx()
    # First run: the external `claude` CLI aborts (e.g. marketplace add rejected).
    with (
        patch("beadhive.rig.run", side_effect=RuntimeError("claude CLI rejected")),
        pytest.raises(RuntimeError),
    ):
        onboard._do_claude(ctx)

    # The independent local steps landed despite the plugin-install failure.
    assert (tmp_path / ".claude" / "settings.json").exists()
    grant = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    subtree = grant["sandbox"]["filesystem"]["allowWrite"]
    assert any(entry.endswith("github/acme/api") for entry in subtree)
    assert rig._AGF_MARK_START in (tmp_path / "CLAUDE.md").read_text()

    snapshot = {
        name: (tmp_path / name).read_text()
        for name in (".claude/settings.json", ".claude/settings.local.json", "CLAUDE.md")
    }

    # Re-run (no --force) with a working CLI: plugin installs, local steps converge
    # idempotently instead of erroring or duplicating content.
    with patch("beadhive.rig.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0)
        onboard._do_claude(ctx)

    assert mock_run.call_count == 2  # marketplace add + plugin install
    for name, before in snapshot.items():
        assert (tmp_path / name).read_text() == before


# ---- _do_skills guard: skip local copy in plugin mode ----


def test_do_skills_skipped_in_plugin_mode_with_claude():
    """--skills is a no-op (with warning) when source==plugin and ctx.claude==True."""
    from beadhive import onboard

    class FakeCtx:
        base = None  # type: ignore
        cfg = {"claude": {"source": "plugin"}}
        force = False
        claude = True

    ctx = FakeCtx()
    with (
        patch("beadhive.rig._install_skills") as mock_skills,
        patch("beadhive.rig._link_skills_claude") as mock_link,
    ):
        onboard._do_skills(ctx)

    mock_skills.assert_not_called()
    mock_link.assert_not_called()


def test_do_skills_runs_in_copy_mode():
    """In copy mode, _do_skills writes skills + link normally."""
    from beadhive import onboard

    class FakeCtx:
        base = None  # type: ignore
        cfg = {"claude": {"source": "copy"}}
        force = False
        claude = True

    ctx = FakeCtx()
    with (
        patch("beadhive.rig._install_skills") as mock_skills,
        patch("beadhive.rig._link_skills_claude") as mock_link,
    ):
        onboard._do_skills(ctx)

    mock_skills.assert_called_once()
    mock_link.assert_called_once()


def test_do_skills_runs_without_claude_flag():
    """--skills alone (no --claude) always runs regardless of source."""
    from beadhive import onboard

    class FakeCtx:
        base = None  # type: ignore
        cfg = {"claude": {"source": "plugin"}}
        force = False
        claude = False  # --skills without --claude

    ctx = FakeCtx()
    with (
        patch("beadhive.rig._install_skills") as mock_skills,
        patch("beadhive.rig._link_skills_claude") as mock_link,
    ):
        onboard._do_skills(ctx)

    mock_skills.assert_called_once()
    mock_link.assert_not_called()  # no --claude means no symlink


# ---- CLI flag conflict: --claude --skills in plugin mode ----


def test_cli_rig_init_rejects_claude_and_skills_in_plugin_mode():
    """ws rig init --claude --skills exits non-zero with a clear error in plugin mode."""
    from typer.testing import CliRunner

    from beadhive.cli import app

    cli_runner = CliRunner()
    with (
        patch("beadhive.config.load", return_value={"claude": {"source": "plugin"}}),
        patch("beadhive.config.claude_source", return_value="plugin"),
    ):
        result = cli_runner.invoke(app, ["rig", "init", "--claude", "--skills"])

    assert result.exit_code != 0
    combined = result.output.lower()
    assert "plugin" in combined or "skills" in combined
