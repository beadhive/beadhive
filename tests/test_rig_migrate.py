"""`bh rig migrate` — rewrite ws -> bh across already-onboarded managed repos.

Contract:
  * rewrites the AGENTS.md/CLAUDE.md managed AGF stanza, upgrading the old
    `<!-- ws:agf:start/end -->` marker to `<!-- bh:agf:start/end -->` and refreshing its
    content to the canonical (bh) block;
  * rewrites `.claude/settings.json` hook/statusLine commands that invoke `ws`;
  * rewrites bundled skill files copied into ./skills/ that reference `ws`;
  * a second run is a no-op (nothing left to rewrite);
  * --dry-run prints the diff and writes nothing.
"""

from __future__ import annotations

import json

from beadhive import config, rig_migrate
from harness.world import git


def _make_repo(world, *, org="acme", repo="widget"):
    target = world.ws_root / "github" / org / repo
    target.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=target)
    (target / ".beads").mkdir()
    return target


def _register(world, *, org="acme", repo="widget", prefix="widget", kind="prototype"):
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": "github", "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    )
    config.save(cfg)


def _old_agents_md() -> str:
    return (
        "intro\n\n"
        "<!-- ws:agf:start (managed by `ws rig init` — edit outside these markers) -->\n"
        "## AGF — Agentic Git Flow\n"
        "Drive beads with `ws work`.\n"
        "<!-- ws:agf:end -->\n\n"
        "outro\n"
    )


def _old_settings_json() -> str:
    return json.dumps(
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "bd prime --hook-json"}]}
                ]
            },
            "statusLine": {"type": "command", "command": "ws statusline", "padding": 0},
        },
        indent=2,
    )


def _seed_onboarded_repo(target):
    """A repo carrying the full set of ws-era artifacts `rig migrate` should upgrade."""
    (target / "AGENTS.md").write_text(_old_agents_md())
    (target / "CLAUDE.md").write_text(_old_agents_md())
    (target / ".claude").mkdir()
    (target / ".claude" / "settings.json").write_text(_old_settings_json())
    skills = target / "skills" / "developer"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("Run `ws work claim <id>` then `ws work submit <id>`.\n")


def test_migrate_rewrites_ws_to_bh(world):
    target = _make_repo(world)
    _register(world)
    _seed_onboarded_repo(target)

    rig_migrate.migrate()

    agents = (target / "AGENTS.md").read_text()
    assert "<!-- bh:agf:start" in agents
    assert "<!-- ws:agf:start" not in agents
    assert "bh work" in agents
    assert "intro" in agents and "outro" in agents  # surrounding content preserved

    claude = (target / "CLAUDE.md").read_text()
    assert "<!-- bh:agf:start" in claude

    settings = json.loads((target / ".claude" / "settings.json").read_text())
    assert settings["statusLine"]["command"] == "bh statusline"
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "bd prime --hook-json"

    skill = (target / "skills" / "developer" / "SKILL.md").read_text()
    assert "bh work claim" in skill
    assert "bh work submit" in skill
    assert "ws work" not in skill


def test_migrate_second_run_is_noop(world, capsys):
    target = _make_repo(world)
    _register(world)
    _seed_onboarded_repo(target)

    rig_migrate.migrate()
    after_first = {
        p: (target / p).read_text()
        for p in ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", "skills/developer/SKILL.md")
    }
    capsys.readouterr()

    rig_migrate.migrate()
    out = capsys.readouterr().out
    after_second = {p: (target / p).read_text() for p in after_first}

    assert after_first == after_second  # byte-identical — nothing left to rewrite
    assert "0 changed" in out
    assert "1 up to date" in out


def test_migrate_dry_run_shows_diff_and_writes_nothing(world, capsys):
    target = _make_repo(world)
    _register(world)
    _seed_onboarded_repo(target)
    before = {
        p: (target / p).read_text()
        for p in ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", "skills/developer/SKILL.md")
    }

    rig_migrate.migrate(dry_run=True)

    out = capsys.readouterr().out
    assert "-ws statusline" in out or "-  ws statusline" in out or "ws statusline" in out
    assert "+bh statusline" in out or "bh statusline" in out
    assert "1 would change" in out
    for p, text in before.items():
        assert (target / p).read_text() == text  # untouched


def test_migrate_skips_repo_without_checkout(world, capsys):
    # Registered but never cloned — migrate must not choke on a missing directory.
    _register(world, org="ghost", repo="phantom", prefix="phantom")

    rig_migrate.migrate()

    out = capsys.readouterr().err
    assert "skip: no checkout" in out


def test_migrate_single_rig_by_id(world):
    a = _make_repo(world, org="acme", repo="widget")
    b = _make_repo(world, org="acme", repo="gadget")
    _register(world, org="acme", repo="widget", prefix="widget")
    _register(world, org="acme", repo="gadget", prefix="gadget")
    _seed_onboarded_repo(a)
    _seed_onboarded_repo(b)

    rig_migrate.migrate(rig_id="widget")

    assert "<!-- bh:agf:start" in (a / "AGENTS.md").read_text()
    assert "<!-- ws:agf:start" in (b / "AGENTS.md").read_text()  # untouched — not targeted


def test_migrate_no_registered_rigs(world, capsys):
    rig_migrate.migrate()
    out = capsys.readouterr().out
    assert "No registered rigs." in out


def test_migrate_repo_with_no_ws_artifacts_is_clean(world, capsys):
    # A repo onboarded with the new bh assets already — nothing to migrate.
    target = _make_repo(world)
    _register(world)
    from beadhive import rig

    rig._ensure_agf_hint(target / "AGENTS.md", force=False, flag="--agents")

    rig_migrate.migrate()

    out = capsys.readouterr().out
    assert "1 up to date" in out
