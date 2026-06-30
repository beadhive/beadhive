"""`rig init --claude` agents injection self-checks — agent defs land under .claude/agents/
(skip-existing by default, overwrite on force). tmp cwd; no real wheel needed
(config.agents_src falls back to the repo-root .claude/agents/)."""

from __future__ import annotations

from ws import config, rig


def _agent_names():
    return {p.name for p in config.agents_src().iterdir() if p.suffix == ".md"}


def test_install_agents_claude_copies_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rig._install_agents_claude()
    for name in _agent_names():
        assert (tmp_path / ".claude" / "agents" / name).exists()


def test_install_agents_claude_skips_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = next(iter(_agent_names()))
    edited = tmp_path / ".claude" / "agents" / name
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    rig._install_agents_claude()
    assert edited.read_text() == "LOCAL EDIT"  # default never touches an existing agent def


def test_install_agents_claude_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = next(iter(_agent_names()))
    edited = tmp_path / ".claude" / "agents" / name
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    rig._install_agents_claude(force=True)
    assert edited.read_text() != "LOCAL EDIT"
