"""Compact, byte-first Development snapshot projection at the daemon boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from beadhive import daemon_contract, operator_contract, state_stream
from beadhive.agent_run_summary import AgentRunState, AgentRunSummary, Freshness
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
    priority: str = "P1",
    issue_type: str = "task",
    hive: str = HIVE,
    title: str | None = None,
    updated_at: str = NOW,
    labels: tuple[str, ...] = (),
) -> state_stream.StreamIssue:
    return state_stream.StreamIssue(
        id=issue_id,
        hive=hive,
        issue_type=issue_type,
        status=status,
        priority=priority,
        title=title or f"Issue {issue_id}",
        updated_at=updated_at,
        labels=labels,
    )


def _dependency(
    issue_id: str, depends_on_id: str, *, hive: str = HIVE, kind: str = "blocks"
) -> state_stream.WorkDependency:
    return state_stream.WorkDependency(
        id=state_stream.projection_id("work-dependency", (hive, issue_id, depends_on_id, kind)),
        hive=hive,
        issue_id=issue_id,
        depends_on_id=depends_on_id,
        type=kind,
        created_at=NOW,
        created_by="planner/test",
    )


def _runtime(*summaries: AgentRunSummary, coverage: Coverage = Coverage.COMPLETE):
    return AgentRunSnapshot(
        host_id="host-1",
        source_id="source-1",
        revision="runtime-1",
        summaries=summaries,
        coverage=coverage,
        coverage_reason=None,
        freshness=Freshness(),
    )


def _project(
    snapshot: state_stream.ProviderSnapshot,
    runtime: AgentRunSnapshot | None = None,
    *,
    producer_epoch: str = "a" * 32,
    sequence: int = 0,
    observed_at: int = 1_000,
) -> dict[str, object]:
    return operator_contract.hive_operator_snapshot(
        ENTRY,
        snapshot,
        runtime or _runtime(),
        producer_epoch=producer_epoch,
        sequence=sequence,
        observed_at=observed_at,
    )


def _snapshot(issues, **kwargs):
    return state_stream.ProviderSnapshot(
        scope="hive", revision="beads-1", as_of=NOW, issues=tuple(issues), **kwargs
    )


def test_projection_is_compact_strict_and_counts_only_the_exact_hive() -> None:
    gate = state_stream.GateRequest(
        id=state_stream.projection_id("gate-request", (HIVE, "gate-1")),
        hive=HIVE,
        gate_id="gate-1",
        blocks=("bh-current",),
        gate_type="human",
        gate_kind="review",
        status="open",
        reason="review",
        opened_at=NOW,
        resolved_at=None,
    )
    projected = _project(
        _snapshot(
            (
                _issue("bh-current", labels=tuple(f"label-{index}" for index in range(15))),
                _issue("bh-closed", status="closed"),
                _issue("bh-event", issue_type="event"),
                _issue("bh-current", hive=OTHER_HIVE, status="closed"),
            ),
            work_dependencies=(
                _dependency("bh-current", "bh-closed"),
                _dependency("bh-current", "foreign", hive=OTHER_HIVE),
            ),
            gate_requests=(gate,),
        ),
        _runtime(AgentRunSummary("bh-current", "session", AgentRunState.ACTIVE)),
    )
    assert set(projected) == {
        "schemaVersion",
        "hive",
        "revision",
        "generatedAt",
        "cursor",
        "projectionPolicy",
        "limits",
        "coverage",
        "workItems",
    }
    assert projected["projectionPolicy"] == "beadhive.snapshot-summary/v1"
    assert projected["limits"] == {"maxBytes": 917_504, "maxWorkItems": 4_096}
    assert len(projected["workItems"]) == 1
    item = projected["workItems"][0]
    assert (item["id"], item["readiness"]) == ("bh-current", "blocked")
    assert (item["blockerCount"], item["openGateCount"], item["liveAgentCount"]) == (0, 1, 1)
    assert len(item["labels"]) == 12 and item["remainingLabelCount"] == 3
    assert daemon_contract.HiveSnapshotResponse.model_validate(projected).to_wire() == projected


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
        _issue(f"bh-scale-{index}", status=status, title=f"Factory item {index}")
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
            kind=f"blocks-{index}",
        )
        for index in range(7_439)
    )
    return _snapshot(issues, work_dependencies=dependencies)


def test_factory_scale_1344_current_items_are_complete_and_under_target() -> None:
    projected = _project(_factory_scale_snapshot(open_count=1_313))
    encoded = json.dumps(
        projected, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    assert len(projected["workItems"]) == 1_344
    assert projected["coverage"]["state"] == "complete"
    assert projected["coverage"]["eligible"] == 1_344
    assert projected["coverage"]["reason"] is None
    assert len(encoded) <= operator_contract.DEVELOPMENT_SNAPSHOT_MAX_BYTES
    daemon_contract.HiveSnapshotResponse.model_validate(projected)


@pytest.mark.parametrize("count", [4_095, 4_096, 4_097])
def test_structural_cap_bounds_candidate_materialization(count: int) -> None:
    candidates, eligible = operator_contract._bounded_summary_candidates(
        tuple(_issue(f"bh-{index:05}") for index in range(count)), HIVE
    )
    assert eligible == count
    assert len(candidates) == min(count, 4_096)


def test_bounded_selection_replaces_the_retained_worst_record() -> None:
    worst_first = tuple(
        _issue(f"bh-open-{index:04}", status="open", priority="P4") for index in range(4_096)
    )
    best_last = _issue("bh-active-best", status="in_progress", priority="P0")

    candidates, eligible = operator_contract._bounded_summary_candidates(
        (*worst_first, best_last), HIVE
    )

    ids = {issue.id for issue in candidates}
    assert eligible == 4_097
    assert len(candidates) == 4_096
    assert candidates[0].id == "bh-active-best"
    assert "bh-open-4095" not in ids


def test_deterministic_order_precedes_byte_selection() -> None:
    projected = _project(
        _snapshot(
            (
                _issue("open-p0", priority="P0", status="open"),
                _issue("blocked-p4", priority="P4", status="blocked"),
                _issue("active-p4", priority="P4", status="in_progress"),
                _issue("active-p0-b", priority="P0", status="in_progress"),
                _issue("active-p0-a", priority="P0", status="in_progress"),
            )
        )
    )
    assert [item["id"] for item in projected["workItems"]] == [
        "active-p0-a",
        "active-p0-b",
        "active-p4",
        "blocked-p4",
        "open-p0",
    ]


def test_byte_budget_returns_truthful_prefix_with_unicode_and_cursor_headroom() -> None:
    source = _snapshot(_issue(f"bh-{index:04}", title="🧪" * 1_000) for index in range(1_000))
    projected = _project(source)
    encoded = json.dumps(
        projected, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    assert len(encoded) <= 917_504
    assert 0 < len(projected["workItems"]) < 1_000
    assert projected["coverage"]["state"] == "partial"
    assert projected["coverage"]["reason"] == "byte_budget"
    assert projected["coverage"]["returned"] == len(projected["workItems"])
    assert projected["coverage"]["eligible"] == 1_000

    maximum = _project(source, sequence=2**53 - 1, observed_at=2**53 - 1)
    maximum_bytes = json.dumps(
        maximum, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    assert len(maximum["workItems"]) == len(projected["workItems"])
    assert len(maximum_bytes) <= 917_504


def test_source_partial_and_selection_partial_are_both_preserved() -> None:
    source = state_stream.ProviderSnapshot(
        scope="hive",
        revision="beads-partial",
        as_of=NOW,
        issues=tuple(_issue(f"bh-{index}") for index in range(4_097)),
        partial=True,
        partial_reason="provider_page_missing",
    )
    projected = _project(source)
    coverage = projected["coverage"]
    assert coverage["state"] == "partial"
    assert coverage["reason"] in {"byte_budget", "structural_cap"}
    assert coverage["sources"]["beads"]["state"] == "partial"
    assert "provider_page_missing" in coverage["sources"]["beads"]["detail"]


def test_duplicate_selected_id_and_invalid_priority_fail_closed() -> None:
    with pytest.raises(operator_contract.SnapshotProjectionUnavailable, match="identity"):
        _project(_snapshot((_issue("dup"), _issue("dup"))))
    with pytest.raises(operator_contract.SnapshotProjectionUnavailable, match="priority"):
        _project(_snapshot((_issue("bad", priority="urgent"),)))


def test_restart_stability_excludes_only_fresh_stream_cursor_values() -> None:
    source = _snapshot((_issue("bh-1", updated_at=""),))
    first = _project(source, producer_epoch="a" * 32, observed_at=1_000)
    restarted = _project(source, producer_epoch="b" * 32, observed_at=2_000)
    assert {k: v for k, v in first.items() if k != "cursor"} == {
        k: v for k, v in restarted.items() if k != "cursor"
    }
    assert first["workItems"][0]["updatedAt"] == 0
