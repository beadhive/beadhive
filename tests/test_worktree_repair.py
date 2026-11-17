"""A worktree gutted by $TMPDIR purging is repaired only when provably safe (bh-uwlsw)."""

from __future__ import annotations

import subprocess

import pytest

from beadhive import worktree_repair

BR = "wt/bead/issue/x-1"


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def wt(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "a.txt").write_text("a\n")
    (main / "sub").mkdir()
    (main / "sub" / "b.txt").write_text("b\n")
    _git(main, "add", ".")
    _git(main, "commit", "-qm", "init")
    target = tmp_path / "wts" / "x-1"
    _git(main, "worktree", "add", "-q", "-b", BR, str(target))
    return main, target


def _gut(target, *names):
    (target / ".git").unlink()
    for name in names:
        (target / name).unlink()


def _list(main):
    return subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=main, capture_output=True, text=True
    ).stdout


def test_healthy_worktree_is_left_alone(wt):
    main, target = wt
    assert worktree_repair.repair_gutted(main, target, BR).state == "healthy"


def test_repairs_missing_git_file_and_tracked_files(wt):
    main, target = wt
    _gut(target, "a.txt", "sub/b.txt")
    assert "prunable" in _list(main)
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "repaired"
    assert (target / "a.txt").read_text() == "a\n"
    assert (target / "sub" / "b.txt").read_text() == "b\n"
    assert "prunable" not in _list(main)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True
    )
    assert status.stdout == ""


def test_repair_keeps_untracked_files(wt):
    main, target = wt
    (target / "notes.md").write_text("mine\n")
    _gut(target, "a.txt")
    assert worktree_repair.repair_gutted(main, target, BR).state == "repaired"
    assert (target / "notes.md").read_text() == "mine\n"


def test_refuses_modified_tracked_file(wt):
    main, target = wt
    (target / "a.txt").write_text("edited\n")
    _gut(target, "sub/b.txt")
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "a.txt" in result.reason
    assert (target / "a.txt").read_text() == "edited\n"
    assert not (target / ".git").exists()  # untouched on refusal
    assert not (target / "sub" / "b.txt").exists()


def test_refuses_staged_changes(wt):
    main, target = wt
    (target / "c.txt").write_text("new\n")
    _git(target, "add", "c.txt")
    _gut(target)
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "staged" in result.reason
    assert not (target / ".git").exists()


def test_refuses_unregistered_directory(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "f").write_text("x")
    result = worktree_repair.repair_gutted(main, stray, BR)
    assert result.state == "refused" and "not registered" in result.reason
    assert not (stray / ".git").exists()


def test_refuses_wrong_branch_and_in_progress(wt):
    main, target = wt
    _gut(target)
    assert worktree_repair.repair_gutted(main, target, "wt/bead/issue/other").state == "refused"
    admin = next((main / ".git" / "worktrees").iterdir())
    (admin / "MERGE_HEAD").write_text("0" * 40 + "\n")
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "in progress" in result.reason


def test_rebuilds_a_missing_index(wt):
    main, target = wt
    admin = next((main / ".git" / "worktrees").iterdir())
    _gut(target, "a.txt")
    (admin / "index").unlink()
    assert worktree_repair.repair_gutted(main, target, BR).state == "repaired"
    assert (target / "a.txt").exists()
    assert (admin / "index").exists()


def _status(target):
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True
    ).stdout


def test_real_index_present_restores_several_missing_files(wt):
    main, target = wt
    for n in ("c.txt", "d.txt", "e.txt"):
        (main / n).write_text(n)
    _git(main, "add", ".")
    _git(main, "commit", "-qm", "more")
    _git(target, "merge", "-q", "main")
    admin = next((main / ".git" / "worktrees").iterdir())
    index_before = (admin / "index").read_bytes()
    _gut(target, "a.txt", "c.txt", "d.txt", "sub/b.txt")
    assert worktree_repair.repair_gutted(main, target, BR).state == "repaired"
    assert (admin / "index").read_bytes() == index_before  # the real index was used, not rebuilt
    for n in ("a.txt", "c.txt", "d.txt", "e.txt", "sub/b.txt"):
        assert (target / n).exists()
    assert _status(target) == ""


