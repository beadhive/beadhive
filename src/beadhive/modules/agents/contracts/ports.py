"""Narrow outbound ports for agent launch application services."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from ..domain.profile import ResolvedAgentLaunchProfile
from ..domain.transaction import (
    AdapterCommitResultV1,
    AgentObservationV1,
    BoundWorkspace,
    CompensationResult,
    LaunchReceiptV1,
    OperationPhase,
    OperationResult,
    PreparedLaunch,
    RecoverLaunchRequest,
    RecoveryResultV1,
    TeardownLaunchRequest,
    TeardownResultV1,
    TerminalReceipt,
    WorkspaceTargetV1,
)


@dataclass(frozen=True, slots=True)
class OperationKey:
    launch_id: str
    phase: OperationPhase
    operation_id: str


@dataclass(frozen=True, slots=True)
class StoredOperation:
    key: OperationKey
    input_digest: str
    result: OperationResult


class WorkspaceBinder(Protocol):
    """Resolve or claim exactly one provider-independent workspace binding."""

    def bind(
        self,
        target: WorkspaceTargetV1,
        profile: ResolvedAgentLaunchProfile,
        *,
        launch_id: str,
    ) -> BoundWorkspace: ...


class LaunchLedger(Protocol):
    """Durable operation replay and terminal-state store.

    Concrete stores must serialize writes per launch. ``record_operation`` and
    ``record_terminal`` return the already-stored value when another writer won.
    """

    def lock(self, launch_id: str) -> AbstractContextManager[None]: ...

    def get_operation(self, key: OperationKey) -> StoredOperation | None: ...

    def record_operation(self, operation: StoredOperation) -> StoredOperation: ...

    def get_terminal(self, launch_id: str) -> TerminalReceipt | None: ...

    def record_terminal(self, receipt: TerminalReceipt) -> TerminalReceipt: ...


class AgentLauncher(Protocol):
    """Bounded external allocation effects; provider topology stays behind this port."""

    def commit(
        self,
        prepared: PreparedLaunch,
        *,
        operation_id: str,
    ) -> AdapterCommitResultV1: ...

    def compensate(
        self,
        prepared: PreparedLaunch,
        result: AdapterCommitResultV1 | None,
    ) -> CompensationResult: ...


class AgentObserver(Protocol):
    def observe(self, receipt: LaunchReceiptV1) -> AgentObservationV1: ...


class AgentRecovery(Protocol):
    def recover(self, request: RecoverLaunchRequest) -> RecoveryResultV1: ...


class AgentTeardown(Protocol):
    def teardown(self, request: TeardownLaunchRequest) -> TeardownResultV1: ...
