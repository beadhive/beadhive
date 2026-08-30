"""Hermetic support matrix for the Herdr-owned external provider boundary."""

import pytest

from beadhive import herdr_plugin
from beadhive.agent_launch_profile import AgentLaunchProfile, AgentLaunchReceipt
from beadhive.herdr_launch_profile import (
    HerdrAgentLaunchProfile,
    launch_spec_digest,
    resolve_herdr_launch_profile,
)


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


def _managed_profile(generation=8):
    return HerdrAgentLaunchProfile(
        managed_bead=True,
        bead="bh-proof.1",
        initial_seat="developer",
        harness="codex",
        herdr_session="proof-session",
        space_id="space-1",
        space_revision="revision-1",
        pane_id="pane-1",
        launch_id="launch-1",
        operation_id="operation-1",
        generation=generation,
    )


def test_restart_reconciliation_adopts_only_exact_durable_generation(monkeypatch):
    profile = _managed_profile()
    resolved, _ = resolve_herdr_launch_profile(profile)
    record = {"name": "agent-1", "state": "idle", "pane_id": "pane-1"}
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: {"revision": "after-restart"})
    monkeypatch.setattr(herdr_plugin, "_snapshot_agent_records", lambda _snapshot: [record])
    monkeypatch.setattr(
        herdr_plugin,
        "_metadata_tokens",
        lambda _record: {
            "bh_launch_id": "launch-1",
            "bh_operation_id": "operation-1",
            "bh_generation": "8",
            "bh_launch_spec_digest": launch_spec_digest(resolved),
            "bh_seat_contract_digest": resolved.seat_contract_digest,
        },
    )
    herdr_plugin._validate_managed_generation("agent-1", profile, resolved)
    with pytest.raises(RuntimeError, match="generation"):
        herdr_plugin._validate_managed_generation("agent-1", _managed_profile(9), resolved)


def test_generation_fenced_reap_is_idempotent_and_cannot_stop_successor(monkeypatch):
    record = {"name": "agent-1", "state": "idle", "pane_id": "pane-1"}
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: {"revision": "r1"})
    monkeypatch.setattr(herdr_plugin, "_snapshot_agent_records", lambda _snapshot: [record])
    monkeypatch.setattr(
        herdr_plugin,
        "_metadata_tokens",
        lambda _record: {
            "bh_generation": "8",
            "bh_launch_spec_digest": "sha256:" + "8" * 64,
        },
    )
    assert herdr_plugin._generation_reap_matches("agent-1", "pane-1", 8, "sha256:" + "8" * 64)
    assert not herdr_plugin._generation_reap_matches("agent-1", "pane-1", 7, "sha256:" + "7" * 64)
    monkeypatch.setattr(herdr_plugin, "_snapshot_agent_records", lambda _snapshot: [])
    assert not herdr_plugin._generation_reap_matches("agent-1", "pane-1", 8, "sha256:" + "8" * 64)
