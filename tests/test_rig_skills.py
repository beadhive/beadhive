"""`rig init --skills` injection self-checks — skills land under ./skills (skip-existing by
default, overwrite on force), the .claude/skills symlink points at ../skills idempotently, and
--prime no longer clobbers an existing PRIME.md without force. tmp cwd; no real wheel needed
(config.skills_src falls back to the repo-root skills/)."""

from __future__ import annotations

from ws import config, rig


def _skill_names():
    return {p.name for p in config.skills_src().iterdir() if p.is_dir()}


def test_install_skills_copies_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rig._install_skills()
    for name in _skill_names():
        assert (tmp_path / "skills" / name / "SKILL.md").exists()


def test_install_skills_skips_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = next(iter(_skill_names()))
    edited = tmp_path / "skills" / name / "SKILL.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    rig._install_skills()
    assert edited.read_text() == "LOCAL EDIT"  # default never touches an existing skill


def test_install_skills_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = next(iter(_skill_names()))
    edited = tmp_path / "skills" / name / "SKILL.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("LOCAL EDIT")
    rig._install_skills(force=True)
    assert edited.read_text() != "LOCAL EDIT"


def test_link_skills_claude_creates_relative_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rig._link_skills_claude()
    link = tmp_path / ".claude" / "skills"
    assert link.is_symlink() and link.readlink().as_posix() == "../skills"
    rig._link_skills_claude()  # idempotent — no raise, link unchanged
    assert link.readlink().as_posix() == "../skills"


def test_link_skills_claude_skips_existing_dir_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real = tmp_path / ".claude" / "skills"
    real.mkdir(parents=True)
    rig._link_skills_claude()
    assert not real.is_symlink()  # pre-existing non-symlink left alone
    rig._link_skills_claude(force=True)
    assert real.is_symlink() and real.readlink().as_posix() == "../skills"


def test_install_prime_skips_then_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dst = tmp_path / ".beads" / "PRIME.md"
    dst.parent.mkdir()
    dst.write_text("LOCAL")
    rig._install_prime_md()
    assert dst.read_text() == "LOCAL"  # default skips
    rig._install_prime_md(force=True)
    assert dst.read_text() != "LOCAL"  # force overwrites with bundled PRIME.md
