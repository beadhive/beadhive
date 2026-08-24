"""GateRequest mapping and retention contract (bh-5wpb6.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from beadhive import state_stream
from beadhive.state_stream_gate_projection import project_gate_requests

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def dependency(issue_id: str, gate_id: str, *, hive: str = "beadhive"):
    return state_stream.WorkDependency(
        id=state_stream.projection_id("work-dependency", (hive, issue_id, gate_id, "blocks")),
        hive=hive,
        issue_id=issue_id,
        depends_on_id=gate_id,
        type="blocks",
        created_at=None,
        created_by=None,
    )


def gate(gate_id: str = "gate-1", **overrides):
    row = {
        "id": gate_id,
        "issue_type": "gate",
        "status": "open",
        "await_type": "human",
        "description": "Ad-hoc gate blocking bh-child\n\nReason: bh:review abc1234",
        "created_at": "2026-08-24T10:00:00Z",
    }
    row.update(overrides)
    return row


def request(scope="hive"):
    return state_stream.StreamRequest(scope, hive="beadhive" if scope == "hive" else None)


def test_hive_gate_maps_exact_fields_and_reverse_sorted_blocks():
    row = gate(reason="raw reason wins")
    dependencies = (
        dependency("bh-z", "gate-1"),
        dependency("bh-a", "gate-1"),
        dependency("bh-a", "gate-1"),
        dependency("other", "another-gate"),
    )

    projected = project_gate_requests(
        [row], request=request(), work_dependencies=dependencies, as_of=NOW
    )

    assert projected.partial_reason is None
    assert projected.gate_requests == (
        state_stream.GateRequest(
            id=state_stream.projection_id("gate-request", ("beadhive", "gate-1")),
            hive="beadhive",
            gate_id="gate-1",
            blocks=("bh-a", "bh-z"),
            gate_type="human",
            gate_kind="review",
            status="open",
            reason="raw reason wins",
            opened_at="2026-08-24T10:00:00Z",
            resolved_at=None,
        ),
    )


def test_reason_falls_back_to_first_case_insensitive_description_marker():
    projected = project_gate_requests(
        [gate(description="header\nrEaSoN: keep the whole\nreason tail")],
        request=request(),
        work_dependencies=(),
        as_of=NOW,
    )

    assert projected.gate_requests[0].reason == "keep the whole\nreason tail"


@pytest.mark.parametrize(
    ("description", "raw_reason", "expected"),
    [
        ("Reason: bh:review abc1234", None, "review"),
        ("Reason: review deadbee", None, "review"),
        ("Reason: security: sbom", None, "security"),
        ("Reason: kickoff bh-epic", None, "kickoff"),
        ("Reason: release-hold: bh-epic kickoff later", None, "other"),
        ("Reason: review the rollout", None, "other"),
        ("unmarked", "security: policy", "security"),
    ],
)
def test_gate_kind_reuses_shared_classifier_and_public_mapping(description, raw_reason, expected):
    row = gate(description=description)
    if raw_reason is not None:
        row["reason"] = raw_reason

    projected = project_gate_requests([row], request=request(), work_dependencies=(), as_of=NOW)

    assert projected.gate_requests[0].gate_kind == expected


def test_resolved_gate_retention_is_inclusive_and_ages_into_removal_state():
    cutoff = NOW - timedelta(hours=24)
    rows = [
        gate("at-now", status="closed", closed_at=NOW.isoformat()),
        gate("at-cutoff", status="closed", closed_at=cutoff.isoformat()),
        gate(
            "too-old", status="closed", closed_at=(cutoff - timedelta(microseconds=1)).isoformat()
        ),
        gate("future", status="closed", closed_at=(NOW + timedelta(microseconds=1)).isoformat()),
    ]

    projected = project_gate_requests(rows, request=request(), work_dependencies=(), as_of=NOW)

    assert {item.gate_id for item in projected.gate_requests} == {"at-now", "at-cutoff"}
    assert all(item.status == "resolved" for item in projected.gate_requests)
    assert projected.partial_reason is None


@pytest.mark.parametrize("closed_at", [None, "", "not-a-time"])
def test_resolved_gate_without_timestamp_is_omitted_and_partial(closed_at):
    projected = project_gate_requests(
        [gate(status="closed", closed_at=closed_at)],
        request=request(),
        work_dependencies=(),
        as_of=NOW,
    )

    assert projected.gate_requests == ()
    assert projected.partial_reason == "gate_resolution_timestamp_unavailable"


def test_aggregate_hive_requires_exactly_one_reverse_dependency_candidate():
    linked = project_gate_requests(
        [gate()],
        request=request("hub"),
        work_dependencies=(dependency("bh-child", "gate-1"),),
        as_of=NOW,
    )
    orphan = project_gate_requests(
        [gate(description="blocking beadhive-looking-id")],
        request=request("hub"),
        work_dependencies=(),
        as_of=NOW,
    )
    conflict = project_gate_requests(
        [gate()],
        request=request("factory"),
        work_dependencies=(
            dependency("bh-child", "gate-1", hive="beadhive"),
            dependency("other-child", "gate-1", hive="other"),
        ),
        as_of=NOW,
    )

    assert linked.gate_requests[0].hive == "beadhive"
    assert linked.gate_requests[0].blocks == ("bh-child",)
    assert orphan.gate_requests == conflict.gate_requests == ()
    assert orphan.partial_reason == conflict.partial_reason == "gate_hive_identity_unavailable"


def test_malformed_and_duplicate_gate_rows_degrade_without_ambiguous_entities():
    projected = project_gate_requests(
        ["not-a-row", {"issue_type": "task"}, gate(), gate(status="closed")],
        request=request(),
        work_dependencies=(),
        as_of=NOW,
    )

    assert projected.gate_requests == ()
    assert projected.partial_reason == "invalid_gate_record"


def test_open_gate_survives_bad_closed_at_but_marks_partial():
    projected = project_gate_requests(
        [gate(closed_at="not-a-time")],
        request=request(),
        work_dependencies=(),
        as_of=NOW,
    )

    assert projected.gate_requests[0].status == "open"
    assert projected.gate_requests[0].resolved_at is None
    assert projected.partial_reason == "gate_resolution_timestamp_unavailable"
