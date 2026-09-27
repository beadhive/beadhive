"""The ``worktree.manager`` and ``workspace.binding`` capability slots (bh-055ot.1).

Declared here, on the stdlib-only slot/binding-contract package, per
``docs/design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md`` ("Placement"). The concrete
spec/handle domain types, the naming and safety policy, and the native Git manager live in
``beadhive-worktrees``, which depends on this package — never the reverse. The port methods are
therefore generic over the spec, handle, and removal-receipt types: ``beadhive-worktrees``
parameterises them with its own ``WorktreeSpec`` / ``WorktreeHandle`` / ``WorktreeRemoved``.

Importing this module declares slots and method shapes only. It never selects a manager, reads
``worktrees.manager``, or composes a binding — that is root composition's job.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypeVar, runtime_checkable

from .contracts import CapabilityRef

SpecT_contra = TypeVar("SpecT_contra", contravariant=True)
HandleT = TypeVar("HandleT")
RemovedT_co = TypeVar("RemovedT_co", covariant=True)

#: Exactly one configured manager per hive executes worktree mechanics (create/attach/remove).
#: Beadhive policy computes the spec; the manager only executes it and never chooses a path or
#: branch name itself. Native Git is the built-in default and, today, the only legal selection.
WORKTREE_MANAGER = CapabilityRef("worktree.manager", 1)

#: Zero or one presentation binding (for example a Herdr workspace) per presenter, composed
#: only when the selected manager does not already bind that presenter itself
#: (:func:`binding_composes`).
WORKSPACE_BINDING = CapabilityRef("workspace.binding", 1)


@runtime_checkable
class WorktreeManager(Protocol[SpecT_contra, HandleT, RemovedT_co]):
    """Typed port for :data:`WORKTREE_MANAGER`.

    ``binds`` names the presenters this manager binds as a side effect of its own
    ``create``/``attach``; ``remove_releases_bindings`` says whether its ``remove`` also
    releases those bindings. Failures raise; a returned handle is always a created or attached
    worktree at exactly the spec's path and branch.
    """

    @property
    def binds(self) -> tuple[str, ...]: ...

    @property
    def remove_releases_bindings(self) -> bool: ...

    def create(self, spec: SpecT_contra) -> HandleT:
        """Create a NEW branch at the exact path the spec names."""
        ...

    def attach(self, spec: SpecT_contra) -> HandleT:
        """Attach an EXISTING branch at the exact path; never moves the branch tip."""
        ...

    def remove(self, handle: HandleT, force: bool) -> RemovedT_co:
        """Remove the linked worktree only (never the branch); ``force`` bypasses dirty refusal."""
        ...


@runtime_checkable
class WorkspaceBinding(Protocol[HandleT]):
    """Typed port for :data:`WORKSPACE_BINDING`: presentation grouping for one presenter.

    ``bind`` runs after ``create``/``attach`` and is idempotent; ``release`` must complete
    before the manager's ``remove`` runs.
    """

    @property
    def presenter(self) -> str: ...

    def bind(self, handle: HandleT) -> HandleT: ...

    def release(self, handle: HandleT) -> None: ...


def binding_composes(manager_binds: Iterable[str], presenter: str) -> bool:
    """The ADR composition rule: compose a binding only if the manager does not bind it."""

    return presenter not in tuple(manager_binds)


__all__ = [
    "WORKSPACE_BINDING",
    "WORKTREE_MANAGER",
    "HandleT",
    "RemovedT_co",
    "SpecT_contra",
    "WorkspaceBinding",
    "WorktreeManager",
    "binding_composes",
]
