"""`hive init --opencode` furnishing self-checks: opencode.json (deep-merged bh MCP server),
translated `.opencode/agents/*.md` seat defs, a global skills install
(~/.config/opencode/skills — overridden to tmp_path in every test so real operator state is
never touched), and the AGENTS.md AGF hint. Mirrors test_hive_claude.py / test_hive_agents.py /
test_hive_skills.py's patterns for the claude installer family."""

from __future__ import annotations

import json

import pytest

from beadhive import config, hive, hub, registry
from harness.world import git

_SAMPLE_AGENT_MD = """\
---
name: developer
description: >-
  DEVELOPER — implements ONE assigned bead to a reviewable state inside a
  bh-managed worktree, then submits.
tools: Bash, Read, Edit, Write, Grep, Glob, Skill
skills: bh:developer, bh:work
model: sonnet
---

# Developer

You are a **developer**. Body text passes through unchanged.
"""

_SAMPLE_AGENT_MD_NO_SKILLS = """\
---
name: analyst
description: >-
  ANALYST — fire-and-forget research sub-agent.
tools: Bash, Read, Grep, Glob, WebFetch, WebSearch
model: sonnet
---

# Analyst

No skills field on this one.
"""


# ---- opencode.json (deep-merge) ---------------------------------------------


def test_install_opencode_config_writes_asset(tmp_path):
    hive._install_opencode_config(tmp_path)
    data = json.loads((tmp_path / "opencode.json").read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["mcp"]["bh"] == {"type": "local", "command": ["bh-mcp"]}
    assert data["snapshot"] is False


def test_install_opencode_config_deep_merges_existing(tmp_path):
    existing = {"mcp": {"other": {"type": "local", "command": ["x"]}}, "theme": "dark"}
    (tmp_path / "opencode.json").write_text(json.dumps(existing))
    hive._install_opencode_config(tmp_path)
    data = json.loads((tmp_path / "opencode.json").read_text())
    assert data["theme"] == "dark"  # unrelated key preserved
    assert data["mcp"]["other"] == {"type": "local", "command": ["x"]}  # unrelated mcp preserved
    assert data["mcp"]["bh"] == {"type": "local", "command": ["bh-mcp"]}  # bh server added
    assert data["snapshot"] is False


def test_install_opencode_config_is_idempotent(tmp_path):
    hive._install_opencode_config(tmp_path)
    once = (tmp_path / "opencode.json").read_text()
    hive._install_opencode_config(tmp_path)
    assert (tmp_path / "opencode.json").read_text() == once


# ---- _translate_agent_md (frontmatter translation) ---------------------------


def test_translate_agent_md_keeps_description_adds_mode():
    out = hive._translate_agent_md(_SAMPLE_AGENT_MD)
    assert "mode: all" in out
    assert "description:" in out
    assert "DEVELOPER" in out


def test_translate_agent_md_drops_tools_and_model():
    out = hive._translate_agent_md(_SAMPLE_AGENT_MD)
    assert "tools:" not in out
    assert "model:" not in out


def test_translate_agent_md_converts_skills_to_body_preamble():
    out = hive._translate_agent_md(_SAMPLE_AGENT_MD)
    assert "skills:" not in out  # dropped from frontmatter
    assert "At session start, load skills via the skill tool: bh:developer, bh:work." in out
    # the preamble lands in the body, after the closing frontmatter fence
    fence = out.index("\n---\n", 3) if out.startswith("---\n") else -1
    assert fence != -1
    assert out.index("At session start") > fence


def test_translate_agent_md_body_passes_through():
    out = hive._translate_agent_md(_SAMPLE_AGENT_MD)
    assert "# Developer" in out
    assert "Body text passes through unchanged." in out


def test_translate_agent_md_no_skills_field_no_preamble():
    out = hive._translate_agent_md(_SAMPLE_AGENT_MD_NO_SKILLS)
    assert "mode: all" in out
    assert "tools:" not in out
    assert "model:" not in out
    assert "load skills via the skill tool" not in out
    assert "# Analyst" in out


def test_translate_agent_md_no_frontmatter_passes_through():
    text = "# Just a heading\n\nno frontmatter here.\n"
    assert hive._translate_agent_md(text) == text


# ---- _install_agents_opencode -------------------------------------------------


def _write_fake_plugin_agents(tmp_path, monkeypatch):
    root = tmp_path / "fake-plugin"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "developer.md").write_text(_SAMPLE_AGENT_MD)
    (agents / "analyst.md").write_text(_SAMPLE_AGENT_MD_NO_SKILLS)
    monkeypatch.setenv("BH_PLUGIN_DIR", str(root))
    return root


def test_install_agents_opencode_translates_all(tmp_path, monkeypatch):
    _write_fake_plugin_agents(tmp_path, monkeypatch)
    dst = tmp_path / "hive"
    dst.mkdir()
    hive._install_agents_opencode(base=dst)
    for name in ("developer.md", "analyst.md"):
        written = (dst / ".opencode" / "agents" / name).read_text()
        assert "mode: all" in written
        assert "tools:" not in written


def test_install_agents_opencode_skips_existing(tmp_path, monkeypatch):
    _write_fake_plugin_agents(tmp_path, monkeypatch)
    dst = tmp_path / "hive"
    edited = dst / ".opencode" / "agents" / "developer.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    hive._install_agents_opencode(base=dst)
    assert edited.read_text() == "LOCAL EDIT"


