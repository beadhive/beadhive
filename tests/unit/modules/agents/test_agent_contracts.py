"""Portable contract tests for the provider-neutral agents module."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from beadhive.agent_launch_profile import AgentLaunchProfile as LegacyAgentLaunchProfile
from beadhive.modules.agents.domain.profile import (
    AgentLaunchProfile,
    AgentLaunchReceipt,
    resolve_agent_launch_profile,
)
from beadhive.modules.agents.domain.seat import SeatContract
from beadhive.modules.agents.domain.transaction import (
    AbortReceiptV1,
    AdapterCommitResultV1,
    AgentObservationV1,
    LaunchReceiptV1,
    PreparedLaunchV1,
    RecoveryResultV1,
    TeardownResultV1,
    WorkspaceBindingV1,
    WorkspaceTargetV1,
    canonical_json_bytes,
    portable_digest,
)
from beadhive.seat_contracts import SeatContract as LegacySeatContract


def _profile_receipt() -> AgentLaunchReceipt:
    return AgentLaunchReceipt.from_resolved(
        resolve_agent_launch_profile(
            AgentLaunchProfile(
                managed_bead=True,
                bead="bh-agents.2",
                initial_seat="developer",
                available_seats={"reviewer", "developer"},
                harness="codex",
            )
        )
    )


def _binding() -> WorkspaceBindingV1:
    return WorkspaceBindingV1(
        hive_id="github/beadhive/beadhive",
        binding_kind="managed_bead",
        requested_bead="bh-agents.2",
        branch="wt/bead/issue/bh-agents.2",
        worktree_id="worktree-opaque-1",
        parent_launch_id="launch-parent",
    )


def _prepared() -> PreparedLaunchV1:
    binding = _binding()
    return PreparedLaunchV1(
        launch_id="launch-1",
        prepare_operation_id="prepare-1",
        profile_receipt=_profile_receipt(),
        workspace_binding=binding,
        binding_digest=portable_digest(binding),
        adapter_kind="fake",
        adapter_action="allocate",
        adapter_plan_digest=portable_digest({"mode": "fresh"}),
    )


def _receipt() -> LaunchReceiptV1:
    prepared = _prepared()
    return LaunchReceiptV1(
        launch_id=prepared.launch_id,
        prepare_operation_id=prepared.prepare_operation_id,
        commit_operation_id="commit-1",
        profile_receipt=prepared.profile_receipt,
        workspace_binding=prepared.workspace_binding,
        adapter_kind=prepared.adapter_kind,
        allocation_id="allocation-1",
        generation=1,
    )


def test_legacy_imports_are_identity_preserving_forwards() -> None:
    assert LegacySeatContract is SeatContract
    assert LegacyAgentLaunchProfile is AgentLaunchProfile
    assert SeatContract.__module__ == "beadhive.modules.agents.domain.seat"
    assert AgentLaunchProfile.__module__ == "beadhive.modules.agents.domain.profile"


@pytest.mark.parametrize(
    "payload",
    [
        {"binding_kind": "managed_bead"},
        {"binding_kind": "batch", "requested_bead": "bh-agents.2"},
        {"binding_kind": "epic_container", "requested_bead": "bh-agents.2"},
        {"binding_kind": "beadless_seat", "requested_bead": "bh-agents.2"},
        {"binding_kind": "shared_checkout", "epic_id": "bh-agents"},
    ],
)
def test_workspace_target_refuses_incomplete_or_leaking_identity(payload) -> None:
    with pytest.raises(ValidationError):
        WorkspaceTargetV1(hive_id="github/beadhive/beadhive", **payload)


def test_requested_child_identity_survives_batch_and_epic_resolution() -> None:
    batch = WorkspaceBindingV1(
        hive_id="github/beadhive/beadhive",
        binding_kind="batch",
        requested_bead="bh-agents.2",
        batch_id="agent-batch",
        branch="wt/batch/agent-batch",
        worktree_id="batch-opaque",
    )
    epic = WorkspaceBindingV1(
        hive_id="github/beadhive/beadhive",
        binding_kind="epic_container",
        requested_bead="bh-agents.2",
        epic_id="bh-agents",
        branch="wt/bead/epic/bh-agents",
        worktree_id="epic-opaque",
    )
    assert batch.requested_bead == epic.requested_bead == "bh-agents.2"
    assert batch.branch != epic.branch


def test_all_v1_portable_schemas_have_canonical_byte_determinism() -> None:
    prepared = _prepared()
    receipt = _receipt()
    binding_digest = portable_digest(receipt.workspace_binding)
    models = (
        receipt.workspace_binding,
        prepared,
        AdapterCommitResultV1(
            launch_id="launch-1",
            commit_operation_id="commit-1",
            binding_digest=binding_digest,
            adapter_kind="fake",
            outcome="committed",
            allocation_id="allocation-1",
            generation=1,
            portable_receipt={"z": 2, "a": 1},
        ),
        receipt,
        AbortReceiptV1(
            launch_id="launch-2",
            abort_operation_id="abort-1",
            workspace_binding=receipt.workspace_binding,
            reason_code="adapter_failed",
            compensation="completed",
            compensated_allocation_id="allocation-2",
        ),
        AgentObservationV1(
            launch_id="launch-1",
            binding_digest=binding_digest,
            status="live",
            generation=1,
            allocation_id="allocation-1",
        ),
        RecoveryResultV1(
            launch_id="launch-1",
            recover_operation_id="recover-1",
            binding_digest=binding_digest,
            disposition="relaunched",
            previous_generation=1,
            generation=2,
            allocation_id="allocation-2",
        ),
        TeardownResultV1(
            launch_id="launch-1",
            teardown_operation_id="teardown-1",
            binding_digest=binding_digest,
            generation=1,
            disposition="torn_down",
        ),
    )
    for model in models:
        first = canonical_json_bytes(model)
        second = canonical_json_bytes(type(model).model_validate_json(first))
        assert first == second
        assert (
            first
            == json.dumps(
                json.loads(first), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )


def test_portable_schemas_exclude_local_and_provider_topology_fields() -> None:
    forbidden = {
        "cwd",
        "environment",
        "herdr_session",
        "local_capability",
        "pane_id",
        "root_path",
        "session_id",
        "space_id",
        "tab_id",
        "worktree_path",
    }
    schemas = (
        WorkspaceBindingV1,
        PreparedLaunchV1,
        AdapterCommitResultV1,
        LaunchReceiptV1,
        AbortReceiptV1,
        AgentObservationV1,
        RecoveryResultV1,
        TeardownResultV1,
    )
    for model in schemas:
        assert forbidden.isdisjoint(model.model_fields)


def test_binding_and_plan_digests_fail_closed() -> None:
    prepared = _prepared()
    with pytest.raises(ValidationError, match="binding_digest"):
        PreparedLaunchV1.model_validate(
            {**prepared.model_dump(), "binding_digest": "sha256:" + "0" * 64}
        )
