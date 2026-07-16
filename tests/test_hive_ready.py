"""`ws hive ready` — read-only AGF readiness verdict + breakdown.

Required core-AGF checks fail the command (exit 1); optional integrations are shown but
never fail. With otel disabled (the test default) observaloop is N/A — no live probe runs.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import config, hive_ready
from harness.world import git


def _make_repo(world, *, org="myorg", repo="myrepo"):
    main = world.ws_root / "github" / org / repo
    main.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)
    (main / ".beads").mkdir()
    world.chdir(main)
    return main


def _register(world, *, org="myorg", repo="myrepo", prefix="mr", kind="personal", furnish=""):
    cfg = config.load()
    entry = {"provider": "github", "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    if furnish:
        entry["furnish"] = furnish
    cfg.setdefault("managed_repos", []).append(entry)
    config.save(cfg)


def _fake_plugin(world):
    """Point BH_PLUGIN_DIR at a minimal plugin tree — the plugin is no longer vendored in-repo
    (beadhive/claude-plugin is canonical), so tests supply their own skills/agents source."""
    root = world.tmp / "fake-plugin"
    (root / "skills" / "demo-skill").mkdir(parents=True)
    (root / "skills" / "demo-skill" / "SKILL.md").write_text("skill\n")
    (root / "agents").mkdir()
    (root / "agents" / "developer.md").write_text("agent\n")
    world._monkeypatch.setenv("BH_PLUGIN_DIR", str(root))
    return root


def _make_ready(world):
    """Fully-set-up core-AGF hive: registered (furnished) + claude settings + skills + agents."""
    _fake_plugin(world)
    main = _make_repo(world)
    _register(world)
    (main / ".claude").mkdir()
    (main / ".claude" / "settings.json").write_text("{}\n")
    # one real bundled skill name so the skills check resolves
    name = next(p.name for p in config.skills_src().iterdir() if p.is_dir())
    (main / "skills" / name).mkdir(parents=True)
    # one real bundled agent def so the agents check resolves
    agent_name = next(p.name for p in config.agents_src().iterdir() if p.suffix == ".md")
    (main / ".claude" / "agents").mkdir(parents=True)
    (main / ".claude" / "agents" / agent_name).write_text("agent\n")
    return main


def _run(verbose=False):
    """Call run_check, returning the typer.Exit code (0 ready / 1 not)."""
    with pytest.raises(typer.Exit) as exc:
        hive_ready.run_check(verbose)
    return exc.value.exit_code


def test_unregistered_repo_not_ready(world, capsys):
    _make_repo(world)  # git repo but never `hive init`ed

    assert _run() == 1
    out = capsys.readouterr().out
    assert "not ready" in out


def test_fully_set_up_hive_is_ready(world, capsys):
    _make_ready(world)

    assert _run() == 0
    assert "ready for AGF" in capsys.readouterr().out


def test_missing_required_fails(world):
    main = _make_ready(world)
    # A furnished hive (missing `furnish` key + non-fork kind infers "full") requires the
    # tracked claude settings.
    (main / ".claude" / "settings.json").unlink()

    assert _run() == 1


def test_zero_footprint_hive_is_ready_without_repo_files(world):
    """A declared zero-footprint hive is green with no tracked furniture at all."""
    _fake_plugin(world)
    _make_repo(world)
    _register(world, furnish="none")

    assert _run() == 0


def test_prime_md_presence_warns_but_never_fails(world, capsys):
    main = _make_ready(world)
    (main / ".beads" / "PRIME.md").write_text("legacy\n")

    assert _run(verbose=True) == 0  # warn-level only
    assert "deprecated" in capsys.readouterr().out


def test_bd_claude_block_presence_warns_but_never_fails(world, capsys):
    main = _make_ready(world)
    (main / "CLAUDE.md").write_text(
        "<!-- BEGIN BEADS INTEGRATION v:1 profile:full hash:6cd5cc61 -->\nstale\n"
    )

    assert _run(verbose=True) == 0  # warn-level only
    assert "BEADS INTEGRATION block present" in capsys.readouterr().out


def test_verbose_breakdown_sections_and_optional_na(world, capsys):
    _make_ready(world)

    assert _run(verbose=True) == 0
    out = capsys.readouterr().out
    assert "# Required" in out and "# Optional" in out
    assert "✓ hive registered" in out
    # otel off → observaloop is N/A (-), never probed; hints absent → optional •
    assert "- observaloop profile" in out
    assert "• AGENTS.md hint" in out


def test_cli_exit_codes(world):
    from typer.testing import CliRunner

    from beadhive.cli import app

    _make_ready(world)
    assert CliRunner().invoke(app, ["hive", "ready"]).exit_code == 0

    (world.ws_root / "github" / "myorg" / "myrepo" / ".claude" / "settings.json").unlink()
    assert CliRunner().invoke(app, ["hive", "ready"]).exit_code == 1


# ---------------------------------------------------------------------------
# Generic plugin readiness line (bead .8) — orca is N/A when disabled,
# ok/missing (live list_repos probe) when enabled.
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from beadhive import hive_ready as _rr  # noqa: E402
from beadhive import orca  # noqa: E402

_ENTRY = {"provider": "github", "org": "acme", "repo": "api", "prefix": "a-api"}


def test_plugin_line_na_when_orca_disabled(world, monkeypatch):
    # default config: git_workspace off → orca_enabled False → N/A (never probed).
    monkeypatch.setattr(config, "orca_enabled", lambda cfg, e=None: False)
    checks = _rr._plugin_checks({}, _ENTRY)
    line = next(c for c in checks if c.label == "orca")
    assert line.state == "na"
    assert line.detail == "disabled"


def test_plugin_line_ok_when_registered(world, monkeypatch):
    monkeypatch.setattr(config, "orca_enabled", lambda cfg, e=None: True)
    clone = Path(orca.workspace_root()) / "github" / "acme" / "api"
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [{"path": str(clone)}])
    line = next(c for c in _rr._plugin_checks({}, _ENTRY) if c.label == "orca")
    assert line.state == "ok"
    assert line.detail == "registered"


def test_plugin_line_missing_when_not_registered(world, monkeypatch):
    monkeypatch.setattr(config, "orca_enabled", lambda cfg, e=None: True)
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [])
    line = next(c for c in _rr._plugin_checks({}, _ENTRY) if c.label == "orca")
    assert line.state == "missing"


def test_scan_includes_orca_line(world, monkeypatch):
    main = _make_ready(world)
    monkeypatch.setattr(config, "orca_enabled", lambda cfg, e=None: False)
    cfg = config.load()
    entry = {"provider": "github", "org": "myorg", "repo": "myrepo",
             "prefix": "mr", "kind": "personal"}
    checks = hive_ready.scan(cfg, ("github", "myorg", "myrepo"), entry, main)
    assert any(c.label == "orca" for c in checks)
