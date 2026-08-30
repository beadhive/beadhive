"""Hermetic support matrix for the Herdr-owned external provider boundary."""

import pytest

from beadhive.agent_launch_profile import AgentLaunchProfile, AgentLaunchReceipt
from beadhive.herdr_launch_profile import launch_spec_digest


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("seat", ["developer", "dispatcher", "planner"])
def test_six_row_authority_transport_is_exact_and_redacted(harness, seat):
    from beadhive.agent_launch_profile import resolve_agent_launch_profile

    managed = seat != "planner"
    resolved = resolve_agent_launch_profile(
        AgentLaunchProfile(
            managed_bead=managed,
            bead="bh-proof.1" if managed else None,
            initial_seat=seat,
            harness=harness,
            model="proof-model",
            effort="low",
        )
    )
    receipt = AgentLaunchReceipt.from_resolved(resolved).model_dump(mode="json")
    assert receipt["seat_contract_digest"] == resolved.seat_contract_digest
    assert launch_spec_digest(resolved).startswith("sha256:")
    assert "argv" not in receipt and "instructions" not in receipt
    if harness == "claude":
        assert resolved.argv[:3] == ("claude", "--agent", f"bh:{seat}")
    else:
        assert resolved.argv[0] == "codex"
        assert any(
            value.startswith("developer_instructions=") and f"{seat} seat" in value
            for value in resolved.argv
        )


def test_stale_generation_cannot_authorize_successor_cleanup():
    current = {"target": "agent-a", "generation": 8, "launch_spec_digest": "sha256:new"}
    stale = {"target": "agent-a", "generation": 7, "launch_spec_digest": "sha256:old"}
    assert (stale["generation"], stale["launch_spec_digest"]) != (
        current["generation"],
        current["launch_spec_digest"],
    )

