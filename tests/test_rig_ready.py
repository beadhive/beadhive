"""`ws rig ready` — read-only AGF readiness verdict + breakdown.

Required core-AGF checks fail the command (exit 1); optional integrations are shown but
never fail. With otel disabled (the test default) observaloop is N/A — no live probe runs.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import config, rig_ready
from harness.world import git


def _make_repo(world, *, org="myorg", repo="myrepo"):
    main = world.ws_root / "github" / org / repo
    main.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)
    (main / ".beads").mkdir()
    world.chdir(main)
    return main


def _register(world, *, org="myorg", repo="myrepo", prefix="mr", kind="personal"):
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": "github", "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    )
    config.save(cfg)


def _make_ready(world):
    """Fully-set-up core-AGF rig: registered + PRIME.md + claude settings + skills + agents."""
    main = _make_repo(world)
    _register(world)
    (main / ".beads" / "PRIME.md").write_text("prime\n")
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
        rig_ready.run_check(verbose)
    return exc.value.exit_code


def test_unregistered_repo_not_ready(world, capsys):
    _make_repo(world)  # git repo but never `rig init`ed

    assert _run() == 1
    out = capsys.readouterr().out
    assert "not ready" in out


def test_fully_set_up_rig_is_ready(world, capsys):
    _make_ready(world)

    assert _run() == 0
    assert "ready for AGF" in capsys.readouterr().out


def test_missing_required_fails(world):
    main = _make_ready(world)
    (main / ".beads" / "PRIME.md").unlink()  # drop one required item

    assert _run() == 1


def test_verbose_breakdown_sections_and_optional_na(world, capsys):
    _make_ready(world)

    assert _run(verbose=True) == 0
    out = capsys.readouterr().out
    assert "# Required" in out and "# Optional" in out
    assert "✓ rig registered" in out
    # otel off → observaloop is N/A (-), never probed; hints absent → optional •
    assert "- observaloop profile" in out
    assert "• AGENTS.md hint" in out


def test_cli_exit_codes(world):
    from typer.testing import CliRunner

    from beadhive.cli import app

    _make_ready(world)
    assert CliRunner().invoke(app, ["rig", "ready"]).exit_code == 0

    (world.ws_root / "github" / "myorg" / "myrepo" / ".claude" / "settings.json").unlink()
    assert CliRunner().invoke(app, ["rig", "ready"]).exit_code == 1
