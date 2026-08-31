"""Provider-neutral launch orchestration over typed outbound ports."""

from __future__ import annotations

from typing import TypeVar, cast

from pydantic import BaseModel

from ..contracts.ports import (
    AgentLauncher,
    AgentObserver,
    AgentRecovery,
    AgentTeardown,
    LaunchLedger,
    OperationKey,
    StoredOperation,
    WorkspaceBinder,
)
from ..domain.profile import AgentLaunchReceipt, resolve_agent_launch_profile
from ..domain.transaction import (
    AbortLaunchRequest,
    AbortReceiptV1,
    AdapterCommitResultV1,
    AdapterOutcome,
    CommitLaunchRequest,
    CommitLaunchResult,
    CompensationStatus,
    LaunchReceiptV1,
    ObserveLaunchRequest,
    OperationPhase,
    OperationResult,
    PreparedLaunch,
    PreparedLaunchV1,
    PrepareLaunchRequest,
    RecoverLaunchRequest,
    RecoveryDisposition,
    RecoveryResultV1,
    TeardownDisposition,
    TeardownLaunchRequest,
    TeardownResultV1,
    portable_digest,
)

ResultT = TypeVar("ResultT", bound=OperationResult)


class OperationConflict(RuntimeError):
    """An operation key was reused with different canonical input."""


class TerminalStateConflict(RuntimeError):
    """A launch was asked to cross from one terminal state to another."""


class PrepareLaunchService:
    def __init__(self, binder: WorkspaceBinder, ledger: LaunchLedger) -> None:
        self._binder = binder
        self._ledger = ledger

    def execute(self, request: PrepareLaunchRequest) -> PreparedLaunch:
        with self._ledger.lock(request.launch_id):
            return self._execute_locked(request)

    def _execute_locked(self, request: PrepareLaunchRequest) -> PreparedLaunch:
        key = OperationKey(request.launch_id, OperationPhase.PREPARE, request.operation_id)
        digest = portable_digest(request)
        replay = _replay(self._ledger, key, digest, PreparedLaunch)
        if replay is not None:
            return replay
        if self._ledger.get_terminal(request.launch_id) is not None:
            raise TerminalStateConflict("cannot prepare a terminal launch")

        # Profile policy is resolved before the binding port can mutate a checkout.
        resolved = resolve_agent_launch_profile(request.profile)
        bound = self._binder.bind(request.target, resolved, launch_id=request.launch_id)
        if bound.binding.hive_id != request.target.hive_id:
            raise ValueError("workspace binding conflicts with requested hive")
        if bound.binding.binding_kind != request.target.binding_kind:
            raise ValueError("workspace binding kind conflicts with request")
        if bound.binding.parent_launch_id != request.target.parent_launch_id:
            raise ValueError("workspace binding changed parent launch identity")
        for field in ("requested_bead", "batch_id", "epic_id"):
            if getattr(bound.binding, field) != getattr(request.target, field):
                raise ValueError(f"workspace binding changed {field}")

        portable = PreparedLaunchV1(
            launch_id=request.launch_id,
            prepare_operation_id=request.operation_id,
            profile_receipt=AgentLaunchReceipt.from_resolved(resolved),
            workspace_binding=bound.binding,
            binding_digest=portable_digest(bound.binding),
            adapter_kind=request.adapter_kind,
            adapter_action=bound.adapter_action,
            adapter_plan_digest=portable_digest(bound.adapter_plan),
        )
        prepared = PreparedLaunch(
            portable=portable,
            resolved_profile=resolved,
            adapter_plan=bound.adapter_plan,
            local_capability=bound.local_capability,
        )
        return _record(self._ledger, key, digest, prepared, PreparedLaunch)


class CommitLaunchService:
    def __init__(self, launcher: AgentLauncher, ledger: LaunchLedger) -> None:
        self._launcher = launcher
        self._ledger = ledger

    def execute(self, request: CommitLaunchRequest) -> CommitLaunchResult:
        request.validate()
        with self._ledger.lock(request.prepared.portable.launch_id):
            return self._execute_locked(request)

    def _execute_locked(self, request: CommitLaunchRequest) -> CommitLaunchResult:
        prepared = request.prepared
        key = OperationKey(
            prepared.portable.launch_id,
            OperationPhase.COMMIT,
            request.operation_id,
        )
        digest = _digest_fields(
            prepared=prepared.portable,
            operation_id=request.operation_id,
        )
        replay = _replay(self._ledger, key, digest, CommitLaunchResult)
        if replay is not None:
            return replay
        terminal = self._ledger.get_terminal(prepared.portable.launch_id)
        if isinstance(terminal, AbortReceiptV1):
            raise TerminalStateConflict("cannot commit an aborted launch")
        if isinstance(terminal, LaunchReceiptV1):
            raise TerminalStateConflict("launch is already committed by another operation")

        adapter_result = self._launcher.commit(prepared, operation_id=request.operation_id)
        _validate_adapter_result(prepared, adapter_result, request.operation_id)
        receipt = None
        if adapter_result.outcome == AdapterOutcome.COMMITTED:
            receipt = LaunchReceiptV1(
                launch_id=prepared.portable.launch_id,
                prepare_operation_id=prepared.portable.prepare_operation_id,
                commit_operation_id=request.operation_id,
                profile_receipt=prepared.portable.profile_receipt,
                workspace_binding=prepared.portable.workspace_binding,
                adapter_kind=prepared.portable.adapter_kind,
                allocation_id=adapter_result.allocation_id,
                generation=adapter_result.generation,
            )
            stored_terminal = self._ledger.record_terminal(receipt)
            if stored_terminal != receipt:
                raise TerminalStateConflict("another terminal launch state won commit")
        result = CommitLaunchResult(adapter_result=adapter_result, receipt=receipt)
        return _record(self._ledger, key, digest, result, CommitLaunchResult)


