"""Provider- and transport-neutral managed-worktree capability (bh-xh8ku.3).

Depends only on ``beadhive-plugins`` — for the ``CapabilityRef`` / ``CapabilityKey`` /
``bind_application_port`` capability-slot machinery this package uses to declare its
``worktree.manager`` slot (:data:`WORKTREE_MANAGER`), not for anything else. Importing it
describes naming policy, typed request/result contracts, ports, and the native Git adapter; it
never selects or binds a provider itself — that is root composition's job, exclusively, via
``bind_application_port`` (docs/design/package-class-library-vs-plugin-adr.md section 5,
docs/design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md).

Root keeps a forwarding facade at the old ``beadhive.modules.worktrees`` import path so existing
consumers keep working unchanged — every name here resolves identically through that facade
(``docs/MODULES.md`` principle 8: compatibility facades are deliberate migration tools).
"""

from __future__ import annotations

from .adapters import (
    NATIVE_PROVIDER_ID,
    CallbackWorktreeInventory,
    NativeGitWorktreeProvisioner,
    PluginWorktreeProvisioner,
    native_worktree_manager_provider_binding,
)
from .application import WorktreeInventoryService, WorktreeLifecycleService
from .contracts import (
    WORKTREE_MANAGER,
    WORKTREE_MANAGER_KEY,
    WorktreeInventory,
    WorktreeProvisioner,
)
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
    "NATIVE_PROVIDER_ID",
    "WORKTREE_MANAGER",
    "WORKTREE_MANAGER_KEY",
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
    "native_worktree_manager_provider_binding",
    "sanitize_leaf",
]
