"""Native Git implementation of the worktree provisioner port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from beadhive_plugins.contracts import ProviderBinding, ProviderKey

from ..contracts.ports import WORKTREE_MANAGER
from ..domain import CreateWorktreeRequest, ProvisioningResult, RemoveWorktreeRequest

#: Plugin id native Git registers under for the built-in default binding of
#: :data:`beadhive_worktrees.contracts.WORKTREE_MANAGER` (root composition is the only caller —
#: this package never selects or binds its own provider).
NATIVE_PROVIDER_ID = "native"


class NativeGitWorktreeProvisioner:
    def __init__(self, run_git: Callable[..., Any]) -> None:
        self._run_git = run_git

    def prepare(self, request: CreateWorktreeRequest) -> None:
        request.target.parent.mkdir(parents=True, exist_ok=True)

    def create(self, request: CreateWorktreeRequest) -> ProvisioningResult:
        if request.new_branch:
            command = [
                "git",
                "-C",
                str(request.main),
                "worktree",
                "add",
                "-b",
                request.branch,
                str(request.target),
            ]
            if request.start_point:
                command.append(request.start_point)
        else:
            self._run_git(["git", "-C", str(request.main), "worktree", "prune"], check=False)
            command = [
                "git",
                "-C",
                str(request.main),
                "worktree",
                "add",
                str(request.target),
                request.branch,
            ]
        result = self._run_git(command, check=False)
        if result.returncode != 0:
            return ProvisioningResult.failure(
                request.target, result.returncode, getattr(result, "stderr", "") or ""
            )
        return ProvisioningResult.success(request.target, delegated=False)

    def created(self, request: CreateWorktreeRequest, result: ProvisioningResult) -> None:
        del request, result

    def remove(self, request: RemoveWorktreeRequest) -> ProvisioningResult:
        command = ["git", "-C", str(request.main), "worktree", "remove", str(request.target)]
        if request.force:
            command.append("--force")
        result = self._run_git(command, check=False)
        if result.returncode != 0:
            return ProvisioningResult.failure(
                request.target, result.returncode, getattr(result, "stderr", "") or ""
            )
        return ProvisioningResult.success(request.target, delegated=False)

    def removed(self, request: RemoveWorktreeRequest, result: ProvisioningResult) -> None:
        del request, result


def native_worktree_manager_provider_binding(
    provisioner: NativeGitWorktreeProvisioner,
) -> ProviderBinding[object]:
    """Wrap the built-in native adapter for ``bind_application_port`` at root composition."""

    return ProviderBinding(ProviderKey(NATIVE_PROVIDER_ID, WORKTREE_MANAGER), provisioner)
