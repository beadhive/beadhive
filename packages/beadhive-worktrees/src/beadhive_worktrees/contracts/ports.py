"""Outbound ports for worktree creation and removal effects."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from beadhive_plugins.contracts import CapabilityKey, CapabilityRef

from ..domain import (
    CreateWorktreeRequest,
    ManagedWorktree,
    ProvisioningResult,
    RemoveWorktreeRequest,
    WorktreeInventoryRequest,
    WorktreeStatusRequest,
)

StatusRowT_co = TypeVar("StatusRowT_co", covariant=True)

#: Exactly one configured manager per hive
#: (docs/design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md). Native Git is the built-in
#: default provider; root composition binds the selected provider to this slot with
#: ``beadhive_plugins.binding.bind_application_port`` and injects the resulting port — this
#: package never selects or binds its own provider.
WORKTREE_MANAGER = CapabilityRef("worktree.manager", 1)


@runtime_checkable
class WorktreeProvisioner(Protocol):
    """One adapter participating in the five lifecycle phases."""

    def prepare(self, request: CreateWorktreeRequest) -> None: ...

    def create(self, request: CreateWorktreeRequest) -> ProvisioningResult: ...

    def created(self, request: CreateWorktreeRequest, result: ProvisioningResult) -> None: ...

    def remove(self, request: RemoveWorktreeRequest) -> ProvisioningResult: ...

    def removed(self, request: RemoveWorktreeRequest, result: ProvisioningResult) -> None: ...


#: Typed application-port request for :data:`WORKTREE_MANAGER`, paired at composition time.
WORKTREE_MANAGER_KEY = CapabilityKey(WORKTREE_MANAGER, WorktreeProvisioner)


class WorktreeInventory(Protocol[StatusRowT_co]):
    """Read linked-worktree identity and adopted status records."""

    def inventory(self, request: WorktreeInventoryRequest) -> tuple[ManagedWorktree, ...]: ...

    def status(self, request: WorktreeStatusRequest) -> tuple[StatusRowT_co, ...]: ...
