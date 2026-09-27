"""Worktree lifecycle sequencing over exactly one selected ``worktree.manager``."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Generic, TypeVar

from ..contracts import BranchInspector, WorktreeCreateObserver, WorktreeInventory
from ..contracts.ports import WorkspaceBindingPort, WorktreeManagerPort
from ..domain import (
    BindingGap,
    BoundWorktree,
    WorkspaceBindingError,
    WorktreeHandle,
    WorktreeInventoryRequest,
    WorktreeInventoryResult,
    WorktreeManagerCapabilities,
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

    Workspace bindings (bh-cb4jo) are composed by the ADR rule: a candidate binding is kept only
    when the manager's ``binds`` does not already name its presenter, so a manager that binds a
    presenter itself never gets a second, redundant binding. ``remove`` always releases every
    composed binding *before* the manager removes the worktree (E30) — a remove without a prior
    release is not expressible through this service. A binding failure is a presentation gap:
    it is reported through ``warn`` and never rolls back or blocks the native mechanics (E33).
    """

    def __init__(
        self,
        *,
        manager: WorktreeManagerPort,
        observer: WorktreeCreateObserver | None = None,
        branches: BranchInspector | None = None,
        bindings: Iterable[WorkspaceBindingPort] = (),
        warn: Callable[[str], None] = _ignore_warning,
    ) -> None:
        self._manager = manager
        self._observer = observer or _NoObserver()
        self._branches = branches
        self._warn = warn
        capabilities = WorktreeManagerCapabilities.of(manager)
        self._bindings = tuple(
            binding for binding in bindings if capabilities.composes_binding(binding.presenter)
        )

    @property
    def manager(self) -> WorktreeManagerPort:
        return self._manager

    @property
    def bindings(self) -> tuple[WorkspaceBindingPort, ...]:
        """The bindings composed for this manager (candidates it already binds are dropped)."""
        return self._bindings

    def binding(self, presenter: str) -> WorkspaceBindingPort | None:
        """The composed binding for ``presenter``, or ``None`` when none was composed."""
        return next((item for item in self._bindings if item.presenter == presenter), None)

    def bind(self, handle: WorktreeHandle) -> BoundWorktree:
        """Bind every composed presenter to an already created/attached worktree (E28, E29).

        Each binding is idempotent (E34), so re-running ``bind`` is the one repair route for a
        missing binding. A failed bind leaves the worktree exactly as it is and returns a gap.
        """
        gaps: list[BindingGap] = []
        for binding in self._bindings:
            try:
                handle = binding.bind(handle)
            except WorkspaceBindingError as exc:
                gaps.append(self._gap(binding.presenter, "bind", handle, exc))
        return BoundWorktree(handle, tuple(gaps))

    def release(self, handle: WorktreeHandle) -> tuple[BindingGap, ...]:
        """Release every composed binding; failures are reported gaps, never raised."""
        gaps: list[BindingGap] = []
        for binding in self._bindings:
            try:
                binding.release(handle)
            except WorkspaceBindingError as exc:
                gaps.append(self._gap(binding.presenter, "release", handle, exc))
        return tuple(gaps)

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
        """Release every composed binding, THEN remove the worktree through the manager (E30).

        The two effects stay separate and ordered: releasing first means a remove can never
        strand an orphaned ``"<label> (deleted)"`` presentation record (E31). An unreleasable
        binding (presenter down) is reported and the native remove still runs — mechanics are
        never held hostage to presentation.
        """
        self.release(handle)
        return self._manager.remove(handle, force)

    def _gap(
        self, presenter: str, action: str, handle: WorktreeHandle, exc: WorkspaceBindingError
    ) -> BindingGap:
        gap = BindingGap(presenter, action, exc.detail or str(exc), exc.code)
        verb = "bind" if action == "bind" else "release"
        self._warn(
            f"{presenter} binding gap: could not {verb} {handle.path} ({gap.detail}); "
            "the worktree is unaffected and the binding is repairable"
        )
        return gap

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
