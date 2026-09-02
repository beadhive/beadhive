"""Provider- and transport-neutral managed-worktree capability."""

from .adapters import (
    CallbackWorktreeInventory,
    NativeGitWorktreeProvisioner,
    PluginWorktreeProvisioner,
)
from .application import WorktreeInventoryService, WorktreeLifecycleService
from .contracts import WorktreeInventory, WorktreeProvisioner
from .domain import (
    BATCH_BRANCH_PREFIX,
    BATCH_LEAF_PREFIX,
    WT_PREFIX,
    CreateWorktreeRequest,
    ManagedWorktree,
    ProvisioningResult,
    RemoveWorktreeRequest,
    WorktreeBinding,
    WorktreeBranchPolicy,
    WorktreeInventoryRequest,
    WorktreeInventoryResult,
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
