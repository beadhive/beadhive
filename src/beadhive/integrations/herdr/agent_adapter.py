"""Herdr implementation of the provider-neutral agent lifecycle ports.

The adapter translates between core launch transactions and one injected Herdr runtime.  It
never imports Beadhive work authority, CLI presentation, configuration, or provider process
details.  Those effects are prepared by the composition root and represented here by an opaque
runtime plus a redacted, digest-fenced plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from beadhive.kernel.plugins import (
    CapabilityKey,
    CapabilityRef,
    ProviderBinding,
    ProviderKey,
)
from beadhive.modules.agents.contracts import (
    AgentLauncher,
    AgentObserver,
    AgentRecovery,
    AgentTeardown,
)
from beadhive.modules.agents.domain import (
    AdapterCommitResultV1,
    AgentObservationV1,
    BoundWorkspace,
    CompensationResult,
    LaunchReceiptV1,
    ObservationStatus,
    PreparedLaunch,
    RecoverLaunchRequest,
    RecoveryDisposition,
    RecoveryResultV1,
    TeardownDisposition,
    TeardownLaunchRequest,
    TeardownResultV1,
    WorkspaceBindingV1,
    portable_digest,
)

from .transport_types import FailureCode, HerdrResult

HERDR_AGENT_SESSION = CapabilityRef("agent.session", 1)
_ADAPTER_KIND = "herdr"
_PLAN_FIELDS = frozenset({"provider", "target", "generation", "action"})


@dataclass(frozen=True, slots=True)
class HerdrCommitCommand:
    launch_id: str
    operation_id: str
    target: str
    generation: int
    action: str


@dataclass(frozen=True, slots=True)
class HerdrCommitEvidence:
    launch_id: str
    operation_id: str
    target: str
    allocation_id: str
    generation: int
    disposition: str
    session: str | None = None
    workspace_id: str | None = None
    source_revision: str | None = None
    generation_fenced: bool = False


@dataclass(frozen=True, slots=True)
class HerdrObservationEvidence:
    launch_id: str
    allocation_id: str | None
    generation: int | None
    status: ObservationStatus


@dataclass(frozen=True, slots=True)
class HerdrRecoveryEvidence:
    launch_id: str
    operation_id: str
    allocation_id: str | None
    previous_generation: int | None
    generation: int | None
    disposition: RecoveryDisposition


@dataclass(frozen=True, slots=True)
class HerdrTeardownEvidence:
    launch_id: str
    operation_id: str
    allocation_id: str | None
    generation: int | None
    disposition: TeardownDisposition


class HerdrAgentRuntime(Protocol):
    """Provider effects already scoped to an exact Herdr session.

    Implementations may retain host paths, argv, environment, credentials, and topology handles,
    but none of those values may cross into the evidence returned by this protocol.
    """

    def commit(self, command: HerdrCommitCommand) -> HerdrResult[HerdrCommitEvidence]: ...

    def observe(self, receipt: LaunchReceiptV1) -> HerdrResult[HerdrObservationEvidence]: ...

    def recover(self, request: RecoverLaunchRequest) -> HerdrResult[HerdrRecoveryEvidence]: ...

    def teardown(self, request: TeardownLaunchRequest) -> HerdrResult[HerdrTeardownEvidence]: ...


class HerdrRuntimeResolver(Protocol):
    """Rehydrate an exact local provider capability after process restart."""

    def resolve(self, receipt: LaunchReceiptV1) -> HerdrAgentRuntime: ...


@dataclass(frozen=True, slots=True)
class HerdrLocalCapability:
    """Opaque local runtime retained outside every portable transaction model."""

    runtime: HerdrAgentRuntime


@runtime_checkable
class HerdrAgentSessionPort(
    AgentLauncher,
    AgentObserver,
    AgentRecovery,
    AgentTeardown,
    Protocol,
):
    """Combined application port supplied by the ``agent.session@1`` capability."""


HERDR_AGENT_SESSION_KEY = CapabilityKey(HERDR_AGENT_SESSION, HerdrAgentSessionPort)


def herdr_bound_workspace(
    binding: WorkspaceBindingV1,
    runtime: HerdrAgentRuntime,
    *,
    target: str,
    generation: int,
    action: str,
) -> BoundWorkspace:
    """Build a redacted Herdr adapter plan after core workspace binding is complete."""

    if action not in {"allocate", "reuse", "none"}:
        raise ValueError("Herdr adapter action must be allocate, reuse, or none")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Herdr target must be non-empty")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("Herdr generation must be a positive integer")
    plan = MappingProxyType(
        {
            "provider": _ADAPTER_KIND,
            "target": target,
            "generation": generation,
            "action": action,
        }
    )
    return BoundWorkspace(
        binding=binding,
        adapter_action=action,
        adapter_plan=plan,
        local_capability=HerdrLocalCapability(runtime),
    )


class HerdrAgentAdapter:
    """Exact-correlation translation for Herdr launch lifecycle effects."""

    def __init__(self, resolver: HerdrRuntimeResolver | None = None) -> None:
        self._resolver = resolver
        self._runtimes: dict[tuple[str, str], HerdrAgentRuntime] = {}

    def commit(
        self,
        prepared: PreparedLaunch,
        *,
        operation_id: str,
    ) -> AdapterCommitResultV1:
        plan, runtime = _prepared_context(prepared)
        if plan["action"] == "none":
            return _commit_result(prepared, operation_id, outcome="committed")
        command = HerdrCommitCommand(
            prepared.portable.launch_id,
            operation_id,
            plan["target"],
            plan["generation"],
            plan["action"],
        )
        provider = runtime.commit(command)
        if provider.failure is not None:
            return _failed_commit(prepared, operation_id, provider.failure.code)
        evidence = provider.unwrap()
        mismatch = _commit_mismatch(command, evidence)
        if mismatch is not None:
            retained = evidence.allocation_id if evidence.disposition == "created" else None
            if retained is not None:
                cleanup = runtime.teardown(
                    TeardownLaunchRequest(
                        receipt=_temporary_receipt(prepared, operation_id, evidence),
                        operation_id=f"{operation_id}:compensate",
                    )
                )
                if cleanup.failure is None:
                    retained = None
            return _commit_result(
                prepared,
                operation_id,
                outcome="failed",
                allocation_id=retained,
                generation=evidence.generation,
                error_code="herdr_identity_conflict",
            )
        result = _commit_result(
            prepared,
            operation_id,
            outcome="committed",
            allocation_id=evidence.allocation_id,
            generation=evidence.generation,
            portable_receipt=_portable_provider_receipt(evidence),
        )
        self._remember(_temporary_receipt_from_result(prepared, result), runtime)
        return result

    def compensate(
        self,
        prepared: PreparedLaunch,
        result: AdapterCommitResultV1 | None,
    ) -> CompensationResult:
        plan, runtime = _prepared_context(prepared)
        disposition = (
            result.portable_receipt.get("disposition")
            if result is not None and result.portable_receipt is not None
            else None
        )
        if (
            plan["action"] != "allocate"
            or disposition != "created"
            or result is None
            or result.allocation_id is None
        ):
            return CompensationResult(status="not_needed")
        receipt = _temporary_receipt_from_result(prepared, result)
        cleanup = runtime.teardown(
            TeardownLaunchRequest(
                receipt=receipt,
                operation_id=f"{result.commit_operation_id}:compensate",
            )
        )
        if cleanup.failure is not None:
            return CompensationResult(status="failed", allocation_id=result.allocation_id)
        evidence = cleanup.unwrap()
        if not _teardown_matches(receipt, evidence):
            return CompensationResult(status="failed", allocation_id=result.allocation_id)
        if evidence.disposition not in {
            TeardownDisposition.TORN_DOWN,
            TeardownDisposition.ALREADY_ABSENT,
        }:
            return CompensationResult(status="failed", allocation_id=result.allocation_id)
        return CompensationResult(status="completed", allocation_id=result.allocation_id)

    def observe(self, receipt: LaunchReceiptV1) -> AgentObservationV1:
        runtime = self._runtime_for_receipt(receipt)
        observed = runtime.observe(receipt)
        if observed.failure is not None:
            return AgentObservationV1(
                launch_id=receipt.launch_id,
                binding_digest=portable_digest(receipt.workspace_binding),
                status="unknown",
                generation=receipt.generation,
                allocation_id=receipt.allocation_id,
            )
        evidence = observed.unwrap()
        if (
            evidence.launch_id != receipt.launch_id
            or evidence.allocation_id != receipt.allocation_id
            or evidence.generation != receipt.generation
        ):
            return AgentObservationV1(
                launch_id=receipt.launch_id,
                binding_digest=portable_digest(receipt.workspace_binding),
                status="stale",
                generation=receipt.generation,
                allocation_id=receipt.allocation_id,
            )
        return AgentObservationV1(
            launch_id=receipt.launch_id,
            binding_digest=portable_digest(receipt.workspace_binding),
            status=evidence.status,
            generation=evidence.generation,
            allocation_id=evidence.allocation_id,
        )

    def recover(self, request: RecoverLaunchRequest) -> RecoveryResultV1:
        runtime = self._runtime_for_receipt(request.receipt)
        recovered = runtime.recover(request)
        if recovered.failure is not None:
            return RecoveryResultV1(
                launch_id=request.receipt.launch_id,
                recover_operation_id=request.operation_id,
                binding_digest=portable_digest(request.receipt.workspace_binding),
                disposition="refused",
                previous_generation=request.receipt.generation,
                generation=request.receipt.generation,
                allocation_id=request.receipt.allocation_id,
                error_code=_error_code(recovered.failure.code),
            )
        evidence = recovered.unwrap()
        if (
            evidence.launch_id != request.receipt.launch_id
            or evidence.operation_id != request.operation_id
        ):
            return RecoveryResultV1(
                launch_id=request.receipt.launch_id,
                recover_operation_id=request.operation_id,
                binding_digest=portable_digest(request.receipt.workspace_binding),
                disposition="refused",
                previous_generation=request.receipt.generation,
                generation=request.receipt.generation,
                allocation_id=request.receipt.allocation_id,
                error_code="herdr_identity_conflict",
            )
        return RecoveryResultV1(
            launch_id=evidence.launch_id,
            recover_operation_id=evidence.operation_id,
            binding_digest=portable_digest(request.receipt.workspace_binding),
            disposition=evidence.disposition,
            previous_generation=evidence.previous_generation,
            generation=evidence.generation,
            allocation_id=evidence.allocation_id,
        )

    def teardown(self, request: TeardownLaunchRequest) -> TeardownResultV1:
        runtime = self._runtime_for_receipt(request.receipt)
        torn_down = runtime.teardown(request)
        if torn_down.failure is not None:
            return TeardownResultV1(
                launch_id=request.receipt.launch_id,
                teardown_operation_id=request.operation_id,
                binding_digest=portable_digest(request.receipt.workspace_binding),
                generation=request.receipt.generation,
                disposition="retained",
                error_code=_error_code(torn_down.failure.code),
            )
        evidence = torn_down.unwrap()
        if (
            not _teardown_matches(request.receipt, evidence)
            or evidence.operation_id != request.operation_id
        ):
            return TeardownResultV1(
                launch_id=request.receipt.launch_id,
                teardown_operation_id=request.operation_id,
                binding_digest=portable_digest(request.receipt.workspace_binding),
                generation=request.receipt.generation,
                disposition="refused",
                error_code="herdr_identity_conflict",
            )
        return TeardownResultV1(
            launch_id=evidence.launch_id,
            teardown_operation_id=evidence.operation_id,
            binding_digest=portable_digest(request.receipt.workspace_binding),
            generation=evidence.generation,
            disposition=evidence.disposition,
        )

    def _remember(self, receipt: LaunchReceiptV1, runtime: HerdrAgentRuntime) -> None:
        self._runtimes[_runtime_key(receipt)] = runtime

    def _runtime_for_receipt(self, receipt: LaunchReceiptV1) -> HerdrAgentRuntime:
        compatibility_action = (
            receipt.adapter_receipt is not None
            and receipt.adapter_receipt.get("compatibility_action") is True
        )
        if compatibility_action:
            if self._resolver is None:
                raise RuntimeError("Herdr local capability is unavailable for this exact launch")
            # Legacy CLI receipts are synthesized per command from the current proven roster.
            # Resolve their exact authority for that action; never retain a monkeypatchable or
            # mutable provider source beyond the command boundary.
            return self._resolver.resolve(receipt)
        runtime = self._runtimes.get(_runtime_key(receipt))
        if runtime is not None:
            return runtime
        if self._resolver is None:
            raise RuntimeError("Herdr local capability is unavailable for this exact launch")
        runtime = self._resolver.resolve(receipt)
        self._remember(receipt, runtime)
        return runtime


def herdr_agent_provider_binding(adapter: HerdrAgentAdapter) -> ProviderBinding[object]:
    """Register the adapter under the capability declared by the built-in manifest."""

    return ProviderBinding(ProviderKey("herdr", HERDR_AGENT_SESSION), adapter)


def _runtime_key(receipt: LaunchReceiptV1) -> tuple[str, str]:
    # A launch id and workspace binding are intentionally reusable across generations and
    # restart observations.  Cache only by the complete portable authority so a runtime
    # resolved for one session/topology/generation can never service a different receipt.
    return receipt.launch_id, portable_digest(receipt)


def _prepared_context(
    prepared: PreparedLaunch,
) -> tuple[dict[str, str | int], HerdrAgentRuntime]:
    prepared.validate()
    if prepared.portable.adapter_kind != _ADAPTER_KIND:
        raise ValueError("prepared launch is not owned by the Herdr adapter")
    if set(prepared.adapter_plan) != _PLAN_FIELDS:
        raise ValueError("Herdr adapter plan has undeclared or missing fields")
    plan = dict(prepared.adapter_plan)
    if plan.get("provider") != _ADAPTER_KIND:
        raise ValueError("Herdr adapter plan provider conflicts")
    if plan.get("action") != prepared.portable.adapter_action:
        raise ValueError("Herdr adapter plan action conflicts")
    target = plan.get("target")
    generation = plan.get("generation")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Herdr adapter target is invalid")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("Herdr adapter generation is invalid")
    if not isinstance(prepared.local_capability, HerdrLocalCapability):
        raise ValueError("Herdr local capability is unavailable")
    return {
        "provider": _ADAPTER_KIND,
        "target": target,
        "generation": generation,
        "action": str(plan["action"]),
    }, prepared.local_capability.runtime


def _commit_mismatch(command: HerdrCommitCommand, evidence: HerdrCommitEvidence) -> str | None:
    if evidence.launch_id != command.launch_id:
        return "launch_id"
    if evidence.operation_id != command.operation_id:
        return "operation_id"
    if evidence.target != command.target:
        return "target"
    if evidence.generation != command.generation:
        return "generation"
    expected_dispositions = {"created", "reused"} if command.action == "allocate" else {"reused"}
    if evidence.disposition not in expected_dispositions:
        return "disposition"
    if not evidence.allocation_id:
        return "allocation_id"
    return None


def _commit_result(
    prepared: PreparedLaunch,
    operation_id: str,
    *,
    outcome: str,
    allocation_id: str | None = None,
    generation: int | None = None,
    portable_receipt: dict[str, object] | None = None,
    error_code: str | None = None,
) -> AdapterCommitResultV1:
    return AdapterCommitResultV1(
        launch_id=prepared.portable.launch_id,
        commit_operation_id=operation_id,
        binding_digest=prepared.portable.binding_digest,
        adapter_kind=_ADAPTER_KIND,
        outcome=outcome,
        allocation_id=allocation_id,
        generation=generation,
        portable_receipt=portable_receipt,
        error_code=error_code,
    )


def _failed_commit(
    prepared: PreparedLaunch,
    operation_id: str,
    code: FailureCode,
) -> AdapterCommitResultV1:
    outcome = "failed" if code in {FailureCode.UNAVAILABLE, FailureCode.RETRYABLE} else "refused"
    return _commit_result(
        prepared,
        operation_id,
        outcome=outcome,
        error_code=_error_code(code),
    )


def _error_code(code: FailureCode) -> str:
    return f"herdr_{code.value}"


def _portable_provider_receipt(evidence: HerdrCommitEvidence) -> dict[str, object]:
    receipt: dict[str, object] = {
        "type": "beadhive.herdr-adapter",
        "version": "1",
        "disposition": evidence.disposition,
        "target": evidence.target,
        "generation": evidence.generation,
    }
    for name in ("session", "workspace_id", "source_revision", "generation_fenced"):
        value = getattr(evidence, name)
        if value is not None and value is not False:
            receipt[name] = value
    return receipt


def _temporary_receipt(
    prepared: PreparedLaunch,
    operation_id: str,
    evidence: HerdrCommitEvidence,
) -> LaunchReceiptV1:
    return LaunchReceiptV1(
        launch_id=prepared.portable.launch_id,
        prepare_operation_id=prepared.portable.prepare_operation_id,
        commit_operation_id=operation_id,
        profile_receipt=prepared.portable.profile_receipt,
        workspace_binding=prepared.portable.workspace_binding,
        adapter_kind=_ADAPTER_KIND,
        adapter_receipt=_portable_provider_receipt(evidence),
        allocation_id=evidence.allocation_id,
        generation=evidence.generation,
    )


def _temporary_receipt_from_result(
    prepared: PreparedLaunch,
    result: AdapterCommitResultV1,
) -> LaunchReceiptV1:
    return LaunchReceiptV1(
        launch_id=prepared.portable.launch_id,
        prepare_operation_id=prepared.portable.prepare_operation_id,
        commit_operation_id=result.commit_operation_id,
        profile_receipt=prepared.portable.profile_receipt,
        workspace_binding=prepared.portable.workspace_binding,
        adapter_kind=_ADAPTER_KIND,
        adapter_receipt=result.portable_receipt,
        allocation_id=result.allocation_id,
        generation=result.generation,
    )


def _teardown_matches(
    receipt: LaunchReceiptV1,
    evidence: HerdrTeardownEvidence,
) -> bool:
    return (
        evidence.launch_id == receipt.launch_id
        and evidence.allocation_id == receipt.allocation_id
        and evidence.generation == receipt.generation
    )
