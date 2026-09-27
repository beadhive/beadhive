"""Provider- and transport-neutral managed-worktree capability (bh-xh8ku.3, bh-055ot.1).

Depends only on ``beadhive-plugins`` — for the capability-slot machinery and the
``worktree.manager`` / ``workspace.binding`` slot declarations (``beadhive_plugins.worktree_slots``)
this package implements, not for anything else. Importing it describes naming policy, the
:class:`WorktreeSpec` / :class:`WorktreeHandle` contract, the capability flags, ports, and the
native Git manager; it never selects or binds a provider itself — that is root composition's
job, exclusively, via ``bind_application_port`` (docs/design/package-class-library-vs-plugin-adr.md
section 5, docs/design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md).

Root keeps a forwarding facade at the old ``beadhive.modules.worktrees`` import path so existing
consumers keep working unchanged — every name here resolves identically through that facade
(``docs/MODULES.md`` principle 8: compatibility facades are deliberate migration tools).
"""

from __future__ import annotations

from .adapters import (
    NATIVE_PROVIDER_ID,
    CallbackWorktreeInventory,
    NativeGitBranchInspector,
    NativeGitWorktreeManager,
    native_worktree_manager_provider_binding,
)
from .application import WorktreeInventoryService, WorktreeLifecycleService
from .contracts import (
    WORKSPACE_BINDING,
    WORKTREE_MANAGER,
    WORKTREE_MANAGER_KEY,
    BranchInspector,
    WorkspaceBindingPort,
    WorktreeCreateObserver,
    WorktreeInventory,
    WorktreeManagerPort,
)
from .domain import (
    BATCH_BRANCH_PREFIX,
    BATCH_LEAF_PREFIX,
    NATIVE_CAPABILITIES,
    WT_PREFIX,
    ManagedWorktree,
    WorktreeBinding,
    WorktreeBranchPolicy,
    WorktreeHandle,
    WorktreeInventoryRequest,
    WorktreeInventoryResult,
    WorktreeManagerCapabilities,
    WorktreeManagerError,
    WorktreeRemoved,
    WorktreeSpec,
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
    "NATIVE_CAPABILITIES",
    "NATIVE_PROVIDER_ID",
    "WORKSPACE_BINDING",
    "WORKTREE_MANAGER",
    "WORKTREE_MANAGER_KEY",
    "WT_PREFIX",
    "BranchInspector",
    "CallbackWorktreeInventory",
    "ManagedWorktree",
    "NativeGitBranchInspector",
    "NativeGitWorktreeManager",
    "WorkspaceBindingPort",
    "WorktreeBinding",
    "WorktreeBranchPolicy",
    "WorktreeCreateObserver",
    "WorktreeHandle",
    "WorktreeInventory",
    "WorktreeInventoryRequest",
    "WorktreeInventoryResult",
    "WorktreeInventoryService",
    "WorktreeLifecycleService",
    "WorktreeManagerCapabilities",
    "WorktreeManagerError",
    "WorktreeManagerPort",
    "WorktreeRemoved",
    "WorktreeSpec",
    "WorktreeStatusRequest",
    "WorktreeStatusResult",
    "apply_prefix",
    "bind_worktree",
    "branch_suffix",
    "leaf_for_branch",
    "native_worktree_manager_provider_binding",
    "sanitize_leaf",
]