class AbortLaunchService:
    def __init__(self, launcher: AgentLauncher, ledger: LaunchLedger) -> None:
        self._launcher = launcher
        self._ledger = ledger

    def execute(self, request: AbortLaunchRequest) -> AbortReceiptV1:
        request.validate()
        with self._ledger.lock(request.prepared.portable.launch_id):
            return self._execute_locked(request)

    def _execute_locked(self, request: AbortLaunchRequest) -> AbortReceiptV1:
        prepared = request.prepared
        key = OperationKey(
            prepared.portable.launch_id,
            OperationPhase.ABORT,
            request.operation_id,
        )
        digest = _digest_fields(
            prepared=prepared.portable,
            operation_id=request.operation_id,
            reason_code=request.reason_code,
            failed_result=request.failed_result,
            failed_operation_id=request.failed_operation_id,
        )
        replay = _replay(self._ledger, key, digest, AbortReceiptV1)
        if replay is not None:
            return replay
        terminal = self._ledger.get_terminal(prepared.portable.launch_id)
        if isinstance(terminal, LaunchReceiptV1):
            raise TerminalStateConflict("cannot abort a committed launch")
        if isinstance(terminal, AbortReceiptV1):
            raise TerminalStateConflict("launch is already aborted by another operation")

        if prepared.portable.adapter_action == "none":
            compensation = CompensationStatus.NOT_NEEDED
            allocation_id = None
        else:
            compensated = self._launcher.compensate(prepared, request.failed_result)
            compensation = compensated.status
            allocation_id = compensated.allocation_id
        receipt = AbortReceiptV1(
            launch_id=prepared.portable.launch_id,
            abort_operation_id=request.operation_id,
            failed_operation_id=request.failed_operation_id,
            workspace_binding=prepared.portable.workspace_binding,
            reason_code=request.reason_code,
            compensation=compensation,
            compensated_allocation_id=allocation_id,
        )
        stored_terminal = self._ledger.record_terminal(receipt)
        if stored_terminal != receipt:
            raise TerminalStateConflict("another terminal launch state won abort")
        return _record(self._ledger, key, digest, receipt, AbortReceiptV1)


class ObserveLaunchService:
    def __init__(self, observer: AgentObserver) -> None:
        self._observer = observer

    def execute(self, request: ObserveLaunchRequest):
        observation = self._observer.observe(request.receipt)
        if observation.launch_id != request.receipt.launch_id:
            raise ValueError("observation conflicts with launch identity")
        expected = portable_digest(request.receipt.workspace_binding)
        if observation.binding_digest != expected:
            raise ValueError("observation conflicts with workspace binding")
        if (
            request.receipt.generation is not None
            and observation.generation is not None
            and observation.generation != request.receipt.generation
        ):
            raise ValueError("observation conflicts with launch generation")
        return observation


class RecoverLaunchService:
    def __init__(self, recovery: AgentRecovery, ledger: LaunchLedger) -> None:
        self._recovery = recovery
        self._ledger = ledger

    def execute(self, request: RecoverLaunchRequest) -> RecoveryResultV1:
        with self._ledger.lock(request.receipt.launch_id):
            return self._execute_locked(request)

    def _execute_locked(self, request: RecoverLaunchRequest) -> RecoveryResultV1:
        key = OperationKey(request.receipt.launch_id, OperationPhase.RECOVER, request.operation_id)
        digest = portable_digest(request)
        replay = _replay(self._ledger, key, digest, RecoveryResultV1)
        if replay is not None:
            return replay
        result = self._recovery.recover(request)
        _validate_recovery(request, result)
        return _record(self._ledger, key, digest, result, RecoveryResultV1)


