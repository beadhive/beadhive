"""Hive identity and lifecycle orchestration over typed ports."""

from __future__ import annotations

from ..contracts.ports import DependencyProbe, HiveRegistry, LifecyclePublisher, WorkspaceRealizer
from ..domain.models import (
    DiscoverHivesResult,
    HiveListRequest,
    HiveListResult,
    HiveStatusRequest,
    HiveStatusResult,
    OnboardHiveRequest,
    OnboardHiveResult,
    ReadinessRequest,
    ReadinessResult,
    RegisterHiveRequest,
    RegisterHiveResult,
    RetireHiveRequest,
    RetireHiveResult,
)


class HiveLifecycleService:
    """One application boundary shared by CLI and MCP transport adapters."""

    def __init__(
        self,
        registry: HiveRegistry,
        workspace: WorkspaceRealizer,
        dependencies: DependencyProbe,
        lifecycle: LifecyclePublisher,
    ) -> None:
        self._registry = registry
        self._workspace = workspace
        self._dependencies = dependencies
        self._lifecycle = lifecycle

    def register(self, request: RegisterHiveRequest) -> RegisterHiveResult:
        result = self._registry.register(request)
        if result.identity != request.identity:
            raise ValueError("registry changed the requested hive identity")
        return result

    def discover(self) -> DiscoverHivesResult:
        return self._registry.discover()

    def list(self, request: HiveListRequest) -> HiveListResult:
        return self._registry.list(request)

    def status(self, request: HiveStatusRequest | None = None) -> HiveStatusResult:
        return self._registry.status(request or HiveStatusRequest())

    def onboard(self, request: OnboardHiveRequest) -> OnboardHiveResult:
        target = self._workspace.target_for(request.identity)
        expected = request.identity.target_under(_workspace_parent(target, request.identity))
        if target != expected:
            raise ValueError("workspace target conflicts with requested hive identity")
        result = self._lifecycle.onboard(request, target=target)
        if result.identity != request.identity or result.target != target:
            raise ValueError("onboard result conflicts with requested hive identity")
        return result

    def readiness(self, request: ReadinessRequest | None = None) -> ReadinessResult:
        return self._dependencies.readiness(request or ReadinessRequest())

    def retire(self, request: RetireHiveRequest) -> RetireHiveResult:
        result = self._lifecycle.retire(request)
        if result.hive_id != request.hive_id or result.scope is not request.scope:
            raise ValueError("retire result conflicts with requested hive identity or scope")
        return result


def _workspace_parent(target: str, identity) -> str:
    suffix = f"/{identity.provider}/{identity.organization}/{identity.repository}"
    if not target.endswith(suffix):
        raise ValueError("workspace target must end in the canonical hive triplet")
    return target[: -len(suffix)] or "/"
