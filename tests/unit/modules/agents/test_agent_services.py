"""In-memory application-service tests with no root runtime fixtures."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from beadhive.modules.agents.application.services import (
    AbortLaunchService,
    CommitLaunchService,
    ObserveLaunchService,
    OperationConflict,
    PrepareLaunchService,
    RecoverLaunchService,
    TeardownLaunchService,
    TerminalStateConflict,
)
from beadhive.modules.agents.contracts.ports import OperationKey, StoredOperation
from beadhive.modules.agents.domain.profile import AgentLaunchProfile
from beadhive.modules.agents.domain.transaction import (
    AbortLaunchRequest,
    AdapterCommitResultV1,
    AgentObservationV1,
    BoundWorkspace,
    CommitLaunchRequest,
    CompensationResult,
    ObserveLaunchRequest,
    PreparedLaunch,
    PrepareLaunchRequest,
    RecoverLaunchRequest,
    RecoveryResultV1,
    TeardownLaunchRequest,
    TeardownResultV1,
    WorkspaceBindingV1,
    WorkspaceTargetV1,
    portable_digest,
)


class MemoryLedger:
    def __init__(self) -> None:
        self.operations: dict[OperationKey, StoredOperation] = {}
        self.terminals = {}
        self.lock_entries = []

    @contextmanager
    def lock(self, _launch_id):
        self.lock_entries.append(_launch_id)
        yield

    def get_operation(self, key):
        return self.operations.get(key)

    def record_operation(self, operation):
        return self.operations.setdefault(operation.key, operation)

    def get_terminal(self, launch_id):
        return self.terminals.get(launch_id)

    def record_terminal(self, receipt):
        return self.terminals.setdefault(receipt.launch_id, receipt)


class MemoryBinder:
    def __init__(self) -> None:
        self.calls = 0

    def bind(self, target, profile, *, launch_id):
        self.calls += 1
        assert profile.bead == target.requested_bead
        return BoundWorkspace(
            binding=WorkspaceBindingV1(
                **target.model_dump(exclude={"type", "version"}),
                branch=f"wt/bead/issue/{target.requested_bead}",
                worktree_id="opaque-worktree",
            ),
            adapter_action="allocate",
            adapter_plan={"launch_id": launch_id, "strategy": "fresh"},
            local_capability={"private_root": "/not-portable"},
        )


class MemoryLauncher:
    def __init__(self, outcome="committed") -> None:
        self.outcome = outcome
        self.commits = 0
        self.compensations = 0

    def commit(self, prepared, *, operation_id):
        self.commits += 1
        return AdapterCommitResultV1(
            launch_id=prepared.portable.launch_id,
            commit_operation_id=operation_id,
            binding_digest=prepared.portable.binding_digest,
            adapter_kind=prepared.portable.adapter_kind,
            outcome=self.outcome,
            allocation_id="allocation-1",
            generation=1,
            error_code=None if self.outcome == "committed" else "adapter_failed",
        )

    def compensate(self, _prepared, result):
        self.compensations += 1
        return CompensationResult(
            status="completed",
            allocation_id=result.allocation_id if result is not None else None,
        )


def _request(*, operation_id="prepare-1", adapter_kind="memory") -> PrepareLaunchRequest:
    return PrepareLaunchRequest(
        profile=AgentLaunchProfile(
            managed_bead=True,
            bead="bh-agents.2",
            initial_seat="developer",
            harness="codex",
        ),
        target=WorkspaceTargetV1(
            hive_id="github/beadhive/beadhive",
            binding_kind="managed_bead",
            requested_bead="bh-agents.2",
            parent_launch_id="parent-1",
        ),
        launch_id="launch-1",
        operation_id=operation_id,
        adapter_kind=adapter_kind,
    )


def _prepare(binder=None, ledger=None):
    binder = binder or MemoryBinder()
    ledger = ledger or MemoryLedger()
    return PrepareLaunchService(binder, ledger).execute(_request()), binder, ledger


def _committed_receipt():
    prepared, _, ledger = _prepare()
    launcher = MemoryLauncher()
    result = CommitLaunchService(launcher, ledger).execute(
        CommitLaunchRequest(prepared=prepared, operation_id="commit-1")
    )
    assert result.receipt is not None
    return result.receipt, ledger


def _assert_no_effects(ledger, launcher) -> None:
    assert ledger.lock_entries == []
    assert ledger.operations == {}
    assert ledger.terminals == {}
    assert launcher.commits == 0
    assert launcher.compensations == 0


def test_prepare_replay_is_a_read_and_conflicting_input_fails_closed() -> None:
    binder = MemoryBinder()
    ledger = MemoryLedger()
    service = PrepareLaunchService(binder, ledger)
    first = service.execute(_request())
    replay = service.execute(_request())
    assert replay is first
    assert first.portable == replay.portable
    assert binder.calls == 1
    with pytest.raises(OperationConflict, match="operation_conflict"):
        service.execute(_request(adapter_kind="different"))
    assert binder.calls == 1
    assert ledger.lock_entries == ["launch-1", "launch-1", "launch-1"]


def test_prepare_request_refuses_profile_workspace_identity_mismatch() -> None:
    with pytest.raises(ValidationError, match="managed bead"):
        PrepareLaunchRequest(
            profile=_request().profile,
            target=WorkspaceTargetV1(
                hive_id="github/beadhive/beadhive",
                binding_kind="managed_bead",
                requested_bead="bh-other.1",
            ),
            launch_id="launch-1",
            operation_id="prepare-1",
            adapter_kind="memory",
        )


def test_profile_resolves_before_workspace_mutation() -> None:
    binder = MemoryBinder()
    ledger = MemoryLedger()
    request = _request().model_copy(
        update={
            "profile": AgentLaunchProfile(
                managed_bead=True,
                bead="bh-agents.2",
                initial_seat="developer",
                harness="codex",
                model="--unsafe",
            )
        }
    )
    with pytest.raises(ValueError, match="invalid model"):
        PrepareLaunchService(binder, ledger).execute(request)
    assert binder.calls == 0


def test_commit_replay_does_not_repeat_adapter_effect() -> None:
    prepared, _, ledger = _prepare()
    launcher = MemoryLauncher()
    service = CommitLaunchService(launcher, ledger)
    request = CommitLaunchRequest(prepared=prepared, operation_id="commit-1")
    first = service.execute(request)
    replay = service.execute(request)
    assert first == replay and first.receipt is not None
    assert launcher.commits == 1
    assert ledger.get_terminal("launch-1") == first.receipt


def test_blank_commit_operation_is_rejected_before_any_effect() -> None:
    prepared, _, _ = _prepare()
    with pytest.raises(ValueError, match="operation_id"):
        CommitLaunchRequest(prepared=prepared, operation_id="  ")
    request = CommitLaunchRequest(prepared=prepared, operation_id="commit-1")
    object.__setattr__(request, "operation_id", "  ")
    ledger = MemoryLedger()
    launcher = MemoryLauncher()

    with pytest.raises(ValueError, match="operation_id"):
        CommitLaunchService(launcher, ledger).execute(request)

    _assert_no_effects(ledger, launcher)


def test_changed_adapter_plan_is_rejected_before_any_effect() -> None:
    prepared, _, _ = _prepare()
    with pytest.raises(ValueError, match="adapter_plan_digest"):
        PreparedLaunch(
            portable=prepared.portable,
            resolved_profile=prepared.resolved_profile,
            adapter_plan={"strategy": "different-at-construction"},
            local_capability=prepared.local_capability,
        )
    assert isinstance(prepared.adapter_plan, dict)
    prepared.adapter_plan["strategy"] = "changed-after-prepare"
    request = object.__new__(CommitLaunchRequest)
    object.__setattr__(request, "prepared", prepared)
    object.__setattr__(request, "operation_id", "commit-1")
    ledger = MemoryLedger()
    launcher = MemoryLauncher()

    with pytest.raises(ValueError, match="adapter_plan_digest"):
        CommitLaunchService(launcher, ledger).execute(request)

    _assert_no_effects(ledger, launcher)


def test_resolved_profile_receipt_mismatch_is_rejected_before_any_effect() -> None:
    prepared, _, _ = _prepare()
    conflicting = prepared.resolved_profile.model_copy(update={"model": "different-model"})
    with pytest.raises(ValueError, match="resolved_profile"):
        PreparedLaunch(
            portable=prepared.portable,
            resolved_profile=conflicting,
            adapter_plan=prepared.adapter_plan,
            local_capability=prepared.local_capability,
        )
    object.__setattr__(prepared, "resolved_profile", conflicting)
    request = object.__new__(CommitLaunchRequest)
    object.__setattr__(request, "prepared", prepared)
    object.__setattr__(request, "operation_id", "commit-1")
    ledger = MemoryLedger()
    launcher = MemoryLauncher()

    with pytest.raises(ValueError, match="resolved_profile"):
        CommitLaunchService(launcher, ledger).execute(request)

    _assert_no_effects(ledger, launcher)


def test_failed_commit_aborts_and_compensates_exactly_once() -> None:
    prepared, _, ledger = _prepare()
    launcher = MemoryLauncher(outcome="failed")
    failed = CommitLaunchService(launcher, ledger).execute(
        CommitLaunchRequest(prepared=prepared, operation_id="commit-1")
    )
    assert failed.receipt is None
    service = AbortLaunchService(launcher, ledger)
    request = AbortLaunchRequest(
        prepared=prepared,
        operation_id="abort-1",
        reason_code="adapter_failed",
        failed_result=failed.adapter_result,
        failed_operation_id="commit-1",
    )
    receipt = service.execute(request)
    replay = service.execute(request)
    assert receipt == replay
    assert receipt.compensation == "completed"
    assert launcher.compensations == 1
    with pytest.raises(TerminalStateConflict, match="aborted"):
        CommitLaunchService(launcher, ledger).execute(
            CommitLaunchRequest(prepared=prepared, operation_id="commit-2")
        )


def test_blank_abort_reason_is_rejected_before_any_effect() -> None:
    prepared, _, _ = _prepare()
    with pytest.raises(ValueError, match="reason_code"):
        AbortLaunchRequest(
            prepared=prepared,
            operation_id="abort-1",
            reason_code="\t",
        )
    request = AbortLaunchRequest(
        prepared=prepared,
        operation_id="abort-1",
        reason_code="adapter_failed",
    )
    object.__setattr__(request, "reason_code", "\t")
    ledger = MemoryLedger()
    launcher = MemoryLauncher()

    with pytest.raises(ValueError, match="reason_code"):
        AbortLaunchService(launcher, ledger).execute(request)

    _assert_no_effects(ledger, launcher)


def test_observe_validates_launch_binding_and_generation() -> None:
    receipt, _ = _committed_receipt()

    class Observer:
        def observe(self, observed_receipt):
            return AgentObservationV1(
                launch_id=observed_receipt.launch_id,
                binding_digest=portable_digest(observed_receipt.workspace_binding),
                status="live",
                generation=observed_receipt.generation,
                allocation_id=observed_receipt.allocation_id,
            )

    assert (
        ObserveLaunchService(Observer()).execute(ObserveLaunchRequest(receipt=receipt)).status
        == "live"
    )


def test_recover_advances_generation_once_and_replays() -> None:
    receipt, ledger = _committed_receipt()

    class Recovery:
        calls = 0

        def recover(self, request):
            self.calls += 1
            return RecoveryResultV1(
                launch_id=request.receipt.launch_id,
                recover_operation_id=request.operation_id,
                binding_digest=portable_digest(request.receipt.workspace_binding),
                disposition="relaunched",
                previous_generation=request.receipt.generation,
                generation=(request.receipt.generation or 0) + 1,
                allocation_id="allocation-2",
            )

    recovery = Recovery()
    service = RecoverLaunchService(recovery, ledger)
    request = RecoverLaunchRequest(
        receipt=receipt,
        observation=AgentObservationV1(
            launch_id=receipt.launch_id,
            binding_digest=portable_digest(receipt.workspace_binding),
            status="missing",
            generation=receipt.generation,
            allocation_id=receipt.allocation_id,
        ),
        operation_id="recover-1",
    )
    assert service.execute(request).generation == 2
    assert service.execute(request).generation == 2
    assert recovery.calls == 1

    with pytest.raises(ValidationError, match="generation"):
        RecoverLaunchRequest(
            receipt=receipt,
            observation=request.observation.model_copy(update={"generation": 2}),
            operation_id="recover-stale",
        )
    assert recovery.calls == 1


def test_teardown_is_generation_fenced_and_idempotent() -> None:
    receipt, ledger = _committed_receipt()

    class Teardown:
        calls = 0

        def teardown(self, request):
            self.calls += 1
            return TeardownResultV1(
                launch_id=request.receipt.launch_id,
                teardown_operation_id=request.operation_id,
                binding_digest=portable_digest(request.receipt.workspace_binding),
                generation=request.receipt.generation,
                disposition="torn_down",
            )

    teardown = Teardown()
    service = TeardownLaunchService(teardown, ledger)
    request = TeardownLaunchRequest(receipt=receipt, operation_id="teardown-1")
    assert service.execute(request).disposition == "torn_down"
    assert service.execute(request).disposition == "torn_down"
    assert teardown.calls == 1
