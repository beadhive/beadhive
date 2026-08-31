"""Frozen compatibility surface for the pre-extraction agent-launch slice."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import herdr_plugin
from beadhive.agent_launch_profile import (
    AgentLaunchProfile,
    AgentLaunchReceipt,
    BeadPolicy,
    Harness,
    ResolvedAgentLaunchProfile,
    agent_launch_receipt_from_env,
    bead_policy_for_seat,
    parse_agent_launch_receipt,
    resolve_agent_launch_profile,
)
from beadhive.cli import app
from beadhive.herdr_launch_profile import (
    HerdrAgentLaunchProfile,
    HerdrAgentLaunchReceipt,
    HerdrPaneCreateTarget,
    build_herdr_launch_receipt,
    consume_herdr_launch_receipt,
    launch_spec_digest,
    parse_herdr_launch_receipt,
    resolve_herdr_launch_profile,
    validate_herdr_receipt_observation,
    validate_herdr_result_observation,
    worktree_binding_digest,
)
from beadhive.operation_catalog import operations
from beadhive.seat_contracts import SeatContract, seat_contract

ROOT = Path(__file__).parents[2]


def test_authoritative_owner_matrix_resolves_to_current_implementation_modules():
    """A move must preserve these owners through facades or migrate this matrix deliberately."""

    owners = {
        "seat contracts": (
            SeatContract,
            seat_contract,
            "beadhive.modules.agents.domain.seat",
        ),
        "generic profiles": (
            AgentLaunchProfile,
            resolve_agent_launch_profile,
            "beadhive.modules.agents.domain.profile",
        ),
        "Herdr profiles": (
            HerdrAgentLaunchProfile,
            resolve_herdr_launch_profile,
            "beadhive.herdr_launch_profile",
        ),
        "prepared launch": (
            herdr_plugin._launch_cmd,
            herdr_plugin._launch_fail,
            "beadhive.herdr_plugin",
        ),
        "receipts": (
            AgentLaunchReceipt,
            HerdrAgentLaunchReceipt,
            "beadhive.modules.agents.domain.profile",
        ),
        "generations": (
            herdr_plugin._validate_managed_generation,
            herdr_plugin._recover_managed_generation,
            "beadhive.herdr_plugin",
        ),
        "adoption": (
            herdr_plugin._recover_managed_generation,
            herdr_plugin._strict_live_target,
            "beadhive.herdr_plugin",
        ),
        "abort": (herdr_plugin._launch_fail, herdr_plugin._close_pane, "beadhive.herdr_plugin"),
        "teardown": (
            herdr_plugin._reap_cmd,
            herdr_plugin._generation_reap_matches,
            "beadhive.herdr_plugin",
        ),
        "CLI output": (herdr_plugin._launch_cmd, herdr_plugin._reap_cmd, "beadhive.herdr_plugin"),
        "error codes": (
            herdr_plugin._launch_fail,
            herdr_plugin._lifecycle_failure,
            "beadhive.herdr_plugin",
        ),
    }

    assert set(owners) == {
        "seat contracts",
        "generic profiles",
        "Herdr profiles",
        "prepared launch",
        "receipts",
        "generations",
        "adoption",
        "abort",
        "teardown",
        "CLI output",
        "error codes",
    }
    for concern, (primary, secondary, expected_module) in owners.items():
        expected_modules = {expected_module}
        if concern == "receipts":
            expected_modules.add("beadhive.herdr_launch_profile")
        assert primary.__module__ in expected_modules, concern
        assert secondary.__module__ in expected_modules, concern


def test_legacy_public_imports_and_model_field_order_are_frozen():
    public_symbols = {
        "beadhive.modules.agents.domain.seat": (SeatContract, seat_contract),
        "beadhive.modules.agents.domain.profile": (
            BeadPolicy,
            Harness,
            AgentLaunchProfile,
            ResolvedAgentLaunchProfile,
            AgentLaunchReceipt,
            bead_policy_for_seat,
            resolve_agent_launch_profile,
            parse_agent_launch_receipt,
            agent_launch_receipt_from_env,
        ),
        "beadhive.herdr_launch_profile": (
            HerdrPaneCreateTarget,
            HerdrAgentLaunchProfile,
            HerdrAgentLaunchReceipt,
            launch_spec_digest,
            worktree_binding_digest,
            build_herdr_launch_receipt,
            validate_herdr_result_observation,
            parse_herdr_launch_receipt,
            validate_herdr_receipt_observation,
            consume_herdr_launch_receipt,
            resolve_herdr_launch_profile,
        ),
    }
    for module, symbols in public_symbols.items():
        assert all(symbol.__module__ == module for symbol in symbols)

    assert tuple(AgentLaunchProfile.model_fields) == (
        "version",
        "managed_bead",
        "bead",
        "initial_seat",
        "available_seats",
        "harness",
        "model",
        "effort",
    )
    assert tuple(AgentLaunchReceipt.model_fields) == (
        "receipt_type",
        "version",
        "managed_bead",
        "bead",
        "initial_seat",
        "current_seat",
        "available_seats",
        "harness",
        "model",
        "effort",
        "seat_contract_version",
        "seat_contract_digest",
    )
    assert tuple(HerdrAgentLaunchProfile.model_fields)[8:] == (
        "herdr_session",
        "space_id",
        "space_revision",
        "pane_id",
        "pane_create",
        "launch_id",
        "operation_id",
        "generation",
        "launch_target",
        "session_checkout_id",
    )
    assert tuple(HerdrAgentLaunchReceipt.model_fields) == (
        "receipt_type",
        "version",
        "core",
        "herdr_session",
        "space_id",
        "space_revision",
        "tab_id",
        "pane_id",
        "agent_target",
        "agent_session",
        "worktree_binding_digest",
        "launch_spec_digest",
        "seat_contract_digest",
        "generation",
        "launch_id",
        "operation_id",
    )


def test_json_discriminators_redaction_and_checked_schema_boundary_are_frozen():
    resolved = resolve_agent_launch_profile(
        AgentLaunchProfile(
            managed_bead=True,
            bead="bh-contract.1",
            initial_seat="developer",
            harness="codex",
            model="gpt-contract",
            effort="high",
        )
    )
    payload = AgentLaunchReceipt.from_resolved(resolved).model_dump(mode="json")
    assert payload["receipt_type"] == "beadhive.agent-launch"
    assert payload["version"] == "1"
    assert set(payload) == set(AgentLaunchReceipt.model_fields)
    assert not {"argv", "instructions", "worktree", "cwd", "environment"} & payload.keys()

    lifecycle_schema = json.loads(
        (ROOT / "docs/schemas/herdr-lifecycle-receipt-v1.schema.json").read_text()
    )
    assert lifecycle_schema["$id"].endswith("/herdr-lifecycle-receipt-v1.schema.json")
    assert "plugin herdr launch" not in lifecycle_schema["properties"]["command"]["enum"]
    assert not (ROOT / "docs/schemas/agent-launch-profile-v1.schema.json").exists()
    assert not (ROOT / "docs/schemas/herdr-agent-launch-receipt-v1.schema.json").exists()


def test_typer_paths_and_catalog_signatures_are_frozen():
    result = CliRunner().invoke(app, ["plugin", "herdr", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("launch", "spawn", "reap", "dispatch", "watch", "attach", "ps", "status"):
        assert command in result.output

    by_name = {operation.name: operation for operation in operations()}
    assert by_name["plugin.herdr.launch"].surfaces["cli"]["path"] == "plugin herdr launch"
    assert tuple(parameter.name for parameter in by_name["plugin.herdr.launch"].parameters) == (
        "bead",
        "hive",
        "kind",
        "session",
        "as_",
        "adopt_expired",
        "direction",
        "focus",
        "as_json",
        "profile_json",
        "recover_after_pane",
        "session_checkout",
    )
    assert tuple(parameter.name for parameter in by_name["plugin.herdr.reap"].parameters) == (
        "target",
        "session",
        "pane",
        "as_json",
        "operation_id",
        "generation",
        "launch_spec_digest",
    )


def test_supported_monkeypatch_points_and_dynamic_callers_remain_addressable():
    seams = {
        "_session_snapshot": (),
        "_snapshot_agent_records": ("snapshot",),
        "_metadata_tokens": ("record",),
        "_launch_warm": ("target",),
        "_close_pane": ("pane",),
        "_strict_live_target": ("target", "hive", "cwd"),
        "_validate_managed_generation": ("target", "profile", "resolved"),
        "_recover_managed_generation": ("profile", "resolved", "snapshot", "after_pane_id"),
        "_generation_reap_matches": ("target", "pane", "generation", "launch_spec_digest"),
    }
    for name, parameters in seams.items():
        seam = getattr(herdr_plugin, name)
        assert tuple(inspect.signature(seam).parameters) == parameters

    role_source = (ROOT / "src/beadhive/role.py").read_text()
    plugin_source = (ROOT / "src/beadhive/herdr_plugin.py").read_text()
    cli_source = (ROOT / "src/beadhive/cli.py").read_text()
    assert "from .agent_launch_profile import (" in role_source
    assert "from .herdr_launch_profile import (" in plugin_source
    assert 'report["agent_launch_profile"]' in cli_source


@pytest.mark.parametrize(
    ("native_marker", "native_child_id"),
    [
        ("CLAUDE_TASK_ID", "native-task-child"),
        ("CODEX_THREAD_ID", "native-collaboration-child"),
    ],
)
def test_native_children_ignore_valid_inherited_parent_receipts(
    native_marker,
    native_child_id,
):
    """Native child correlation cannot inherit managed lifecycle or teardown authority."""

    parent_receipt = AgentLaunchReceipt.from_resolved(
        resolve_agent_launch_profile(
            AgentLaunchProfile(
                managed_bead=True,
                bead="bh-contract.1",
                initial_seat="developer",
                available_seats={"developer", "reviewer"},
                harness="codex",
            )
        )
    )
    native_child_environment = {
        native_marker: native_child_id,
        "BH_ROLE": "developer",
        "BH_AGENT_LAUNCH_RECEIPT": parent_receipt.model_dump_json(),
    }

    assert agent_launch_receipt_from_env(native_child_environment) is None


def test_managed_external_harness_accepts_valid_receipt_without_native_child_marker():
    receipt = AgentLaunchReceipt.from_resolved(
        resolve_agent_launch_profile(
            AgentLaunchProfile(
                managed_bead=True,
                bead="bh-contract.1",
                initial_seat="developer",
                harness="codex",
            )
        )
    )

    assert (
        agent_launch_receipt_from_env({"BH_AGENT_LAUNCH_RECEIPT": receipt.model_dump_json()})
        == receipt
    )
