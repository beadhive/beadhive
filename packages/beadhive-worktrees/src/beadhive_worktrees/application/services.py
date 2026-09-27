"""Worktree lifecycle sequencing over replaceable provisioner adapters."""

from __future__ import annotations

from typing import Generic, TypeVar

from ..contracts import WorktreeInventory, WorktreeProvisioner
from ..domain import (
    CreateWorktreeRequest,
    ProvisioningResult,
    RemoveWorktreeRequest,
    WorktreeInventoryRequest,
    WorktreeInventoryResult,
    WorktreeStatusRequest,
    WorktreeStatusResult,
)

StatusRowT = TypeVar("StatusRowT")


class WorktreeLifecycleService:
    """Preserve plugin-first fallback while keeping native effects available."""

    def __init__(
        self,
        *,
        native: WorktreeProvisioner,
        plugin: WorktreeProvisioner,
    ) -> None:
        self._native = native
        self._plugin = plugin

    def create(self, request: CreateWorktreeRequest) -> ProvisioningResult:
        # Native preparation owns the target parent. Observers have historically run only after
        # that parent exists, before either delegated or native creation.
        self._native.prepare(request)
        self._plugin.prepare(request)
        result = self._plugin.create(request)
        if not result.handled:
            result = self._native.create(request)
        if result.succeeded:
            self._plugin.created(request, result)
            self._native.created(request, result)
        return result

    def remove(self, request: RemoveWorktreeRequest) -> ProvisioningResult:
        result = self._plugin.remove(request)
        if not result.handled:
            result = self._native.remove(request)
        if result.succeeded:
            self._plugin.removed(request, result)
            self._native.removed(request, result)
        return result


class WorktreeInventoryService(Generic[StatusRowT]):
    """Typed query boundary; adapters retain classifier and Git I/O ownership."""

    def __init__(self, inventory: WorktreeInventory[StatusRowT]) -> None:
        self._inventory = inventory

    def inventory(self, request: WorktreeInventoryRequest | None = None) -> WorktreeInventoryResult:
        request = request or WorktreeInventoryRequest()
        return WorktreeInventoryResult(self._inventory.inventory(request))

    def status(
        self, request: WorktreeStatusRequest | None = None
    ) -> WorktreeStatusResult[StatusRowT]:
        request = request or WorktreeStatusRequest()
        return WorktreeStatusResult(self._inventory.status(request))
