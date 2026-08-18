"""`hive init --codex` sandbox-grant self-checks — the Codex-native twin of
test_hive_claude.py: the grant lands in `.codex/config.toml`'s `[sandbox_workspace_write]`
table, is self-healing on a worktrees-root move, idempotent, and never clobbers an unmanaged
pre-existing table. Pure strings + tmp_path; no real Codex sandbox needed (see hive.py's
bh-odulu comment block for the live-binary verification this design is based on)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from beadhive import hive, worktree

SUFFIX = "github/o/r"


def test_grant_block_roundtrips_through_tomllib():
    block = hive._codex_grant_block(["~/wts/github/o/r"])
    parsed = tomllib.loads(block)
    assert parsed["sandbox_workspace_write"]["writable_roots"] == ["~/wts/github/o/r"]


def test_grant_roots_reads_only_the_managed_block():
    text = hive._codex_grant_block(["~/wts/github/o/r"])
    assert hive._codex_grant_roots(text) == ["~/wts/github/o/r"]


def test_grant_roots_empty_when_markers_absent():
    assert hive._codex_grant_roots("") == []
    assert hive._codex_grant_roots("[sandbox_workspace_write]\nwritable_roots = []\n") == []


def test_subtree_matches_claude_subtree(monkeypatch):
    # Codex reuses the exact same _sandbox_subtree helper Claude's grant uses — same
    # granularity, same hive's own worktree subtree, not the whole shared root.
    root = Path.home() / ".ws-test-wts-codex"
    monkeypatch.setenv("BH_WORKTREES", str(root))
    sub = hive._sandbox_subtree({}, "github", "o", "r")
    entry = {"provider": "github", "org": "o", "repo": "r"}
    assert os.path.expanduser(sub) == str(worktree.wt_dir(entry, "leaf").parent)


def test_install_grant_is_noop_when_ephemeral(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": True}}, "github", "o", "r")
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_install_grant_writes_when_persistent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    f = tmp_path / ".codex" / "config.toml"
    assert f.exists()
    parsed = tomllib.loads(f.read_text())
    roots = parsed["sandbox_workspace_write"]["writable_roots"]
    assert any(r.endswith("/github/o/r") for r in roots)


def test_install_grant_git_excludes_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    (tmp_path / ".git").mkdir()
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert ".codex/config.toml" in exclude


def test_install_grant_replaces_stale_entry_on_move(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts-old"))
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    old_text = (tmp_path / ".codex" / "config.toml").read_text()
    assert "wts-old" in old_text

    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts-new"))
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    new_text = (tmp_path / ".codex" / "config.toml").read_text()
    roots = tomllib.loads(new_text)["sandbox_workspace_write"]["writable_roots"]
    assert not any("wts-old" in r for r in roots)  # stale entry gone, not accumulated
    assert any("wts-new" in r for r in roots)


def test_install_grant_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    once = (tmp_path / ".codex" / "config.toml").read_text()
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    twice = (tmp_path / ".codex" / "config.toml").read_text()
    assert once == twice  # re-running rewrites, never accumulates blank lines/entries


def test_install_grant_preserves_unrelated_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-5"\n')
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert 'model = "gpt-5"' in text  # operator's own unrelated config survives
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5"
    assert parsed["sandbox_workspace_write"]["writable_roots"]


def test_install_grant_refuses_to_clobber_unmanaged_table(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    (tmp_path / ".codex").mkdir()
    unmanaged = '[sandbox_workspace_write]\nwritable_roots = ["/some/hand-written/root"]\n'
    (tmp_path / ".codex" / "config.toml").write_text(unmanaged)
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert text == unmanaged  # left untouched, not corrupted with a duplicate TOML table
    assert "unmanaged" in capsys.readouterr().err


def test_codex_granted_subtree_and_grant_is_current(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_WORKTREES", str(Path.home() / ".ws-codex-new"))
    clone = tmp_path / "clone"
    (clone / ".codex").mkdir(parents=True)
    (clone / ".codex" / "config.toml").write_text(
        hive._codex_grant_block(["~/.ws-codex-old/github/o/r"])
    )
    assert hive.codex_grant_is_current({}, clone, "github", "o", "r") is False  # stale
    assert hive.codex_grant_is_current({}, tmp_path / "bare", "github", "o", "r") is None
