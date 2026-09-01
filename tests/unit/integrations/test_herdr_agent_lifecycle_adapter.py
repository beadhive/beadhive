"""Contract and generation-fence tests for the Herdr agent lifecycle adapter."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from beadhive.herdr_launch_profile import (
    HerdrAgentLaunchProfile,
    HerdrPaneCreateTarget,
    resolve_herdr_launch_profile,
)
from beadhive.integrations.herdr import (
    HERDR_AGENT_SESSION,
    HERDR_AGENT_SESSION_KEY,
    HerdrAgentAdapter,
    HerdrCommitCommand,
    HerdrCommitEvidence,
    HerdrObservationEvidence,
    HerdrProductionResolver,
    HerdrRecoveryEvidence,
    HerdrTeardownEvidence,
    herdr_agent_provider_binding,
    herdr_bound_workspace,
)
from beadhive.integrations.herdr import cli_application as herdr_application
from beadhive.integrations.herdr.transport_types import FailureCode, HerdrResult
from beadhive.kernel.plugins import (
    CapabilitySelection,
    DiscoveryResult,
    bind_application_port,
)
from beadhive.modules.agents import (
    AbortLaunchRequest,
    AbortLaunchService,
    AgentLaunchProfile,
    AgentObservationV1,
    CommitLaunchRequest,
    CommitLaunchService,
    ObserveLaunchService,
    OperationKey,
    PrepareLaunchRequest,
    PrepareLaunchService,
    RecoverLaunchRequest,
    RecoverLaunchService,
    RecoveryResultV1,
    StoredOperation,
    TeardownLaunchRequest,
    TeardownLaunchService,
    WorkspaceBindingV1,
    WorkspaceTargetV1,
    portable_digest,
)
from beadhive.modules.agents.domain.transaction import ObserveLaunchRequest


class MemoryLedger:
    def __init__(self) -> None:
        self.operations: dict[OperationKey, StoredOperation] = {}
        self.terminals = {}

    @contextmanager
    def lock(self, _launch_id):
        yield

    def get_operation(self, key):
        return self.operations.get(key)

    def record_operation(self, operation):
        return self.operations.setdefault(operation.key, operation)

    def get_terminal(self, launch_id):
        return self.terminals.get(launch_id)

    def record_terminal(self, receipt):
        return self.terminals.setdefault(receipt.launch_id, receipt)


class Runtime:
    def __init__(self, *, created: bool = True) -> None:
        self.created = created
        self.commits = []
        self.teardowns = []
        self.observations = []
        self.recoveries = []
        self.commit_failure = None
        self.teardown_failure = None
        self.identity_override = None

    def commit(self, command):
        self.commits.append(command)
        if self.commit_failure is not None:
            return HerdrResult(failure=self.commit_failure)
        evidence = HerdrCommitEvidence(
            command.launch_id,
            command.operation_id,
            command.target,
            "pane-created" if self.created else "pane-reused",
            command.generation,
            "created" if self.created else "reused",
        )
        return HerdrResult.ok(self.identity_override or evidence)

    def observe(self, receipt):
        self.observations.append(receipt)
        return HerdrResult.ok(
            HerdrObservationEvidence(
                receipt.launch_id,
                receipt.allocation_id,
                receipt.generation,
                "live",
            )
        )

    def recover(self, request):
        self.recoveries.append(request)
        generation = (request.receipt.generation or 0) + 1
        return HerdrResult.ok(
            HerdrRecoveryEvidence(
                request.receipt.launch_id,
                request.operation_id,
                "pane-recovered",
                request.receipt.generation,
                generation,
                "relaunched",
            )
        )

    def teardown(self, request):
        self.teardowns.append(request)
        if self.teardown_failure is not None:
            return HerdrResult(failure=self.teardown_failure)
        return HerdrResult.ok(
            HerdrTeardownEvidence(
                request.receipt.launch_id,
                request.operation_id,
                request.receipt.allocation_id,
                request.receipt.generation,
                "torn_down",
            )
        )


class Binder:
    def __init__(self, runtime, *, action="allocate") -> None:
        self.runtime = runtime
        self.action = action

    def bind(self, target, _profile, *, launch_id):
        binding = WorkspaceBindingV1(
            **target.model_dump(exclude={"type", "version"}),
            branch="wt/bead/issue/bh-agents.4",
            worktree_id="opaque-worktree",
        )
        return herdr_bound_workspace(
            binding,
            self.runtime,
            target=f"bh-{launch_id}",
            generation=1,
            action=self.action,
        )


def _prepare(runtime=None, *, action="allocate"):
    runtime = runtime or Runtime(created=action == "allocate")
    ledger = MemoryLedger()
    prepared = PrepareLaunchService(Binder(runtime, action=action), ledger).execute(
        PrepareLaunchRequest(
            profile=AgentLaunchProfile(
                managed_bead=True,
                bead="bh-agents.4",
                initial_seat="developer",
                harness="codex",
            ),
            target=WorkspaceTargetV1(
                hive_id="github/beadhive/beadhive",
                binding_kind="managed_bead",
                requested_bead="bh-agents.4",
            ),
            launch_id="launch-1",
            operation_id="prepare-1",
            adapter_kind="herdr",
        )
    )
    return prepared, runtime, ledger


def _commit(runtime=None, *, action="allocate"):
    prepared, runtime, ledger = _prepare(runtime, action=action)
    adapter = HerdrAgentAdapter()
    result = CommitLaunchService(adapter, ledger).execute(
        CommitLaunchRequest(prepared=prepared, operation_id="commit-1")
    )
    return prepared, runtime, ledger, adapter, result


def test_manifest_capability_binds_the_combined_typed_agent_port() -> None:
    adapter = HerdrAgentAdapter()
    discovery = DiscoveryResult(
        (),
        (CapabilitySelection(HERDR_AGENT_SESSION, "herdr"),),
        (),
    )
    bound = bind_application_port(
        HERDR_AGENT_SESSION_KEY,
        discovery,
        (herdr_agent_provider_binding(adapter),),
    )
    assert bound is adapter


def test_core_prepare_and_commit_produce_only_redacted_portable_evidence() -> None:
    _prepared, runtime, _ledger, _adapter, result = _commit()
    assert result.receipt is not None
    assert len(runtime.commits) == 1
    payload = result.adapter_result.model_dump_json()
    for secret in ("/tmp/checkout", "--dangerous", "TOKEN=secret", "credential"):
        assert secret not in payload
    assert result.adapter_result.portable_receipt == {
        "type": "beadhive.herdr-adapter",
        "version": "1",
        "disposition": "created",
        "target": "bh-launch-1",
        "generation": 1,
    }


def test_commit_failure_is_typed_and_never_exposes_provider_detail() -> None:
    runtime = Runtime()
    runtime.commit_failure = HerdrResult.fail(
        FailureCode.RETRYABLE,
        "credential=provider-secret",
        detail="argv=/host/private",
    ).failure
    _prepared, _runtime, _ledger, _adapter, result = _commit(runtime)
    assert result.receipt is None
    assert result.adapter_result.outcome == "failed"
    assert result.adapter_result.error_code == "herdr_retryable"
    assert "provider-secret" not in result.adapter_result.model_dump_json()


def test_conflicting_created_identity_compensates_only_that_allocation() -> None:
    runtime = Runtime()
    runtime.identity_override = HerdrCommitEvidence(
        "other-launch",
        "commit-1",
        "bh-launch-1",
        "pane-created",
        1,
        "created",
    )
    _prepared, _runtime, _ledger, _adapter, result = _commit(runtime)
    assert result.receipt is None
    assert result.adapter_result.error_code == "herdr_identity_conflict"
    assert result.adapter_result.allocation_id is None
    assert [item.receipt.allocation_id for item in runtime.teardowns] == ["pane-created"]


def test_abort_compensates_created_generation_once_but_never_reused_provider_state() -> None:
    prepared, runtime, ledger = _prepare()
    adapter = HerdrAgentAdapter()
    failed = replace(
        Runtime().commit(_commit_command(prepared)).unwrap(),
        launch_id="wrong",
    )
    runtime.identity_override = failed
    commit = CommitLaunchService(adapter, ledger).execute(
        CommitLaunchRequest(prepared=prepared, operation_id="commit-1")
    )
    request = AbortLaunchRequest(
        prepared=prepared,
        operation_id="abort-1",
        reason_code="adapter_failed",
        failed_result=commit.adapter_result,
        failed_operation_id="commit-1",
    )
    AbortLaunchService(adapter, ledger).execute(request)
    AbortLaunchService(adapter, ledger).execute(request)
    assert len(runtime.teardowns) == 1

    reused, reused_runtime, reused_ledger = _prepare(Runtime(created=False), action="reuse")
    reused_adapter = HerdrAgentAdapter()
    refused = CommitLaunchService(reused_adapter, reused_ledger).execute(
        CommitLaunchRequest(prepared=reused, operation_id="commit-2")
    )
    assert refused.receipt is not None
    assert reused_adapter.compensate(reused, refused.adapter_result).status == "not_needed"
    assert reused_runtime.teardowns == []


def test_allocate_race_may_adopt_winner_but_never_compensates_reused_state() -> None:
    prepared, runtime, ledger = _prepare(Runtime(created=False), action="allocate")
    adapter = HerdrAgentAdapter()
    committed = CommitLaunchService(adapter, ledger).execute(
        CommitLaunchRequest(prepared=prepared, operation_id="commit-race")
    )

    assert committed.receipt is not None
    assert committed.adapter_result.portable_receipt["disposition"] == "reused"
    assert adapter.compensate(prepared, committed.adapter_result).status == "not_needed"
    assert runtime.teardowns == []


def test_observe_recover_and_teardown_use_one_exact_generation_fence() -> None:
    _prepared, runtime, ledger, adapter, committed = _commit()
    receipt = committed.receipt
    assert receipt is not None
    observed = ObserveLaunchService(adapter).execute(ObserveLaunchRequest(receipt=receipt))
    assert observed.status == "live"

    missing = AgentObservationV1(
        launch_id=receipt.launch_id,
        binding_digest=portable_digest(receipt.workspace_binding),
        status="missing",
        generation=receipt.generation,
        allocation_id=receipt.allocation_id,
    )
    recovered = RecoverLaunchService(adapter, ledger).execute(
        RecoverLaunchRequest(
            receipt=receipt,
            observation=missing,
            operation_id="recover-1",
        )
    )
    assert recovered.previous_generation == 1
    assert recovered.generation == 2

    torn_down = TeardownLaunchService(adapter, ledger).execute(
        TeardownLaunchRequest(receipt=receipt, operation_id="teardown-1")
    )
    assert torn_down.disposition == "torn_down"
    assert runtime.teardowns[-1].receipt.generation == 1


def test_restart_resolver_rehydrates_runtime_from_only_portable_receipt() -> None:
    _prepared, _runtime, _ledger, _adapter, committed = _commit()
    receipt = committed.receipt
    assert receipt is not None
    resumed_runtime = Runtime()
    resolved = []
    adapter = HerdrAgentAdapter(
        HerdrProductionResolver(lambda portable: resolved.append(portable) or resumed_runtime)
    )

    observed = adapter.observe(receipt)

    assert observed.status == "live"
    assert resolved == [receipt]
    assert resumed_runtime.observations == [receipt]


def test_production_resolver_uses_receipt_session_and_exact_generation(monkeypatch) -> None:
    _prepared, _runtime, _ledger, _adapter, committed = _commit()
    assert committed.receipt is not None
    receipt = committed.receipt.model_copy(
        update={
            "adapter_receipt": {
                "type": "beadhive.herdr-adapter",
                "version": "1",
                "session": "session-a",
                "target": "bh-launch-1",
                "workspace_id": "workspace-1",
                "source_revision": "revision-1",
                "generation": 1,
                "generation_fenced": True,
            }
        }
    )
    snapshot = {
        "session": "session-a",
        "revision": "revision-2",
        "agents": [
            {
                "name": "bh-launch-1",
                "state": "idle",
                "pane_id": receipt.allocation_id,
            }
        ],
        "panes": [
            {
                "pane_id": receipt.allocation_id,
                "workspace_id": "workspace-1",
                "tokens": {"bh_generation": "1"},
            }
        ],
        "workspaces": [{"workspace_id": "workspace-1"}],
    }
    calls = []

    def invoke(argv, **_kwargs):
        calls.append(argv)
        payload = snapshot if argv[-2:] == ["api", "snapshot"] else {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(herdr_application, "_invoke", invoke)
    runtime = herdr_application.production_runtime_for_receipt(receipt)

    assert runtime.observe(receipt).unwrap().status == "live"
    torn_down = runtime.teardown(
        TeardownLaunchRequest(receipt=receipt, operation_id="teardown-restart")
    )
    assert torn_down.unwrap().disposition == "torn_down"
    assert all(call[1:3] == ["--session", "session-a"] for call in calls)


def test_production_resolver_fails_cross_session_and_stale_generation_before_effect(
    monkeypatch,
) -> None:
    _prepared, _runtime, _ledger, _adapter, committed = _commit()
    assert committed.receipt is not None
    receipt = committed.receipt.model_copy(
        update={
            "adapter_receipt": {
                "type": "beadhive.herdr-adapter",
                "version": "1",
                "session": "session-a",
                "target": "bh-launch-1",
                "workspace_id": "workspace-1",
                "source_revision": "revision-1",
                "generation": 1,
                "generation_fenced": True,
            }
        }
    )
    snapshot = {
        "session": "session-a",
        "revision": "revision-2",
        "agents": [{"name": "bh-launch-1", "state": "idle", "pane_id": receipt.allocation_id}],
        "panes": [
            {
                "pane_id": receipt.allocation_id,
                "workspace_id": "workspace-1",
                "tokens": {"bh_generation": "2"},
            }
        ],
        "workspaces": [{"workspace_id": "workspace-1"}],
    }
    effects = []

    def invoke(argv, **_kwargs):
        if argv[-2:] == ["pane", "close"]:
            effects.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(snapshot), stderr="")

    monkeypatch.setattr(herdr_application, "_invoke", invoke)
    runtime = herdr_application.production_runtime_for_receipt(receipt)
    stale = runtime.teardown(TeardownLaunchRequest(receipt=receipt, operation_id="teardown-stale"))
    assert stale.failure is not None
    assert effects == []

    snapshot["session"] = "session-b"
    crossed = runtime.teardown(
        TeardownLaunchRequest(receipt=receipt, operation_id="teardown-cross-session")
    )
    assert crossed.failure is not None
    assert effects == []


def test_production_resolver_rejects_cross_generation_receipt_before_source_access(
    monkeypatch,
) -> None:
    _prepared, _runtime, _ledger, _adapter, committed = _commit()
    assert committed.receipt is not None
    receipt = committed.receipt.model_copy(
        update={
            "adapter_receipt": {
                "type": "beadhive.herdr-adapter",
                "version": "1",
                "session": "session-a",
                "target": "bh-launch-1",
                "workspace_id": "workspace-1",
                "source_revision": "revision-1",
                "generation": 2,
                "generation_fenced": True,
            }
        }
    )
    calls = []
    monkeypatch.setattr(herdr_application, "_invoke", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(ValueError, match="generation conflicts"):
        herdr_application.production_runtime_for_receipt(receipt)

    assert calls == []


def test_fresh_generation_recovery_crosses_bound_port_services_without_provider_fallback(
    monkeypatch,
) -> None:
    profile = HerdrAgentLaunchProfile.model_validate(
        {
            "managed_bead": True,
            "bead": "widget-1",
            "initial_seat": "developer",
            "harness": "codex",
            "herdr_session": "session-a",
            "space_id": "workspace-1",
            "space_revision": "revision-1",
            "pane_id": "pane-old",
            "launch_id": "launch-a",
            "operation_id": "operation-a",
            "generation": 1,
        }
    )
    resolved, _receipt = resolve_herdr_launch_profile(profile)
    monkeypatch.setattr(herdr_application, "HerdrPaneCreateTarget", HerdrPaneCreateTarget)
    monkeypatch.setattr(
        herdr_application, "resolve_herdr_launch_profile", resolve_herdr_launch_profile
    )
    calls = []

    class RecoveryPort:
        def observe(self, receipt):
            calls.append(("observe", receipt))
            return AgentObservationV1(
                launch_id=receipt.launch_id,
                binding_digest=portable_digest(receipt.workspace_binding),
                status="missing",
                generation=receipt.generation,
                allocation_id=receipt.allocation_id,
            )

        def recover(self, request):
            calls.append(("recover", request))
            return RecoveryResultV1(
                launch_id=request.receipt.launch_id,
                recover_operation_id=request.operation_id,
                binding_digest=portable_digest(request.receipt.workspace_binding),
                disposition="relaunched",
                previous_generation=request.receipt.generation,
                generation=(request.receipt.generation or 0) + 1,
            )

        def __getattr__(self, name):
            raise AssertionError(f"unexpected direct provider fallback: {name}")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recovery must not call a provider source outside the bound port")

    monkeypatch.setattr(herdr_application, "_session_snapshot_for", forbidden)
    monkeypatch.setattr(herdr_application, "_invoke", forbidden)
    monkeypatch.setattr(herdr_application, "_command", forbidden)
    snapshot = {
        "session": "session-a",
        "revision": "revision-2",
        "agents": [],
        "panes": [{"pane_id": "pane-anchor", "workspace_id": "workspace-1"}],
        "workspaces": [{"workspace_id": "workspace-1"}],
    }

    with herdr_application.agent_session_scope(RecoveryPort()):
        recovered, _resolved = herdr_application._recover_profile_through_port(
            profile,
            resolved,
            snapshot,
            "pane-anchor",
            hive_id="github/acme/widgets",
        )

    assert recovered.generation == 2
    assert recovered.operation_id == "operation-a:recovery:2"
    assert recovered.pane_id is None
    assert [name for name, _value in calls] == ["observe", "recover"]


def test_restart_resolver_rehydrates_local_capability_without_portable_authority() -> None:
    _prepared, runtime, _ledger, _adapter, committed = _commit()
    receipt = committed.receipt
    assert receipt is not None

    class Resolver:
        calls = 0

        def resolve(self, resolved_receipt):
            self.calls += 1
            assert resolved_receipt == receipt
            return runtime

    resolver = Resolver()
    restarted = HerdrAgentAdapter(resolver)
    assert restarted.observe(receipt).status == "live"
    assert restarted.observe(receipt).status == "live"
    assert resolver.calls == 1


def test_invalid_or_mutated_plan_fails_before_any_provider_effect() -> None:
    prepared, runtime, _ledger = _prepare()
    object.__setattr__(prepared, "adapter_plan", {**prepared.adapter_plan, "cwd": "/secret"})
    with pytest.raises(ValueError, match="adapter_plan_digest"):
        HerdrAgentAdapter().commit(prepared, operation_id="commit-1")
    assert runtime.commits == []


def _commit_command(prepared):
    plan = prepared.adapter_plan
    return HerdrCommitCommand(
        prepared.portable.launch_id,
        "commit-1",
        str(plan["target"]),
        int(plan["generation"]),
        str(plan["action"]),
    )
