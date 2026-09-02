from __future__ import annotations

import pytest

from beadhive.modules.worktrees import (
    WorktreeBinding,
    WorktreeBranchPolicy,
    bind_worktree,
    branch_suffix,
    leaf_for_branch,
)


def test_branch_policy_preserves_bead_raw_and_session_shapes() -> None:
    policy = WorktreeBranchPolicy()

    assert branch_suffix(policy, bead="bh-123", kind="epic") == "bead/epic/bh-123"
    assert branch_suffix(policy, branch="wt/spike/one") == "wt/spike/one"
    assert (
        branch_suffix(policy, timestamp="20260902T060000Z", random_token="abcd")
        == "session/20260902T060000Z-abcd"
    )


def test_binding_applies_prefix_once_and_keeps_batch_leaf_namespace() -> None:
    assert bind_worktree("feature/login") == WorktreeBinding("wt/feature/login", "login")
    assert bind_worktree("wt/feature/login") == WorktreeBinding("wt/feature/login", "login")
    assert bind_worktree("batch/My_Group") == WorktreeBinding("wt/batch/My_Group", "batch-my-group")
    assert leaf_for_branch("wt/bead/issue/BH.ONE") == "bh-one"


def test_session_binding_fails_closed_without_explicit_entropy() -> None:
    with pytest.raises(ValueError, match="timestamp and random token"):
        branch_suffix(WorktreeBranchPolicy())
