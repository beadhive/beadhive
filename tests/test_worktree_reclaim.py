"""Automatic worktree reclaim + the live-count cap (bh-qbu9t): a merged bead's clean worktree is
removed, a dirty one never is, and provisioning at ``worktrees.max_live`` reclaims clean merged
trees first and refuses (with a message) when nothing is reclaimable."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from beadhive import worktree, worktree_cleanup


def _make_trees(root: Path, count: int) -> list[Path]:
    trees = []
    for i in range(count):
        leaf = root / "github" / "org" / "repo" / f"bead-{i}"
        leaf.mkdir(parents=True)
        (leaf / ".git").write_text("gitdir: /nowhere\n")
        trees.append(leaf)
    return trees


@pytest.fixture
def root(monkeypatch, tmp_path):
    monkeypatch.delenv("BH_WORKTREES_MAX_LIVE", raising=False)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wt"))
    return tmp_path / "wt"


# ---- config -------------------------------------------------------------------------------


def test_max_live_defaults_on_and_is_configurable(monkeypatch):
    monkeypatch.delenv("BH_WORKTREES_MAX_LIVE", raising=False)
    assert worktree.config.worktrees_max_live({}) == 64
    assert worktree.config.worktrees_max_live({"worktrees": {"max_live": 5}}) == 5
    assert worktree.config.worktrees_max_live({"worktrees": {"max_live": 0}}) == 0  # off
    assert (
        worktree.config.worktrees_max_live({"worktrees": {"max_live": -3}}) == 64
    )  # junk -> default
    monkeypatch.setenv("BH_WORKTREES_MAX_LIVE", "7")
    assert worktree.config.worktrees_max_live({"worktrees": {"max_live": 5}}) == 7  # env wins


def test_reclaim_on_merge_defaults_on():
    assert worktree.config.worktrees_reclaim_on_merge({}) is True
    assert (
        worktree.config.worktrees_reclaim_on_merge({"worktrees": {"reclaim_on_merge": False}})
        is False
    )


# ---- live cap -----------------------------------------------------------------------------


def test_live_worktree_dirs_counts_only_linked_worktrees(root):
    _make_trees(root, 3)
    (root / "github" / "org" / "repo" / "plain-dir").mkdir()  # no .git: not a worktree
    assert len(worktree.live_worktree_dirs(root)) == 3
    assert worktree.live_worktree_dirs(root / "absent") == []


def test_under_cap_is_a_noop(root, monkeypatch):
    _make_trees(root, 2)
    monkeypatch.setattr(worktree_cleanup, "prune", lambda *a, **k: pytest.fail("no reclaim needed"))
    worktree.enforce_live_cap({"worktrees": {"max_live": 3}})


def test_cap_zero_disables_the_guard(root, monkeypatch):
    _make_trees(root, 5)
    monkeypatch.setattr(worktree_cleanup, "prune", lambda *a, **k: pytest.fail("cap is off"))
    worktree.enforce_live_cap({"worktrees": {"max_live": 0}})


def test_at_cap_reclaims_clean_merged_trees_then_allows(root, monkeypatch, capsys):
    trees = _make_trees(root, 3)

    def fake_prune(*_a, **_k):  # the SAFE classifier reclaimed one tree
        (trees[0] / ".git").unlink()
        trees[0].rmdir()

    monkeypatch.setattr(worktree_cleanup, "prune", fake_prune)
    worktree.enforce_live_cap({"worktrees": {"max_live": 3}})  # 3 -> 2 < 3: allowed
    assert "reclaiming clean, merged trees" in capsys.readouterr().err


def test_at_cap_with_nothing_reclaimable_refuses_with_a_message(root, monkeypatch, capsys):
    _make_trees(root, 3)
    monkeypatch.setattr(worktree_cleanup, "prune", lambda *a, **k: None)  # nothing SAFE
    with pytest.raises(typer.Exit) as exc:
        worktree.enforce_live_cap({"worktrees": {"max_live": 3}})
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "refusing to provision another worktree" in err
    assert "worktrees.max_live=3" in err and "abandon" in err and "BH_WORKTREES_MAX_LIVE" in err


def test_a_failing_reclaim_still_ends_in_the_refusal(root, monkeypatch):
    _make_trees(root, 2)

    def boom(*_a, **_k):
        raise RuntimeError("bd down")

    monkeypatch.setattr(worktree_cleanup, "prune", boom)
    with pytest.raises(typer.Exit):
        worktree.enforce_live_cap({"worktrees": {"max_live": 2}})


def test_do_add_refuses_at_the_cap_before_creating_anything(root, monkeypatch):
    _make_trees(root, 2)
    monkeypatch.setattr(worktree_cleanup, "prune", lambda *a, **k: None)
    monkeypatch.setattr(
        worktree,
        "_worktree_lifecycle_service",
        lambda *a, **k: pytest.fail("must refuse before provisioning"),
    )
    with pytest.raises(typer.Exit):
        worktree._do_add(
            {"worktrees": {"max_live": 2}},
            {"prefix": "x"},
            root,
            "wt/b",
            root / "new",
            new_branch=True,
        )


# ---- reclaim after merge --------------------------------------------------------------------


def _wire(monkeypatch, tmp_path, target: Path, status_out: str, rc: int = 0):
    home = tmp_path / "bh-home"  # an empty, isolated config
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text("schema_version: 1\nmanaged_repos: []\n")
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("BH_CONFIG", str(home / "config.yaml"))
    monkeypatch.setattr(worktree_cleanup, "_resolve_entry", lambda cfg, hive: {"prefix": "x"})
    monkeypatch.setattr(worktree_cleanup, "wt_dir", lambda entry, leaf: target)
    monkeypatch.setattr(worktree_cleanup, "_leaf", lambda ref: ref)
    monkeypatch.setattr(
        worktree_cleanup,
        "_run_git",
        lambda *a, **k: SimpleNamespace(returncode=rc, stdout=status_out),
    )
    removed = []
    monkeypatch.setattr(worktree_cleanup, "remove", lambda *a, **k: removed.append(a))
    return removed


def test_reclaim_merged_removes_a_clean_tree(tmp_path, monkeypatch):
    target = tmp_path / "bh-1"
    target.mkdir()
    removed = _wire(monkeypatch, tmp_path, target, "")
    assert worktree.reclaim_merged("x", "bh-1") is True
    assert removed == [("x", "bh-1", True)]


def test_reclaim_merged_never_removes_uncommitted_work(tmp_path, monkeypatch, capsys):
    target = tmp_path / "bh-1"
    target.mkdir()
    removed = _wire(monkeypatch, tmp_path, target, " M src/a.py\n?? notes.md\n")
    assert worktree.reclaim_merged("x", "bh-1") is False
    assert removed == []
    assert "uncommitted changes" in capsys.readouterr().err


def test_reclaim_merged_keeps_a_tree_whose_status_is_unreadable(tmp_path, monkeypatch):
    target = tmp_path / "bh-1"
    target.mkdir()
    removed = _wire(monkeypatch, tmp_path, target, "", rc=128)
    assert worktree.reclaim_merged("x", "bh-1") is False
    assert removed == []


def test_reclaim_merged_on_an_absent_tree_is_idempotent(tmp_path, monkeypatch):
    removed = _wire(monkeypatch, tmp_path, tmp_path / "gone", "")
    assert worktree.reclaim_merged("x", "bh-1") is True
    assert removed == []
