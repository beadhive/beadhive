"""Typed worktree identity, branch policy, and lifecycle outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Generic, TypeVar

from beadhive_plugins.worktree_slots import binding_composes

WT_PREFIX = "wt/"
BATCH_BRANCH_PREFIX = "batch/"
BATCH_LEAF_PREFIX = "batch-"
StatusRowT = TypeVar("StatusRowT")


def sanitize_leaf(value: str) -> str:
    """Return the existing registry-compatible filesystem leaf."""
    value = value.lower()
    value = re.sub(r"[^a-z0-9-]", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


@dataclass(frozen=True, slots=True)
class WorktreeBranchPolicy:
    """Configuration needed to derive one managed branch and leaf."""

    bead_template: str = "bead/{kind}/{id}"
    session_template: str = "session/{ts}-{rand}"


@dataclass(frozen=True, slots=True)
class WorktreeBinding:
    """Canonical branch/filesystem identity for one managed worktree."""

    branch: str
    leaf: str

    def __post_init__(self) -> None:
        if not self.branch.startswith(WT_PREFIX):
            raise ValueError("managed worktree branch must start with wt/")
        if not self.leaf or "/" in self.leaf:
            raise ValueError("managed worktree leaf must be one non-empty path segment")


@dataclass(frozen=True, slots=True)
class ManagedWorktree:
    """One linked worktree discovered below the managed shadow root."""

    hive: str
    path: str
    branch: str

    def __post_init__(self) -> None:
        if not self.hive or not self.path or not self.branch:
            raise ValueError("managed worktree identity fields must be non-empty")


@dataclass(frozen=True, slots=True)
class WorktreeInventoryRequest:
    hive: str = ""


@dataclass(frozen=True, slots=True)
class WorktreeInventoryResult:
    worktrees: tuple[ManagedWorktree, ...]


@dataclass(frozen=True, slots=True)
class WorktreeStatusRequest:
    hive: str = ""


@dataclass(frozen=True, slots=True)
class WorktreeStatusResult(Generic[StatusRowT]):
    """Typed carrier that leaves adopted classification records at their owner."""

    rows: tuple[StatusRowT, ...]


def apply_prefix(suffix: str) -> str:
    """Prepend the managed prefix once, preserving the legacy normalization."""
    return WT_PREFIX + suffix.removeprefix(WT_PREFIX).lstrip("/")


def branch_suffix(
    policy: WorktreeBranchPolicy,
    *,
    bead: str = "",
    branch: str = "",
    kind: str = "issue",
    timestamp: str = "",
    random_token: str = "",
) -> str:
    """Derive the suffix after ``wt/`` without performing I/O."""
    if bead:
        return policy.bead_template.format(id=bead, kind=kind or "issue")
    if branch:
        return branch
    if not timestamp or not random_token:
        raise ValueError("session binding requires timestamp and random token")
    return policy.session_template.format(
        ts=timestamp,
        rand=random_token,
        id=f"{timestamp}-{random_token}",
    )


def leaf_for_branch(branch: str) -> str:
    """Derive the collision-safe legacy leaf for a managed branch."""
    body = branch.removeprefix(WT_PREFIX)
    if body.startswith(BATCH_BRANCH_PREFIX):
        return BATCH_LEAF_PREFIX + sanitize_leaf(body[len(BATCH_BRANCH_PREFIX) :])
    return sanitize_leaf(branch.rsplit("/", 1)[-1])


def bind_worktree(suffix: str) -> WorktreeBinding:
    branch = apply_prefix(suffix)
    return WorktreeBinding(branch, leaf_for_branch(branch))


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    """What Beadhive policy decided; the selected manager only executes it (bh-055ot.1).

    ``path`` and ``branch`` are exact — a manager never chooses either. ``base`` is the start
    point intent: the fork point for ``create``; for ``attach`` it is recorded on the handle
    only and never moves an existing branch tip (E37). ``identity`` is the bead/worktree
    correlation id and defaults to the path leaf.
    """

    main: Path
    branch: str
    path: Path
    base: str = ""
    identity: str = ""

    @property
    def correlation(self) -> str:
        return self.identity or self.path.name


@dataclass(frozen=True, slots=True)
class WorktreeHandle:
    """One created or attached worktree as the manager left it.

    ``bindings`` maps a presenter to its binding reference (for example ``{"herdr": "w4"}``),
    filled by whichever component performed the bind. It is a cache of a fact re-derivable from
    the presenter's own inventory, never its sole source of truth. ``branch`` may be empty on a
    handle re-derived for removal when the caller does not know it; removal never needs it.
    """

    identity: str
    main: Path
    path: Path
    branch: str
    base: str = ""
    bindings: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, spec: WorktreeSpec) -> WorktreeHandle:
        return cls(spec.correlation, spec.main, spec.path, spec.branch, spec.base)

    @classmethod
    def for_removal(cls, main: Path, path: Path, branch: str = "") -> WorktreeHandle:
        return cls(path.name, main, path, branch)

    def with_binding(self, presenter: str, reference: str) -> WorktreeHandle:
        return replace(self, bindings={**self.bindings, presenter: reference})

    def without_binding(self, presenter: str) -> WorktreeHandle:
        return replace(
            self, bindings={key: ref for key, ref in self.bindings.items() if key != presenter}
        )


@dataclass(frozen=True, slots=True)
class WorktreeRemoved:
    """Receipt for a removed linked worktree; the branch is never deleted by a manager.

    ``gaps`` names any composed binding the lifecycle service could not release before the
    remove (a presenter outage) — reported, repairable, and never a reason to keep the worktree.
    """

    handle: WorktreeHandle
    gaps: tuple[BindingGap, ...] = ()


class WorkspaceBindingError(RuntimeError):
    """A ``workspace.binding`` could not bind or release one worktree (bh-cb4jo).

    Always a presentation-layer gap, never a mechanics-layer one: the lifecycle service reports
    it and carries on (a native claim is never rolled back, a native remove is never blocked).
    ``code`` is the presenter's own machine-readable failure code when it supplied one (for
    example Herdr's ``server_not_running``).
    """

    def __init__(self, presenter: str, detail: str, *, code: str = "") -> None:
        super().__init__(detail or f"{presenter} binding failed")
        self.presenter = presenter
        self.detail = detail
        self.code = code


@dataclass(frozen=True, slots=True)
class BindingGap:
    """One binding step that did not happen: a worktree left unbound or a binding unreleased.

    ``action`` is ``"bind"`` or ``"release"``. A gap is always re-bindable/re-closable later
    through the presenter's single reconciliation primitive; it is never a reason to undo work.
    """

    presenter: str
    action: str
    detail: str
    code: str = ""


@dataclass(frozen=True, slots=True)
class BoundWorktree:
    """Result of binding one handle: the (possibly re-referenced) handle plus any gaps."""

    handle: WorktreeHandle
    gaps: tuple[BindingGap, ...] = ()


class WorktreeManagerError(RuntimeError):
    """A manager could not execute a spec; carries the effect's exit code and stderr."""

    def __init__(self, path: Path, returncode: int, error: str = "") -> None:
        super().__init__(error or f"worktree operation failed for {path} (exit {returncode})")
        self.path = path
        self.returncode = returncode
        self.error = error


@dataclass(frozen=True, slots=True)
class WorktreeManagerCapabilities:
    """The ADR's capability declarations for one ``worktree.manager`` provider.

    ``binds`` lists presenters the manager binds as a side effect of create/attach;
    ``remove_releases_bindings`` says whether its remove also releases them.
    """

    binds: tuple[str, ...] = ()
    remove_releases_bindings: bool = False

    @classmethod
    def of(cls, manager: object) -> WorktreeManagerCapabilities:
        return cls(
            tuple(getattr(manager, "binds", ())),
            bool(getattr(manager, "remove_releases_bindings", False)),
        )

    def composes_binding(self, presenter: str) -> bool:
        """Compose a separate ``WorkspaceBinding`` only for a presenter not already bound."""
        return binding_composes(self.binds, presenter)


#: Native Git binds nothing and has no binding concept to release.
NATIVE_CAPABILITIES = WorktreeManagerCapabilities(binds=(), remove_releases_bindings=False)
