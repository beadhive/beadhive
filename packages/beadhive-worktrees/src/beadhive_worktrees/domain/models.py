"""Typed worktree identity, branch policy, and lifecycle outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

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
class CreateWorktreeRequest:
    main: Path
    branch: str
    target: Path
    new_branch: bool
    start_point: str = ""


@dataclass(frozen=True, slots=True)
class RemoveWorktreeRequest:
    main: Path
    target: Path
    force: bool = False
    keep_branch: bool = True


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    """One adapter outcome; ``handled=False`` requests the native fallback."""

    target: Path
    handled: bool
    succeeded: bool
    returncode: int = 0
    delegated: bool = False
    error: str = ""

    @classmethod
    def unhandled(cls, target: Path) -> ProvisioningResult:
        return cls(target, handled=False, succeeded=False)

    @classmethod
    def success(cls, target: Path, *, delegated: bool) -> ProvisioningResult:
        return cls(target, handled=True, succeeded=True, delegated=delegated)

    @classmethod
    def failure(cls, target: Path, returncode: int, error: str = "") -> ProvisioningResult:
        return cls(
            target,
            handled=True,
            succeeded=False,
            returncode=returncode,
            error=error,
        )
