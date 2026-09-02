"""Compatibility and callback-adapter contracts for the state capability."""

from __future__ import annotations

from datetime import UTC, datetime

from beadhive import agent_run_summary, state_services, state_stream
from beadhive.modules import state


class FixedClock:
    def now(self):
        return datetime(2026, 9, 2, tzinfo=UTC)


def test_legacy_contract_facades_reexport_owned_state_types() -> None:
    assert state_stream.StreamRequest is state.StreamRequest
    assert state_stream.ProviderSnapshot is state.ProviderSnapshot
    assert state_stream.stream_frames is state.stream_frames
    assert agent_run_summary.AgentRunSummary is state.AgentRunSummary
    assert agent_run_summary.Freshness is state.Freshness


def test_callback_validation_adapter_preserves_complete_payload() -> None:
    saved = []
    record = state.ValidationRecord.from_mapping(
        {
            "run_id": "run-1",
            "lifecycle": "completed",
            "verdict": "green",
            "tree": "tree-1",
            "command_hash": "cmd-1",
            "schema": 2,
        }
    )
    service = state_services.validation_record_service(
        read=lambda _run_id: record,
        latest=lambda _query: record,
        write=lambda value: saved.append(value) or value,
        clock=FixedClock(),
    )

    assert service.write(record).to_mapping()["schema"] == 2
    assert service.read("run-1") is record
    assert service.latest(state.ValidationQuery("tree-1", "cmd-1")) is record
    assert saved == [record]


def test_callback_projection_adapter_forwards_typed_requests() -> None:
    request = state.StreamRequest(state.StreamScope.HIVE, hive="github/beadhive/beadhive")
    snapshot = state.ProviderSnapshot(state.StreamScope.HIVE, "rev-1", "2026-09-02Z", ())
    calls = []
    service = state_services.read_projection_service(
        snapshot=lambda selected: calls.append(selected) or snapshot,
        frames=lambda selected: calls.append(selected) or (),
        clock=FixedClock(),
    )

    assert service.snapshot(request) is snapshot
    assert tuple(service.frames(request)) == ()
    assert calls == [request, request]
