"""Outbound ports for worktree creation and removal effects."""

from __future__ import annotations

from typing import Protocol, TypeVar

from ..domain import (
    CreateWorktreeRequest,
    ManagedWorktree,
    ProvisioningResult,
    RemoveWorktreeRequest,
    WorktreeInventoryRequest,
    WorktreeStatusRequest,
)

StatusRowT_co = TypeVar("StatusRowT_co", covariant=True)


class WorktreeProvisioner(Protocol):
    """One adapter participating in the five lifecycle phases."""

    def prepare(self, request: CreateWorktreeRequest) -> None: ...

    def create(self, request: CreateWorktreeRequest) -> ProvisioningResult: ...

    def created(self, request: CreateWorktreeRequest, result: ProvisioningResult) -> None: ...

    def remove(self, request: RemoveWorktreeRequest) -> ProvisioningResult: ...

    def removed(self, request: RemoveWorktreeRequest, result: ProvisioningResult) -> None: ...


class WorktreeInventory(Protocol[StatusRowT_co]):
    """Read linked-worktree identity and adopted status records."""

    def inventory(self, request: WorktreeInventoryRequest) -> tuple[ManagedWorktree, ...]: ...

    def status(self, request: WorktreeStatusRequest) -> tuple[StatusRowT_co, ...]: ...
