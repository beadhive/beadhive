from __future__ import annotations

import pytest

from beadhive_worktrees import (
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


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        # Default kind is the leaf 'issue'; <type> lives in the branch, the leaf stays <id>.
        ({"bead": "ag-infra-7"}, ("wt/bead/issue/ag-infra-7", "ag-infra-7")),
        # An explicit epic kind opens the container namespace; the leaf is unchanged.
        ({"bead": "ag-epic", "kind": "epic"}, ("wt/bead/epic/ag-epic", "ag-epic")),
        # A raw branch is prefixed, never overridden, and never double-prefixed.
        ({"branch": "spike-xyz"}, ("wt/spike-xyz", "spike-xyz")),
        ({"branch": "feature/login"}, ("wt/feature/login", "login")),
        ({"branch": "wt/foo"}, ("wt/foo", "foo")),
        # A work-group's leaf carries `batch-` so it never collides with a same-named bead seat.
        ({"branch": "batch/samefile"}, ("wt/batch/samefile", "batch-samefile")),
    ],
)
def test_default_policy_names_bead_raw_and_batch_worktrees(suffix, expected) -> None:
    """Re-homed from root tests/test_worktree.py's `_branch_and_leaf` table (bh-qdezo.9)."""
    binding = bind_worktree(branch_suffix(WorktreeBranchPolicy(), **suffix))

    assert (binding.branch, binding.leaf) == expected
