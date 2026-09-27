"""Worktree lifecycle sequencing over exactly one selected ``worktree.manager``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from ..contracts import BranchInspector, WorktreeCreateObserver, WorktreeInventory
from ..contracts.ports import WorktreeManagerPort
from ..domain import (
    WorktreeHandle,
    WorktreeInventoryRequest,
    WorktreeInventoryResult,
    WorktreeManagerError,
    WorktreeRemoved,
    WorktreeSpec,
    WorktreeStatusRequest,
    WorktreeStatusResult,
)

StatusRowT = TypeVar("StatusRowT")


class _NoObserver:
    def creating(self, spec: WorktreeSpec) -> None:
        del spec

    def created(self, handle: WorktreeHandle) -> None:
        del handle


def _ignore_warning(_message: str) -> None:
    return None


class WorktreeLifecycleService:
    """Sequence create/attach/remove through the ONE selected manager (bh-055ot.1).

    There is no plugin-first fallback and no registry-order race: the manager handed in at
    composition is the only thing that ever executes worktree mechanics. Create observers are a
    separate, cross-cutting list that never owns mechanics. Beadhive — not the manager — guards
    attach's start-point intent (E37): when the spec names a ``base``, the branch tip is read
    before and after the manager runs, and a moved tip is refused.
    """

    def __init__(
        self,
        *,
        manager: WorktreeManagerPort,
        observer: WorktreeCreateObserver | None = None,
        branches: BranchInspector | None = None,
        warn: Callable[[str], None] = _ignore_warning,
    ) -> None:
        self._manager = manager
        self._observer = observer or _NoObserver()
        self._branches = branches
        self._warn = warn

    @property
    def manager(self) -> WorktreeManagerPort:
        return self._manager

    def create(self, spec: WorktreeSpec) -> WorktreeHandle:
        return self._provision(spec, self._manager.create)

    def attach(self, spec: WorktreeSpec) -> WorktreeHandle:
        if not spec.base or self._branches is None:
            return self._provision(spec, self._manager.attach)
        before = self._branches.tip(spec.main, spec.branch)
        if before and not self._branches.contains(spec.main, spec.branch, spec.base):
            self._warn(
                f"attach keeps {spec.branch} at {before[:12]}: base {spec.base} has diverged "
                "and is recorded as intent only (attach never moves a branch tip)"
            )
        handle = self._provision(spec, self._manager.attach)
        after = self._branches.tip(spec.main, spec.branch)
        if after != before:
            raise WorktreeManagerError(
                spec.path,
                1,
                f"worktree manager moved {spec.branch} from {before or '?'} to {after or '?'} "
                "on attach — attach must never move an existing branch tip",
            )
        return handle

    def remove(self, handle: WorktreeHandle, *, force: bool = False) -> WorktreeRemoved:
        return self._manager.remove(handle, force)

    def _provision(
        self, spec: WorktreeSpec, execute: Callable[[WorktreeSpec], WorktreeHandle]
    ) -> WorktreeHandle:
        # Beadhive computed the exact path, so it owns the parent directory. Observers have
        # historically run only after that parent exists, before the manager executes.
        spec.path.parent.mkdir(parents=True, exist_ok=True)
        self._observer.creating(spec)
        handle = execute(spec)
        self._observer.created(handle)
        return handle


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
