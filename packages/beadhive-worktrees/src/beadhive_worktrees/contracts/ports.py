"""Outbound ports for worktree mechanics, observation, and inventory.

The ``worktree.manager`` and ``workspace.binding`` slots themselves are declared on
``beadhive-plugins`` (``beadhive_plugins.worktree_slots``, bh-055ot.1); this module binds their
generic method shapes to this package's concrete :class:`WorktreeSpec` / :class:`WorktreeHandle`
/ :class:`WorktreeRemoved` and re-exports the slot identities for existing importers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypeAlias, TypeVar

from beadhive_plugins.contracts import CapabilityKey
from beadhive_plugins.worktree_slots import (
    WORKSPACE_BINDING,
    WORKTREE_MANAGER,
    WorkspaceBinding,
    WorktreeManager,
)

from ..domain import (
    ManagedWorktree,
    WorktreeHandle,
    WorktreeInventoryRequest,
    WorktreeRemoved,
    WorktreeSpec,
    WorktreeStatusRequest,
)

StatusRowT_co = TypeVar("StatusRowT_co", covariant=True)

#: The ``worktree.manager`` port specialised to this package's spec/handle/receipt types.
WorktreeManagerPort: TypeAlias = WorktreeManager[WorktreeSpec, WorktreeHandle, WorktreeRemoved]

#: The ``workspace.binding`` port specialised to this package's handle type.
WorkspaceBindingPort: TypeAlias = WorkspaceBinding[WorktreeHandle]

#: Typed application-port request for :data:`WORKTREE_MANAGER`, paired at composition time.
#: Exactly one configured manager per hive; root composition binds the selected provider with
#: ``beadhive_plugins.binding.bind_application_port`` — this package never selects or binds one.
WORKTREE_MANAGER_KEY = CapabilityKey(WORKTREE_MANAGER, WorktreeManager)


class WorktreeCreateObserver(Protocol):
    """Cross-cutting create notifications, decoupled from the manager/binding contract.

    ``creating`` fires once the target's parent exists and before the manager runs; ``created``
    fires only after the manager succeeded. Observation never owns mechanics.
    """

    def creating(self, spec: WorktreeSpec) -> None: ...

    def created(self, handle: WorktreeHandle) -> None: ...


class BranchInspector(Protocol):
    """Read-only branch facts for Beadhive's own attach pre-check (E37)."""

    def tip(self, main: Path, branch: str) -> str: ...

    def contains(self, main: Path, branch: str, base: str) -> bool: ...


class WorktreeInventory(Protocol[StatusRowT_co]):
    """Read linked-worktree identity and adopted status records."""

    def inventory(self, request: WorktreeInventoryRequest) -> tuple[ManagedWorktree, ...]: ...

    def status(self, request: WorktreeStatusRequest) -> tuple[StatusRowT_co, ...]: ...


__all__ = [
    "WORKSPACE_BINDING",
    "WORKTREE_MANAGER",
    "WORKTREE_MANAGER_KEY",
    "BranchInspector",
    "WorkspaceBindingPort",
    "WorktreeCreateObserver",
    "WorktreeInventory",
    "WorktreeManagerPort",
]