def test_partial_purge_leaves_survivors_and_untracked_untouched(wt):
    main, target = wt
    (target / "scratch.log").write_text("keep me\n")
    survivor = (target / "a.txt").read_bytes()
    _gut(target, "sub/b.txt")
    assert worktree_repair.repair_gutted(main, target, BR).state == "repaired"
    assert (target / "a.txt").read_bytes() == survivor
    assert (target / "scratch.log").read_text() == "keep me\n"
    assert _status(target) == "?? scratch.log\n"


def test_refuses_sparse_checkout(wt):
    main, target = wt
    _git(target, "sparse-checkout", "set", "sub")
    _gut(target)
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "sparse" in result.reason
    assert not (target / ".git").exists()


def test_refuses_sparse_checkout_with_missing_index(wt):
    main, target = wt
    _git(target, "sparse-checkout", "set", "sub")
    admin = next((main / ".git" / "worktrees").iterdir())
    _gut(target)
    (admin / "index").unlink()
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "sparse" in result.reason
    assert not (admin / "index").exists() and not (target / ".git").exists()


def test_refuses_submodules(wt):
    main, target = wt
    (target / ".gitmodules").write_text('[submodule "m"]\n\tpath = m\n\turl = ./m\n')
    _git(target, "add", ".gitmodules")
    _git(target, "commit", "-qm", "submodule")
    _gut(target)
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "submodule" in result.reason
    assert not (target / ".git").exists()


def test_refuses_gitlink_entries(wt):
    main, target = wt
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True
    ).stdout.strip()
    _git(target, "update-index", "--add", "--cacheinfo", f"160000,{head},m")
    _git(target, "commit", "-qm", "gitlink")
    _gut(target)
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "submodule" in result.reason


def test_refuses_file_mode_noise(wt):
    main, target = wt
    (target / "a.txt").chmod(0o755)
    _gut(target, "sub/b.txt")
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "a.txt" in result.reason
    assert not (target / ".git").exists() and not (target / "sub" / "b.txt").exists()


def test_refuses_when_branch_moved(wt):
    main, target = wt
    _git(target, "checkout", "-q", "-b", "wt/bead/issue/moved")
    _gut(target)
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and f"not {BR}" in result.reason
    assert not (target / ".git").exists()


def test_refuses_unstaged_deletion_beside_a_modified_file(wt):
    main, target = wt
    (target / "a.txt").write_text("edited\n")
    _gut(target, "sub/b.txt")  # a deliberate-looking deletion next to real edits
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "a.txt" in result.reason
    assert (target / "a.txt").read_text() == "edited\n"
    assert not (target / "sub" / "b.txt").exists()


def test_known_limitation_lone_deletion_is_restored(wt):
    main, target = wt
    _gut(target, "a.txt")  # indistinguishable from a purge: documented, restored
    assert worktree_repair.repair_gutted(main, target, BR).state == "repaired"
    assert (target / "a.txt").exists()


@pytest.mark.parametrize("index_missing", [False, True])
def test_checkout_index_failure_rolls_back_to_clean_gutted_state(wt, monkeypatch, index_missing):
    main, target = wt
    admin = next((main / ".git" / "worktrees").iterdir())
    _gut(target, "a.txt")
    if index_missing:
        (admin / "index").unlink()
    index_before = None if index_missing else (admin / "index").read_bytes()
    real = worktree_repair._git

    def fake(args, env_extra, cwd=None):
        if args[0] == "checkout-index":
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(args, env_extra, cwd)

    monkeypatch.setattr(worktree_repair, "_git", fake)
    result = worktree_repair.repair_gutted(main, target, BR)
    assert result.state == "refused" and "rolled back" in result.reason and "boom" in result.reason
    assert not (target / ".git").exists()
    assert not (target / "a.txt").exists()
    if index_missing:
        assert not (admin / "index").exists()
    else:
        assert (admin / "index").read_bytes() == index_before
    monkeypatch.setattr(worktree_repair, "_git", real)  # the state is repairable afterwards
    assert worktree_repair.repair_gutted(main, target, BR).state == "repaired"
