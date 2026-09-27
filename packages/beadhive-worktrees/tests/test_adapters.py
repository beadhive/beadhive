from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive_worktrees import (
    NATIVE_CAPABILITIES,
    NativeGitBranchInspector,
    NativeGitWorktreeManager,
    WorktreeHandle,
    WorktreeManagerCapabilities,
    WorktreeManagerError,
    WorktreeRemoved,
    WorktreeSpec,
)


def test_native_manager_builds_new_attach_and_remove_commands(tmp_path) -> None:
    calls = []

    def run_git(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    manager = NativeGitWorktreeManager(run_git)
    main = tmp_path / "main"
    target = tmp_path / "nested" / "seat"
    spec = WorktreeSpec(main, "wt/bead/issue/x", target, "main")

    handle = manager.create(spec)
    removed = manager.remove(handle, True)

    assert handle == WorktreeHandle("seat", main, target, "wt/bead/issue/x", "main")
    assert removed == WorktreeRemoved(handle)
    assert calls == [
        (
            [
                "git",
                "-C",
                str(main),
                "worktree",
                "add",
                "-b",
                "wt/bead/issue/x",
                str(target),
                "main",
            ],
            {"check": False},
        ),
        (
            ["git", "-C", str(main), "worktree", "remove", str(target), "--force"],
            {"check": False},
        ),
    ]


def test_native_manager_preserves_attach_prune_order_and_failure_code(tmp_path) -> None:
    calls = []

    def run_git(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=19 if "add" in command else 0)

    manager = NativeGitWorktreeManager(run_git)
    spec = WorktreeSpec(tmp_path / "main", "wt/existing", tmp_path / "seat")

    with pytest.raises(WorktreeManagerError) as failed:
        manager.attach(spec)

    assert failed.value.returncode == 19
    assert failed.value.path == spec.path
    assert calls[0][-2:] == ["worktree", "prune"]
    assert calls[1][-2:] == [str(spec.path), spec.branch]


def test_native_attach_never_forwards_base_to_git(tmp_path) -> None:
    """E37: an attach records ``base`` as intent only and never hands it to git, so the existing
    branch tip cannot be moved by the manager even when that base has diverged."""
    calls = []

    def run_git(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    manager = NativeGitWorktreeManager(run_git)
    main = tmp_path / "main"
    spec = WorktreeSpec(main, "wt/bead/issue/b-2", tmp_path / "b-2", "main")

    handle = manager.attach(spec)

    assert calls == [
        ["git", "-C", str(main), "worktree", "prune"],
        ["git", "-C", str(main), "worktree", "add", str(spec.path), spec.branch],
    ]
    assert handle.base == "main"
    assert handle.bindings == {}


def test_native_remove_without_force_keeps_the_dirty_refusal(tmp_path) -> None:
    calls = []

    def run_git(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    handle = WorktreeHandle.for_removal(tmp_path / "main", tmp_path / "seat")
    NativeGitWorktreeManager(run_git).remove(handle, False)

    assert calls == [["git", "-C", str(tmp_path / "main"), "worktree", "remove", str(handle.path)]]


def test_native_manager_declares_it_binds_nothing_and_releases_nothing() -> None:
    manager = NativeGitWorktreeManager(lambda *_a, **_k: None)

    assert manager.binds == ()
    assert manager.remove_releases_bindings is False
    assert WorktreeManagerCapabilities.of(manager) == NATIVE_CAPABILITIES
    assert NATIVE_CAPABILITIES.composes_binding("herdr") is True
    assert WorktreeManagerCapabilities(binds=("herdr",)).composes_binding("herdr") is False


def test_branch_inspector_reads_tip_and_ancestry(tmp_path) -> None:
    calls = []

    def run_git(command, **kwargs):
        calls.append((command, kwargs))
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout="abc123\n")
        return SimpleNamespace(returncode=1, stdout="")

    inspector = NativeGitBranchInspector(run_git)
    main = tmp_path / "main"

    assert inspector.tip(main, "wt/x") == "abc123"
    assert inspector.contains(main, "wt/x", "main") is False
    assert calls[1][0] == [
        "git",
        "-C",
        str(main),
        "merge-base",
        "--is-ancestor",
        "main",
        "refs/heads/wt/x",
    ]


def test_handle_records_bindings_without_mutating_the_original(tmp_path) -> None:
    spec = WorktreeSpec(tmp_path / "main", "wt/bead/issue/b-1", tmp_path / "b-1", identity="b-1")
    handle = WorktreeHandle.of(spec)

    bound = handle.with_binding("herdr", "w4")

    assert handle.bindings == {}
    assert bound.bindings == {"herdr": "w4"}
    assert (bound.identity, bound.path, bound.branch) == ("b-1", spec.path, spec.branch)
