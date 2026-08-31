"""Portable v1 agent-launch transaction contracts.

These models contain policy and correlation facts only. Host paths, provider topology,
credentials, raw argv, and cleanup handles belong to local capabilities held by adapters.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .profile import AgentLaunchProfile, AgentLaunchReceipt, ResolvedAgentLaunchProfile

TransactionVersion = Literal["1"]
Generation = Annotated[int, Field(strict=True, ge=1)]


class _PortableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class WorkspaceBindingKind(StrEnum):
    MANAGED_BEAD = "managed_bead"
    BEADLESS_SEAT = "beadless_seat"
    BATCH = "batch"
    EPIC_CONTAINER = "epic_container"
    SHARED_CHECKOUT = "shared_checkout"


class AdapterAction(StrEnum):
    ALLOCATE = "allocate"
    REUSE = "reuse"
    NONE = "none"


class AdapterOutcome(StrEnum):
    COMMITTED = "committed"
    REFUSED = "refused"
    FAILED = "failed"


class CompensationStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    COMPLETED = "completed"
    FAILED = "failed"


class OperationPhase(StrEnum):
    PREPARE = "prepare"
    COMMIT = "commit"
    ABORT = "abort"
    RECOVER = "recover"
    TEARDOWN = "teardown"


class ObservationStatus(StrEnum):
    LIVE = "live"
    MISSING = "missing"
    STALE = "stale"
    UNKNOWN = "unknown"


class RecoveryDisposition(StrEnum):
    ADOPTED = "adopted"
    RELAUNCHED = "relaunched"
    UNCHANGED = "unchanged"
    REFUSED = "refused"


class TeardownDisposition(StrEnum):
    TORN_DOWN = "torn_down"
    ALREADY_ABSENT = "already_absent"
    RETAINED = "retained"
    REFUSED = "refused"


class WorkspaceTargetV1(_PortableModel):
    type: Literal["beadhive.workspace-target"] = "beadhive.workspace-target"
    version: TransactionVersion = "1"
    hive_id: str
    binding_kind: WorkspaceBindingKind
    requested_bead: str | None = None
    batch_id: str | None = None
    epic_id: str | None = None
    parent_launch_id: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> WorkspaceTargetV1:
        _require_text(self.hive_id, "hive_id")
        _validate_binding_identity(
            self.binding_kind,
            requested_bead=self.requested_bead,
            batch_id=self.batch_id,
            epic_id=self.epic_id,
        )
        if self.parent_launch_id is not None:
            _require_text(self.parent_launch_id, "parent_launch_id")
        return self


class WorkspaceBindingV1(_PortableModel):
    type: Literal["beadhive.workspace-binding"] = "beadhive.workspace-binding"
    version: TransactionVersion = "1"
    hive_id: str
    binding_kind: WorkspaceBindingKind
    requested_bead: str | None = None
    batch_id: str | None = None
    epic_id: str | None = None
    branch: str
    worktree_id: str
    parent_launch_id: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> WorkspaceBindingV1:
        for value, name in (
            (self.hive_id, "hive_id"),
            (self.branch, "branch"),
            (self.worktree_id, "worktree_id"),
        ):
            _require_text(value, name)
        _validate_binding_identity(
            self.binding_kind,
            requested_bead=self.requested_bead,
            batch_id=self.batch_id,
            epic_id=self.epic_id,
        )
        if self.parent_launch_id is not None:
            _require_text(self.parent_launch_id, "parent_launch_id")
        return self


class PreparedLaunchV1(_PortableModel):
    type: Literal["beadhive.prepared-launch"] = "beadhive.prepared-launch"
    version: TransactionVersion = "1"
    launch_id: str
    prepare_operation_id: str
    profile_receipt: AgentLaunchReceipt
    workspace_binding: WorkspaceBindingV1
    binding_digest: str
    adapter_kind: str
    adapter_action: AdapterAction
    adapter_plan_digest: str
    state: Literal["prepared"] = "prepared"

    @model_validator(mode="after")
    def validate_correlations(self) -> PreparedLaunchV1:
        for value, name in (
            (self.launch_id, "launch_id"),
            (self.prepare_operation_id, "prepare_operation_id"),
            (self.adapter_kind, "adapter_kind"),
        ):
            _require_text(value, name)
        _require_digest(self.binding_digest, "binding_digest")
        _require_digest(self.adapter_plan_digest, "adapter_plan_digest")
        if self.binding_digest != portable_digest(self.workspace_binding):
            raise ValueError("binding_digest conflicts with workspace_binding")
        _validate_profile_binding(self.profile_receipt, self.workspace_binding)
        return self


class AdapterCommitResultV1(_PortableModel):
    type: Literal["beadhive.adapter-commit-result"] = "beadhive.adapter-commit-result"
    version: TransactionVersion = "1"
    launch_id: str
    commit_operation_id: str
    binding_digest: str
    adapter_kind: str
    outcome: AdapterOutcome
    allocation_id: str | None = None
    generation: Generation | None = None
    portable_receipt: dict[str, JsonValue] | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> AdapterCommitResultV1:
        for value, name in (
            (self.launch_id, "launch_id"),
            (self.commit_operation_id, "commit_operation_id"),
            (self.adapter_kind, "adapter_kind"),
        ):
            _require_text(value, name)
        _require_digest(self.binding_digest, "binding_digest")
        if self.outcome == AdapterOutcome.COMMITTED:
            if self.error_code is not None:
                raise ValueError("committed adapter result cannot contain error_code")
        elif self.error_code is None:
            raise ValueError("refused or failed adapter result requires error_code")
        if self.allocation_id is not None:
            _require_text(self.allocation_id, "allocation_id")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")
        return self


class LaunchReceiptV1(_PortableModel):
    type: Literal["beadhive.launch"] = "beadhive.launch"
    version: TransactionVersion = "1"
    launch_id: str
    prepare_operation_id: str
    commit_operation_id: str
    profile_receipt: AgentLaunchReceipt
    workspace_binding: WorkspaceBindingV1
    adapter_kind: str
    allocation_id: str | None = None
    generation: Generation | None = None
    state: Literal["committed"] = "committed"

    @model_validator(mode="after")
    def validate_correlations(self) -> LaunchReceiptV1:
        for value, name in (
            (self.launch_id, "launch_id"),
            (self.prepare_operation_id, "prepare_operation_id"),
            (self.commit_operation_id, "commit_operation_id"),
            (self.adapter_kind, "adapter_kind"),
        ):
            _require_text(value, name)
        if self.allocation_id is not None:
            _require_text(self.allocation_id, "allocation_id")
        _validate_profile_binding(self.profile_receipt, self.workspace_binding)
        return self


class AbortReceiptV1(_PortableModel):
    type: Literal["beadhive.launch-abort"] = "beadhive.launch-abort"
    version: TransactionVersion = "1"
    launch_id: str
    abort_operation_id: str
    failed_operation_id: str | None = None
    workspace_binding: WorkspaceBindingV1 | None = None
    reason_code: str
    compensation: CompensationStatus
    compensated_allocation_id: str | None = None
    state: Literal["aborted"] = "aborted"

    @model_validator(mode="after")
    def validate_correlations(self) -> AbortReceiptV1:
        for value, name in (
            (self.launch_id, "launch_id"),
            (self.abort_operation_id, "abort_operation_id"),
            (self.reason_code, "reason_code"),
        ):
            _require_text(value, name)
        if self.failed_operation_id is not None:
            _require_text(self.failed_operation_id, "failed_operation_id")
        if self.compensated_allocation_id is not None:
            _require_text(self.compensated_allocation_id, "compensated_allocation_id")
        if (
            self.compensation == CompensationStatus.NOT_NEEDED
            and self.compensated_allocation_id is not None
        ):
            raise ValueError("not_needed compensation cannot name an allocation")
        return self


class AgentObservationV1(_PortableModel):
    type: Literal["beadhive.agent-observation"] = "beadhive.agent-observation"
    version: TransactionVersion = "1"
    launch_id: str
    binding_digest: str
    status: ObservationStatus
    generation: Generation | None = None
    allocation_id: str | None = None

    @model_validator(mode="after")
    def validate_correlations(self) -> AgentObservationV1:
        _require_text(self.launch_id, "launch_id")
        _require_digest(self.binding_digest, "binding_digest")
        if self.allocation_id is not None:
            _require_text(self.allocation_id, "allocation_id")
        return self


class RecoveryResultV1(_PortableModel):
    type: Literal["beadhive.launch-recovery"] = "beadhive.launch-recovery"
    version: TransactionVersion = "1"
    launch_id: str
    recover_operation_id: str
    binding_digest: str
    disposition: RecoveryDisposition
    previous_generation: Generation | None = None
    generation: Generation | None = None
    allocation_id: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_correlations(self) -> RecoveryResultV1:
        _require_text(self.launch_id, "launch_id")
        _require_text(self.recover_operation_id, "recover_operation_id")
        _require_digest(self.binding_digest, "binding_digest")
        if self.allocation_id is not None:
            _require_text(self.allocation_id, "allocation_id")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")
        return self


class TeardownResultV1(_PortableModel):
    type: Literal["beadhive.launch-teardown"] = "beadhive.launch-teardown"
    version: TransactionVersion = "1"
    launch_id: str
    teardown_operation_id: str
    binding_digest: str
    generation: Generation | None = None
    disposition: TeardownDisposition
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_correlations(self) -> TeardownResultV1:
        _require_text(self.launch_id, "launch_id")
        _require_text(self.teardown_operation_id, "teardown_operation_id")
        _require_digest(self.binding_digest, "binding_digest")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")
        return self


class PrepareLaunchRequest(_PortableModel):
    profile: AgentLaunchProfile
    target: WorkspaceTargetV1
    launch_id: str
    operation_id: str
    adapter_kind: str

    @model_validator(mode="after")
    def validate_policy(self) -> PrepareLaunchRequest:
        for value, name in (
            (self.launch_id, "launch_id"),
            (self.operation_id, "operation_id"),
            (self.adapter_kind, "adapter_kind"),
        ):
            _require_text(value, name)
        if self.profile.managed_bead:
            if self.target.requested_bead != self.profile.bead:
                raise ValueError("workspace target conflicts with managed bead")
            if self.target.binding_kind not in {
                WorkspaceBindingKind.MANAGED_BEAD,
                WorkspaceBindingKind.BATCH,
                WorkspaceBindingKind.EPIC_CONTAINER,
            }:
                raise ValueError("managed profile requires a managed workspace target")
        elif self.target.binding_kind not in {
            WorkspaceBindingKind.BEADLESS_SEAT,
            WorkspaceBindingKind.SHARED_CHECKOUT,
        }:
            raise ValueError("beadless profile requires a beadless workspace target")
        return self


@dataclass(frozen=True, slots=True)
class BoundWorkspace:
    binding: WorkspaceBindingV1
    adapter_action: AdapterAction
    adapter_plan: Mapping[str, JsonValue]
    local_capability: object


@dataclass(frozen=True, slots=True)
class PreparedLaunch:
    portable: PreparedLaunchV1
    resolved_profile: ResolvedAgentLaunchProfile
    adapter_plan: Mapping[str, JsonValue]
    local_capability: object

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.portable.adapter_plan_digest != portable_digest(self.adapter_plan):
            raise ValueError("adapter_plan_digest conflicts with adapter_plan")
        expected_receipt = AgentLaunchReceipt.from_resolved(self.resolved_profile)
        if self.portable.profile_receipt != expected_receipt:
            raise ValueError("resolved_profile conflicts with profile_receipt")


@dataclass(frozen=True, slots=True)
class CommitLaunchRequest:
    prepared: PreparedLaunch
    operation_id: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require_text(self.operation_id, "operation_id")
        self.prepared.validate()


@dataclass(frozen=True, slots=True)
class CommitLaunchResult:
    adapter_result: AdapterCommitResultV1
    receipt: LaunchReceiptV1 | None


@dataclass(frozen=True, slots=True)
class AbortLaunchRequest:
    prepared: PreparedLaunch
    operation_id: str
    reason_code: str
    failed_result: AdapterCommitResultV1 | None = None
    failed_operation_id: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require_text(self.operation_id, "operation_id")
        _require_text(self.reason_code, "reason_code")
        if self.failed_operation_id is not None:
            _require_text(self.failed_operation_id, "failed_operation_id")
        self.prepared.validate()
        if self.failed_result is None:
            return
        expected = self.prepared.portable
        mismatches = []
        if self.failed_result.launch_id != expected.launch_id:
            mismatches.append("launch_id")
        if self.failed_result.binding_digest != expected.binding_digest:
            mismatches.append("binding_digest")
        if self.failed_result.adapter_kind != expected.adapter_kind:
            mismatches.append("adapter_kind")
        if mismatches:
            raise ValueError(
                "failed adapter result conflicts with prepared launch: " + ", ".join(mismatches)
            )


class ObserveLaunchRequest(_PortableModel):
    receipt: LaunchReceiptV1


class RecoverLaunchRequest(_PortableModel):
    receipt: LaunchReceiptV1
    observation: AgentObservationV1
    operation_id: str

    @model_validator(mode="after")
    def validate_correlations(self) -> RecoverLaunchRequest:
        _require_text(self.operation_id, "operation_id")
        _validate_observation(self.receipt, self.observation)
        return self


class TeardownLaunchRequest(_PortableModel):
    receipt: LaunchReceiptV1
    operation_id: str

    @model_validator(mode="after")
    def validate_correlations(self) -> TeardownLaunchRequest:
        _require_text(self.operation_id, "operation_id")
        return self


@dataclass(frozen=True, slots=True)
class CompensationResult:
    status: CompensationStatus
    allocation_id: str | None = None


TerminalReceipt = LaunchReceiptV1 | AbortReceiptV1
OperationResult = (
    PreparedLaunch | CommitLaunchResult | AbortReceiptV1 | RecoveryResultV1 | TeardownResultV1
)


def canonical_json_bytes(value: BaseModel | Mapping[str, JsonValue]) -> bytes:
    """Return the stable UTF-8 representation used by v1 digests and replay checks."""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def portable_digest(value: BaseModel | Mapping[str, JsonValue]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_digest(value: str, field_name: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError(f"{field_name} must be a sha256 digest")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a sha256 digest") from exc


def _validate_binding_identity(
    kind: WorkspaceBindingKind,
    *,
    requested_bead: str | None,
    batch_id: str | None,
    epic_id: str | None,
) -> None:
    values = (
        (requested_bead, "requested_bead"),
        (batch_id, "batch_id"),
        (epic_id, "epic_id"),
    )
    for value, name in values:
        if value is not None:
            _require_text(value, name)
    if kind == WorkspaceBindingKind.MANAGED_BEAD:
        if requested_bead is None or batch_id is not None or epic_id is not None:
            raise ValueError("managed_bead requires only requested_bead")
    elif kind == WorkspaceBindingKind.BATCH:
        if requested_bead is None or batch_id is None or epic_id is not None:
            raise ValueError("batch requires requested_bead and batch_id")
    elif kind == WorkspaceBindingKind.EPIC_CONTAINER:
        if requested_bead is None or epic_id is None or batch_id is not None:
            raise ValueError("epic_container requires requested_bead and epic_id")
    elif requested_bead is not None or batch_id is not None or epic_id is not None:
        raise ValueError(f"{kind} cannot carry bead, batch, or epic identity")


def _validate_profile_binding(
    profile: AgentLaunchReceipt,
    binding: WorkspaceBindingV1,
) -> None:
    if profile.managed_bead:
        if binding.requested_bead != profile.bead:
            raise ValueError("workspace binding conflicts with managed bead")
        if binding.binding_kind not in {
            WorkspaceBindingKind.MANAGED_BEAD,
            WorkspaceBindingKind.BATCH,
            WorkspaceBindingKind.EPIC_CONTAINER,
        }:
            raise ValueError("managed receipt requires a managed workspace binding")
    elif binding.binding_kind not in {
        WorkspaceBindingKind.BEADLESS_SEAT,
        WorkspaceBindingKind.SHARED_CHECKOUT,
    }:
        raise ValueError("beadless receipt requires a beadless workspace binding")


def _validate_observation(
    receipt: LaunchReceiptV1,
    observation: AgentObservationV1,
) -> None:
    if observation.launch_id != receipt.launch_id:
        raise ValueError("observation conflicts with launch identity")
    if observation.binding_digest != portable_digest(receipt.workspace_binding):
        raise ValueError("observation conflicts with workspace binding")
    if (
        receipt.generation is not None
        and observation.generation is not None
        and observation.generation != receipt.generation
    ):
        raise ValueError("observation conflicts with launch generation")