class TeardownLaunchService:
    def __init__(self, teardown: AgentTeardown, ledger: LaunchLedger) -> None:
        self._teardown = teardown
        self._ledger = ledger

    def execute(self, request: TeardownLaunchRequest) -> TeardownResultV1:
        with self._ledger.lock(request.receipt.launch_id):
            return self._execute_locked(request)

    def _execute_locked(self, request: TeardownLaunchRequest) -> TeardownResultV1:
        key = OperationKey(
            request.receipt.launch_id,
            OperationPhase.TEARDOWN,
            request.operation_id,
        )
        digest = portable_digest(request)
        replay = _replay(self._ledger, key, digest, TeardownResultV1)
        if replay is not None:
            return replay
        result = self._teardown.teardown(request)
        if result.launch_id != request.receipt.launch_id:
            raise ValueError("teardown result conflicts with launch identity")
        if result.teardown_operation_id != request.operation_id:
            raise ValueError("teardown result conflicts with operation identity")
        expected = portable_digest(request.receipt.workspace_binding)
        if result.binding_digest != expected:
            raise ValueError("teardown result conflicts with workspace binding")
        if result.generation != request.receipt.generation:
            raise ValueError("teardown result conflicts with launch generation")
        if result.disposition in {TeardownDisposition.REFUSED, TeardownDisposition.RETAINED}:
            if result.error_code is None:
                raise ValueError("retained or refused teardown requires error_code")
        elif result.error_code is not None:
            raise ValueError("successful teardown cannot contain error_code")
        return _record(self._ledger, key, digest, result, TeardownResultV1)


def _validate_adapter_result(
    prepared: PreparedLaunch,
    result: AdapterCommitResultV1,
    operation_id: str,
) -> None:
    expected = prepared.portable
    mismatches = []
    if result.launch_id != expected.launch_id:
        mismatches.append("launch_id")
    if result.commit_operation_id != operation_id:
        mismatches.append("commit_operation_id")
    if result.binding_digest != expected.binding_digest:
        mismatches.append("binding_digest")
    if result.adapter_kind != expected.adapter_kind:
        mismatches.append("adapter_kind")
    if mismatches:
        raise ValueError("adapter result conflicts with prepared launch: " + ", ".join(mismatches))


def _validate_recovery(request: RecoverLaunchRequest, result: RecoveryResultV1) -> None:
    expected_digest = portable_digest(request.receipt.workspace_binding)
    if result.launch_id != request.receipt.launch_id:
        raise ValueError("recovery result conflicts with launch identity")
    if result.recover_operation_id != request.operation_id:
        raise ValueError("recovery result conflicts with operation identity")
    if result.binding_digest != expected_digest:
        raise ValueError("recovery result conflicts with workspace binding")
    if result.previous_generation != request.receipt.generation:
        raise ValueError("recovery result conflicts with previous generation")
    if result.disposition == RecoveryDisposition.RELAUNCHED:
        if result.generation is None or result.previous_generation is None:
            raise ValueError("relaunch requires old and new generations")
        if result.generation <= result.previous_generation:
            raise ValueError("relaunch must advance generation")
    elif result.disposition in {RecoveryDisposition.ADOPTED, RecoveryDisposition.UNCHANGED}:
        if result.generation != result.previous_generation:
            raise ValueError("adoption cannot change generation")
    if result.disposition == RecoveryDisposition.REFUSED:
        if result.error_code is None:
            raise ValueError("refused recovery requires error_code")
    elif result.error_code is not None:
        raise ValueError("successful recovery cannot contain error_code")


def _digest_fields(**values: object) -> str:
    payload: dict[str, object] = {}
    for key, value in values.items():
        if isinstance(value, BaseModel):
            payload[key] = value.model_dump(mode="json")
        else:
            payload[key] = value
    import hashlib
    import json

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _replay(
    ledger: LaunchLedger,
    key: OperationKey,
    digest: str,
    result_type: type[ResultT],
) -> ResultT | None:
    stored = ledger.get_operation(key)
    if stored is None:
        return None
    if stored.input_digest != digest:
        raise OperationConflict(
            f"operation_conflict: {key.launch_id}/{key.phase.value}/{key.operation_id}"
        )
    if not isinstance(stored.result, result_type):
        raise TypeError("stored operation result has the wrong type")
    return cast(ResultT, stored.result)


def _record(
    ledger: LaunchLedger,
    key: OperationKey,
    digest: str,
    result: ResultT,
    result_type: type[ResultT],
) -> ResultT:
    stored = ledger.record_operation(StoredOperation(key, digest, result))
    if stored.input_digest != digest:
        raise OperationConflict(
            f"operation_conflict: {key.launch_id}/{key.phase.value}/{key.operation_id}"
        )
    if not isinstance(stored.result, result_type):
        raise TypeError("stored operation result has the wrong type")
    return cast(ResultT, stored.result)
