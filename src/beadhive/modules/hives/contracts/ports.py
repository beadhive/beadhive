"""Narrow outbound ports for hive application services."""

from __future__ import annotations

from typing import Protocol

from ..domain.models import (
    DiscoverHivesResult,
    HiveIdentity,
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


class HiveRegistry(Protocol):
    def register(self, request: RegisterHiveRequest) -> RegisterHiveResult: ...

    def discover(self) -> DiscoverHivesResult: ...

    def list(self, request: HiveListRequest) -> HiveListResult: ...

    def status(self, request: HiveStatusRequest) -> HiveStatusResult: ...


class WorkspaceRealizer(Protocol):
    """Resolve a hive target under the accepted workspace-root policy."""

    def target_for(self, identity: HiveIdentity) -> str: ...


class DependencyProbe(Protocol):
    def readiness(self, request: ReadinessRequest) -> ReadinessResult: ...


class LifecyclePublisher(Protocol):
    """Execute hive lifecycle effects through kernel-backed concrete adapters."""

    def onboard(self, request: OnboardHiveRequest, *, target: str) -> OnboardHiveResult: ...

    def retire(self, request: RetireHiveRequest) -> RetireHiveResult: ...
