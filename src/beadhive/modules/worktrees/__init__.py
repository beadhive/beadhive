"""Forwarding facade — moved to the ``beadhive-worktrees`` library package (bh-xh8ku.3).

The provider- and transport-neutral managed-worktree capability (naming policy, typed
request/result contracts, ports, the ``worktree.manager`` capability declaration, and the
native Git adapter) now lives in :mod:`beadhive_worktrees`, a package that depends only on
``beadhive-plugins``. This module re-exports the identical objects at the old import path so
existing consumers keep working unchanged; migrating them onto the new package directly is not
required by this move.
"""

from __future__ import annotations

from beadhive_worktrees import (
    BATCH_BRANCH_PREFIX,
    BATCH_LEAF_PREFIX,
    WT_PREFIX,
    CallbackWorktreeInventory,
    CreateWorktreeRequest,
    ManagedWorktree,
    NativeGitWorktreeProvisioner,
    PluginWorktreeProvisioner,
    ProvisioningResult,
    RemoveWorktreeRequest,
    WorktreeBinding,
    WorktreeBranchPolicy,
    WorktreeInventory,
    WorktreeInventoryRequest,
    WorktreeInventoryResult,
    WorktreeInventoryService,
    WorktreeLifecycleService,
    WorktreeProvisioner,
    WorktreeStatusRequest,
    WorktreeStatusResult,
    apply_prefix,
    bind_worktree,
    branch_suffix,
    leaf_for_branch,
    sanitize_leaf,
)

__all__ = [
    "BATCH_BRANCH_PREFIX",
    "BATCH_LEAF_PREFIX",
    "WT_PREFIX",
    "CallbackWorktreeInventory",
    "CreateWorktreeRequest",
    "ManagedWorktree",
    "NativeGitWorktreeProvisioner",
    "PluginWorktreeProvisioner",
    "ProvisioningResult",
    "RemoveWorktreeRequest",
    "WorktreeBinding",
    "WorktreeBranchPolicy",
    "WorktreeInventory",
    "WorktreeInventoryRequest",
    "WorktreeInventoryResult",
    "WorktreeInventoryService",
    "WorktreeLifecycleService",
    "WorktreeProvisioner",
    "WorktreeStatusRequest",
    "WorktreeStatusResult",
    "apply_prefix",
    "bind_worktree",
    "branch_suffix",
    "leaf_for_branch",
    "sanitize_leaf",
]
