"""Plugin-mediated implementation of the worktree provisioner port."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..domain import CreateWorktreeRequest, ProvisioningResult, RemoveWorktreeRequest


def _ignore_create(_request: CreateWorktreeRequest) -> None:
    return None


def _unhandled_create(_request: CreateWorktreeRequest) -> Path | None:
    return None


def _ignore_created(_request: CreateWorktreeRequest, _result: ProvisioningResult) -> None:
    return None


def _unhandled_remove(_request: RemoveWorktreeRequest) -> bool:
    return False


def _ignore_removed(_request: RemoveWorktreeRequest, _result: ProvisioningResult) -> None:
    return None


def _unsupported() -> bool:
    return False


def _ignore_warning(_message: str) -> None:
    return None


class PluginWorktreeProvisioner:
    """Adapt one action-scoped plugin composition to ``WorktreeProvisioner``."""

    def __init__(
        self,
        *,
        preparing: Callable[[CreateWorktreeRequest], None] = _ignore_create,
        create_delegate: Callable[[CreateWorktreeRequest], Path | None] = _unhandled_create,
        created_observer: Callable[
            [CreateWorktreeRequest, ProvisioningResult], None
        ] = _ignore_created,
        remove_delegate: Callable[[RemoveWorktreeRequest], bool] = _unhandled_remove,
        removed_observer: Callable[
            [RemoveWorktreeRequest, ProvisioningResult], None
        ] = _ignore_removed,
        supports_create: Callable[[], bool] = _unsupported,
        warn: Callable[[str], None] = _ignore_warning,
    ) -> None:
        self._preparing = preparing
        self._create_delegate = create_delegate
        self._created_observer = created_observer
        self._remove_delegate = remove_delegate
        self._removed_observer = removed_observer
        self._supports_create = supports_create
        self._warn = warn

    def prepare(self, request: CreateWorktreeRequest) -> None:
        self._preparing(request)

    def create(self, request: CreateWorktreeRequest) -> ProvisioningResult:
        if not request.new_branch:
            if self._supports_create():
                self._warn(
                    "worktree attach stays native (delegation only covers new-branch create)"
                )
            return ProvisioningResult.unhandled(request.target)
        target = self._create_delegate(request)
        if target is None:
            return ProvisioningResult.unhandled(request.target)
        return ProvisioningResult.success(target, delegated=True)

    def created(self, request: CreateWorktreeRequest, result: ProvisioningResult) -> None:
        self._created_observer(request, result)

    def remove(self, request: RemoveWorktreeRequest) -> ProvisioningResult:
        if not self._remove_delegate(request):
            return ProvisioningResult.unhandled(request.target)
        return ProvisioningResult.success(request.target, delegated=True)

    def removed(self, request: RemoveWorktreeRequest, result: ProvisioningResult) -> None:
        self._removed_observer(request, result)
