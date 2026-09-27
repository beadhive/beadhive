"""Native Git implementation of the ``worktree.manager`` port (the built-in default)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from beadhive_plugins.contracts import ProviderBinding, ProviderKey

from ..contracts.ports import WORKTREE_MANAGER
from ..domain import (
    NATIVE_CAPABILITIES,
    WorktreeHandle,
    WorktreeManagerError,
    WorktreeRemoved,
    WorktreeSpec,
)

#: Plugin id native Git registers under for the built-in default binding of
#: :data:`beadhive_worktrees.contracts.WORKTREE_MANAGER`, and the only legal value of the
#: ``worktrees.manager`` config key today (root composition is the only caller — this package
#: never selects or binds its own provider).
NATIVE_PROVIDER_ID = "native"


class NativeGitWorktreeManager:
    """Execute a Beadhive-computed spec with ``git worktree`` — never choosing names or paths.

    Capability declarations (docs/design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md):
    native binds no presenter and has no binding concept for ``remove`` to release.
    """

    binds: tuple[str, ...] = NATIVE_CAPABILITIES.binds
    remove_releases_bindings: bool = NATIVE_CAPABILITIES.remove_releases_bindings

    def __init__(self, run_git: Callable[..., Any]) -> None:
        self._run_git = run_git

    def create(self, spec: WorktreeSpec) -> WorktreeHandle:
        """``git worktree add -b <branch> <path> [<base>]`` — a NEW branch forked off ``base``."""
        command = ["git", "-C", str(spec.main), "worktree", "add", "-b", spec.branch]
        command.append(str(spec.path))
        if spec.base:
            command.append(spec.base)
        self._execute(command, spec.path)
        return WorktreeHandle.of(spec)

    def attach(self, spec: WorktreeSpec) -> WorktreeHandle:
        """Attach an EXISTING branch at the exact path, leaving its tip exactly where it is.

        ``spec.base`` is deliberately never passed to git: attach records it on the handle as
        intent only and never rebases, resets, or fast-forwards the branch (E37). Stale admin
        entries are pruned first so a worktree whose directory vanished out-of-band doesn't
        block the re-attach.
        """
        self._run_git(["git", "-C", str(spec.main), "worktree", "prune"], check=False)
        command = ["git", "-C", str(spec.main), "worktree", "add", str(spec.path), spec.branch]
        self._execute(command, spec.path)
        return WorktreeHandle.of(spec)

    def remove(self, handle: WorktreeHandle, force: bool) -> WorktreeRemoved:
        """``git worktree remove <path> [--force]`` — the branch always survives."""
        command = ["git", "-C", str(handle.main), "worktree", "remove", str(handle.path)]
        if force:
            command.append("--force")
        self._execute(command, handle.path)
        return WorktreeRemoved(handle)

    def _execute(self, command: list[str], path: Path) -> None:
        result = self._run_git(command, check=False)
        if result.returncode != 0:
            raise WorktreeManagerError(path, result.returncode, getattr(result, "stderr", "") or "")


class NativeGitBranchInspector:
    """Read-only ``git`` branch facts backing Beadhive's own attach pre-check (E37)."""

    def __init__(self, run_git: Callable[..., Any]) -> None:
        self._run_git = run_git

    def tip(self, main: Path, branch: str) -> str:
        result = self._run_git(
            ["git", "-C", str(main), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
            capture=True,
        )
        return (getattr(result, "stdout", "") or "").strip() if result.returncode == 0 else ""

    def contains(self, main: Path, branch: str, base: str) -> bool:
        result = self._run_git(
            ["git", "-C", str(main), "merge-base", "--is-ancestor", base, f"refs/heads/{branch}"],
            check=False,
            capture=True,
        )
        return result.returncode == 0


def native_worktree_manager_provider_binding(
    manager: NativeGitWorktreeManager,
) -> ProviderBinding[object]:
    """Wrap the built-in native manager for ``bind_application_port`` at root composition."""

    return ProviderBinding(ProviderKey(NATIVE_PROVIDER_ID, WORKTREE_MANAGER), manager)
