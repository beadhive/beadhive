"""`rig init --skills` injection self-checks — skills land under ./skills (skip-existing by
default, overwrite on force) and the .claude/skills symlink points at ../skills idempotently.
tmp cwd; no real wheel needed (config.skills_src falls back to the repo-root skills/)."""

from __future__ import annotations

from beadhive import config, hive


def _skill_names():
    return {p.name for p in config.skills_src().iterdir() if p.is_dir()}


def test_install_skills_copies_all(tmp_path, monkeypatch, fake_plugin):
    monkeypatch.chdir(tmp_path)
    hive._install_skills()
    for name in _skill_names():
        assert (tmp_path / "skills" / name / "SKILL.md").exists()


def test_install_skills_skips_existing(tmp_path, monkeypatch, fake_plugin):
    monkeypatch.chdir(tmp_path)
    name = next(iter(_skill_names()))
    edited = tmp_path / "skills" / name / "SKILL.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    hive._install_skills()
    assert edited.read_text() == "LOCAL EDIT"  # default never touches an existing skill


def test_install_skills_force_overwrites(tmp_path, monkeypatch, fake_plugin):
    monkeypatch.chdir(tmp_path)
    name = next(iter(_skill_names()))
    edited = tmp_path / "skills" / name / "SKILL.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    hive._install_skills(force=True)
    assert edited.read_text() != "LOCAL EDIT"


def test_link_skills_claude_creates_relative_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hive._link_skills_claude()
    link = tmp_path / ".claude" / "skills"
    assert link.is_symlink() and link.readlink().as_posix() == "../skills"
    hive._link_skills_claude()  # idempotent — no raise, link unchanged
    assert link.readlink().as_posix() == "../skills"


def test_link_skills_claude_skips_existing_dir_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real = tmp_path / ".claude" / "skills"
    real.mkdir(parents=True)
    hive._link_skills_claude()
    assert not real.is_symlink()  # pre-existing non-symlink left alone
    hive._link_skills_claude(force=True)
    assert real.is_symlink() and real.readlink().as_posix() == "../skills"


