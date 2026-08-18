"""`hive init --codex` sandbox-grant self-checks — the Codex-native twin of
test_hive_claude.py: the grant lands in `.codex/config.toml`'s `[sandbox_workspace_write]`
table, is self-healing on a worktrees-root move, idempotent, and never clobbers an unmanaged
pre-existing table. Pure strings + tmp_path; no real Codex sandbox needed (see hive.py's
bh-odulu comment block for the live-binary verification this design is based on)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from beadhive import config, hive, worktree

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


# ---- global Codex sandbox grant (bh-n0m7n) -----------------------------------
# Opt-in, coarser alternative: one entry covering the WHOLE worktrees_root() in the GLOBAL
# ~/.codex/config.toml (config.codex_home(), sandboxed per-test by conftest's
# _sandbox_codex_home) instead of one hive's own subtree in the project-local file. Same
# managed-marker splice as the per-hive writer (_write_codex_grant_block) — reuses
# _codex_grant_block/_codex_grant_roots directly, only the target file/value differ.


def test_install_global_grant_is_noop_when_ephemeral():
    hive._install_global_codex_sandbox_grant({"worktrees": {"ephemeral": True}})
    assert not (config.codex_home() / "config.toml").exists()


def test_install_global_grant_writes_the_whole_root(monkeypatch):
    monkeypatch.setenv("BH_WORKTREES", str(Path.home() / ".ws-codex-global-wts"))
    hive._install_global_codex_sandbox_grant({"worktrees": {"ephemeral": False}})
    f = config.codex_home() / "config.toml"
    parsed = tomllib.loads(f.read_text())
    roots = parsed["sandbox_workspace_write"]["writable_roots"]
    assert "~/.ws-codex-global-wts" in roots
    assert hive.global_codex_grant_is_current({}) is True


def test_install_global_grant_is_idempotent(monkeypatch):
    monkeypatch.setenv("BH_WORKTREES", str(Path.home() / ".ws-codex-global-wts2"))
    hive._install_global_codex_sandbox_grant({"worktrees": {"ephemeral": False}})
    once = (config.codex_home() / "config.toml").read_text()
    hive._install_global_codex_sandbox_grant({"worktrees": {"ephemeral": False}})
    twice = (config.codex_home() / "config.toml").read_text()
    assert once == twice


def test_install_global_grant_preserves_unrelated_content(monkeypatch):
    monkeypatch.setenv("BH_WORKTREES", str(Path.home() / ".ws-codex-global-wts3"))
    home = config.codex_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        'model = "gpt-5"\n\n[projects."/some/trusted/project"]\ntrust_level = "trusted"\n'
    )
    hive._install_global_codex_sandbox_grant({"worktrees": {"ephemeral": False}})
    text = (home / "config.toml").read_text()
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5"  # operator's own unrelated config survives
    assert parsed["projects"]["/some/trusted/project"]["trust_level"] == "trusted"
    assert parsed["sandbox_workspace_write"]["writable_roots"]


def test_global_codex_grant_is_current_false_when_absent():
    assert hive.global_codex_grant_is_current({}) is False


def test_per_hive_and_global_codex_grants_coexist(tmp_path, monkeypatch):
    """Different files (project-local .codex/config.toml vs global ~/.codex/config.toml) — no
    shared key, so writing both never conflicts or dedupes across the two shapes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    hive._install_codex_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    hive._install_global_codex_sandbox_grant({"worktrees": {"ephemeral": False}})
    assert hive.codex_grant_is_current({}, tmp_path, "github", "o", "r") is True
    assert hive.global_codex_grant_is_current({}) is True