def test_install_agents_opencode_force_overwrites(tmp_path, monkeypatch):
    _write_fake_plugin_agents(tmp_path, monkeypatch)
    dst = tmp_path / "hive"
    edited = dst / ".opencode" / "agents" / "developer.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    hive._install_agents_opencode(force=True, base=dst)
    assert edited.read_text() != "LOCAL EDIT"


# ---- _install_skills_opencode (global install) -------------------------------


def _skill_names():
    return {p.name for p in config.skills_src().iterdir() if p.is_dir()}


def test_install_skills_opencode_copies_all_to_global_home(tmp_path, monkeypatch, fake_plugin):
    skills_home = tmp_path / "opencode-skills-home"
    monkeypatch.setenv("BH_OPENCODE_SKILLS_HOME", str(skills_home))
    hive._install_skills_opencode()
    for name in _skill_names():
        assert (skills_home / name / "SKILL.md").exists()


def test_install_skills_opencode_skips_existing(tmp_path, monkeypatch, fake_plugin):
    skills_home = tmp_path / "opencode-skills-home"
    monkeypatch.setenv("BH_OPENCODE_SKILLS_HOME", str(skills_home))
    name = next(iter(_skill_names()))
    edited = skills_home / name / "SKILL.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    hive._install_skills_opencode()
    assert edited.read_text() == "LOCAL EDIT"


def test_install_skills_opencode_force_overwrites(tmp_path, monkeypatch, fake_plugin):
    skills_home = tmp_path / "opencode-skills-home"
    monkeypatch.setenv("BH_OPENCODE_SKILLS_HOME", str(skills_home))
    name = next(iter(_skill_names()))
    edited = skills_home / name / "SKILL.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    hive._install_skills_opencode(force=True)
    assert edited.read_text() != "LOCAL EDIT"


def test_opencode_skills_home_default_is_dot_config(monkeypatch):
    monkeypatch.delenv("BH_OPENCODE_SKILLS_HOME", raising=False)
    from pathlib import Path

    assert config.opencode_skills_home() == Path.home() / ".config" / "opencode" / "skills"


# ---- end-to-end: `hive.onboard(..., opencode=True)` --------------------------
# Mirrors test_hive_onboard.py's local-folder onboard pattern.


@pytest.fixture
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(hub, "sync", lambda: calls.append(True))
    return calls


def _make_local_repo(world, *, org="acme", repo="widget"):
    target = world.ws_root / "github" / org / repo
    target.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=target)
    (target / ".beads").mkdir()
    return target


@pytest.fixture
def opencode_fake_plugin(tmp_path, monkeypatch):
    """BH_PLUGIN_DIR -> a minimal plugin tree with a real-shaped (frontmatter-bearing) agent
    def, so onboard-level assertions can check the translation actually ran end-to-end."""
    root = tmp_path / "fake-plugin"
    (root / "skills" / "demo-skill").mkdir(parents=True)
    (root / "skills" / "demo-skill" / "SKILL.md").write_text("skill\n")
    (root / "agents").mkdir()
    (root / "agents" / "developer.md").write_text(_SAMPLE_AGENT_MD)
    monkeypatch.setenv("BH_PLUGIN_DIR", str(root))
    return root


def test_onboard_opencode_writes_config_agents_and_agf_hint(
    world, synced, monkeypatch, tmp_path, opencode_fake_plugin
):
    target = _make_local_repo(world)
    world.chdir(world.ws_root)
    skills_home = tmp_path / "opencode-skills-home"
    monkeypatch.setenv("BH_OPENCODE_SKILLS_HOME", str(skills_home))
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    hive.onboard("github/acme/widget", opencode=True)

    assert (target / "opencode.json").exists()
    agents_dir = target / ".opencode" / "agents"
    assert (agents_dir / "developer.md").exists()
    assert "mode: all" in (agents_dir / "developer.md").read_text()
    assert (target / "AGENTS.md").exists()  # OpenCode reads AGENTS.md natively
    for name in _skill_names():
        assert (skills_home / name / "SKILL.md").exists()
    assert synced == [True]


def test_onboard_opencode_is_idempotent(
    world, synced, monkeypatch, tmp_path, opencode_fake_plugin
):
    target = _make_local_repo(world)
    world.chdir(world.ws_root)
    skills_home = tmp_path / "opencode-skills-home"
    monkeypatch.setenv("BH_OPENCODE_SKILLS_HOME", str(skills_home))
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    hive.onboard("github/acme/widget", opencode=True)
    edited = target / ".opencode" / "agents" / "developer.md"
    edited.write_text("LOCAL EDIT")

    # re-run: never clobbers local edits (the furnished-hive scaffold commit dirties the tree
    # with the LOCAL EDIT itself — skip the pre-existing dirty-tree/branch checks like
    # test_onboard_skip_check_proceeds_past_dirty_and_branch does)
    hive.onboard(
        "github/acme/widget", opencode=True, skip_check="dirty-tree,on-default-branch",
    )

    assert edited.read_text() == "LOCAL EDIT"


def test_onboard_opencode_force_refreshes(
    world, synced, monkeypatch, tmp_path, opencode_fake_plugin
):
    target = _make_local_repo(world)
    world.chdir(world.ws_root)
    skills_home = tmp_path / "opencode-skills-home"
    monkeypatch.setenv("BH_OPENCODE_SKILLS_HOME", str(skills_home))
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    hive.onboard("github/acme/widget", opencode=True)
    edited = target / ".opencode" / "agents" / "developer.md"
    edited.write_text("LOCAL EDIT")

    hive.onboard(
        "github/acme/widget", opencode=True, force=True,
        skip_check="dirty-tree,on-default-branch",
    )

    assert edited.read_text() != "LOCAL EDIT"
