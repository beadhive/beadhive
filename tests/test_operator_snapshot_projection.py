"""Bounded Development snapshot projection at the host-daemon boundary."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from beadhive import daemon_contract, operator_contract, state_stream
from beadhive.agent_run_summary import Freshness
from beadhive.public_readers import AgentRunSnapshot, Coverage

HIVE = "github/beadhive/beadhive"
OTHER_HIVE = "github/beadhive/other"
NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
ENTRY = {
    "provider": "github",
    "org": "beadhive",
    "repo": "beadhive",
    "prefix": "bh",
    "kind": "org-native",
}


def _issue(
    issue_id: str,
    *,
    status: str = "open",
    issue_type: str = "task",
    hive: str = HIVE,
    parent_id: str | None = None,
    title: str | None = None,
) -> state_stream.StreamIssue:
    return state_stream.StreamIssue(
        id=issue_id,
        hive=hive,
        issue_type=issue_type,
        status=status,
        priority="P1",
        title=title or f"Issue {issue_id}",
        updated_at=NOW,
        parent_id=parent_id,
    )


def _dependency(issue_id: str, depends_on_id: str, kind: str = "blocks"):
    return state_stream.WorkDependency(
        id=state_stream.projection_id("work-dependency", (HIVE, issue_id, depends_on_id, kind)),
        hive=HIVE,
        issue_id=issue_id,
        depends_on_id=depends_on_id,
        type=kind,
        created_at=NOW,
        created_by="planner/test",
    )


def _runtime() -> AgentRunSnapshot:
    return AgentRunSnapshot(
        host_id="host-1",
        source_id="source-1",
        revision="runtime-1",
        summaries=(),
        coverage=Coverage.UNKNOWN,
        coverage_reason="source_missing",
        freshness=Freshness(),
    )


def _project(snapshot: state_stream.ProviderSnapshot) -> dict[str, object]:
    return operator_contract.hive_operator_snapshot(
        ENTRY,
        snapshot,
        _runtime(),
        producer_epoch="a" * 32,
        sequence=0,
        observed_at=1_000,
    )


def test_projection_removes_history_internal_work_and_dangling_relationships() -> None:
    issues = (
        _issue("bh-epic", issue_type="epic"),
        _issue("bh-current", parent_id="bh-epic"),
        _issue("bh-closed", status="closed", parent_id="bh-epic"),
        _issue("bh-deferred", status="deferred"),
        _issue("bh-event", issue_type="event"),
        _issue("bh-gate", issue_type="gate"),
        _issue("other-1", hive=OTHER_HIVE),
    )
    dependencies = (
        _dependency("bh-current", "bh-epic"),
        _dependency("bh-current", "bh-closed"),
    )
    assignments = (
        state_stream.Assignment(
            id=state_stream.projection_id("assignment", (HIVE, "bh-current")),
            hive=HIVE,
            issue_id="bh-current",
            seat="dev/current",
        ),
        state_stream.Assignment(
            id=state_stream.projection_id("assignment", (HIVE, "bh-closed")),
            hive=HIVE,
            issue_id="bh-closed",
            seat="dev/history",
        ),
    )
    gate = state_stream.GateRequest(
        id=state_stream.projection_id("gate-request", (HIVE, "gate-review")),
        hive=HIVE,
        gate_id="gate-review",
        blocks=("bh-current", "bh-closed"),
        gate_type="human",
        gate_kind="review",
        status="open",
        reason="review",
        opened_at=NOW,
        resolved_at=None,
    )
    schedule = state_stream.EpicSchedule(
        id=state_stream.projection_id("epic-schedule", (HIVE, "bh-epic")),
        hive=HIVE,
        epic_id="bh-epic",
        groups=(
            state_stream.ScheduleGroup(
                kind="chain", batch=None, issue_ids=("bh-current", "bh-closed")
            ),
        ),
        singletons=("bh-closed",),
        coordinators=(),
    )
    projected = _project(
        state_stream.ProviderSnapshot(
            scope="hive",
            revision="beads-1",
            as_of=NOW,
            issues=issues,
            work_dependencies=dependencies,
            assignments=assignments,
            gate_requests=(gate,),
            epic_schedules=(schedule,),
        )
    )

    retained = {item["record"]["id"] for item in projected["workItems"]}
    assert retained == {"bh-current", "bh-epic"}
    assert len(projected["dependencies"]) == 1
    assert projected["dependencies"][0]["dependentId"]["id"] in retained
    assert projected["dependencies"][0]["prerequisiteId"]["id"] in retained
    assert [item["workItemId"]["id"] for item in projected["assignments"]] == ["bh-current"]
    assert [item["id"] for item in projected["gates"][0]["blocks"]] == ["bh-current"]
    assert projected["epics"][0]["childIds"] == [{"hiveId": HIVE, "id": "bh-current"}]
    assert projected["schedules"][0]["groups"][0]["workItemIds"] == [
        {"hiveId": HIVE, "id": "bh-current"}
    ]


def _factory_scale_snapshot(*, open_count: int) -> state_stream.ProviderSnapshot:
    statuses = (
        ["open"] * open_count
        + ["in_progress"] * 30
        + ["blocked"]
        + ["closed"] * (6_546 - open_count)
        + ["deferred"] * 36
    )
    assert len(statuses) == 6_613
    issues = tuple(
        _issue(
            f"bh-scale-{index}",
            status=status,
            title=f"Factory item {index} " + "x" * 40,
        )
        for index, status in enumerate(statuses)
    )
    current_count = open_count + 31
    dependencies = tuple(
        _dependency(
            f"bh-scale-{index % current_count}"
            if index < 400
            else f"bh-scale-{current_count + index % (len(issues) - current_count)}",
            f"bh-scale-{(index + 1) % current_count}"
            if index < 400
            else f"bh-scale-{current_count + (index + 1) % (len(issues) - current_count)}",
            f"blocks-{index}",
        )
        for index in range(7_439)
    )
    return state_stream.ProviderSnapshot(
        scope="hive",
        revision="factory-scale-1",
        as_of=NOW,
        issues=issues,
        work_dependencies=dependencies,
    )


def test_factory_scale_history_is_projected_before_the_one_mib_boundary() -> None:
    source = _factory_scale_snapshot(open_count=900)
    canonical_bytes = len(
        json.dumps(
            {
                "issues": [asdict(item) for item in source.issues],
                "dependencies": [asdict(item) for item in source.work_dependencies],
            },
            separators=(",", ":"),
        ).encode()
    )
    assert canonical_bytes > operator_contract.DEVELOPMENT_SNAPSHOT_MAX_BYTES

    projected = _project(source)
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(projected["workItems"]) == 931
    assert len(encoded) <= operator_contract.DEVELOPMENT_SNAPSHOT_MAX_BYTES
    assert daemon_contract.HiveSnapshotResponse.model_validate(projected)


def test_factory_scale_current_work_overload_fails_closed() -> None:
    source = _factory_scale_snapshot(open_count=1_313)

    with pytest.raises(
        operator_contract.SnapshotProjectionUnavailable,
        match="work-item limit exceeded",
    ):
        _project(source)


def test_snapshot_over_one_mib_after_item_projection_fails_closed() -> None:
    source = state_stream.ProviderSnapshot(
        scope="hive",
        revision="oversized-current-1",
        as_of=NOW,
        issues=tuple(_issue(f"bh-large-{index}", title="x" * 2_000) for index in range(600)),
    )

    with pytest.raises(
        operator_contract.SnapshotProjectionUnavailable,
        match="byte limit exceeded",
    ):
        _project(source)


def test_snapshot_size_reserves_json_safe_cursor_sequence_and_timestamp_widths() -> None:
    source = state_stream.ProviderSnapshot(
        scope="hive",
        revision="cursor-headroom-1",
        as_of=NOW,
        issues=tuple(
            _issue(
                f"bh-{index}",
                title="x" * (1_136 if index == 0 else 546),
            )
            for index in range(operator_contract.DEVELOPMENT_WORK_ITEM_LIMIT)
        ),
    )

    with pytest.raises(
        operator_contract.SnapshotProjectionUnavailable,
        match="byte limit exceeded",
    ):
        _project(source)
