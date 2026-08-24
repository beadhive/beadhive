"""EpicSchedule projection from the polling adapter's accepted export carrier."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from beadhive import schedule, state_stream, state_stream_epic_schedule, state_stream_polling


def issue(
    issue_id: str,
    *,
    hive: str = "beadhive",
    issue_type: str = "task",
    status: str = "open",
    parent_id: str | None = None,
    labels: tuple[str, ...] = (),
    dependencies: tuple[state_stream.StreamDependency, ...] = (),
) -> state_stream.StreamIssue:
    return state_stream.StreamIssue(
        id=issue_id,
        hive=hive,
        issue_type=issue_type,
        status=status,
        priority="P2",
        title=f"Issue {issue_id}",
        updated_at="2026-08-24T00:00:00Z",
        parent_id=parent_id,
        labels=labels,
        dependencies=dependencies,
    )


def accepted(*issues: state_stream.StreamIssue) -> state_stream_polling.AcceptedExport:
    return state_stream_polling.AcceptedExport(
        records=tuple(
            state_stream_polling.AcceptedExportRecord(raw={"id": item.id}, issue=item)
            for item in issues
        )
    )


def blocks(child: str, parent: str) -> tuple[state_stream.StreamDependency, ...]:
    return (state_stream.StreamDependency(child, parent, "blocks"),)


def test_projects_exact_scheduler_vocabulary_ordering_and_candidates(monkeypatch) -> None:
    source = accepted(
        issue("bh-root", issue_type="epic", status="closed"),
        issue("bh-z2", parent_id="bh-root", labels=("batch:alpha",)),
        issue("bh-z1", parent_id="bh-root", labels=("batch:alpha",)),
        issue("bh-a2", parent_id="bh-root", labels=("batch:zeta",)),
        issue("bh-a1", parent_id="bh-root", labels=("batch:zeta",)),
        issue("bh-chain-2", parent_id="bh-root", dependencies=blocks("bh-chain-2", "bh-chain-1")),
        issue("bh-chain-1", parent_id="bh-root"),
        issue("bh-single", parent_id="bh-root", status="blocked"),
        issue("bh-child-epic", parent_id="bh-root", issue_type="epic", status="in_progress"),
        issue("bh-closed", parent_id="bh-root", status="closed"),
        issue("bh-grandchild", parent_id="bh-z1"),
        issue("bh-foreign", hive="other", parent_id="bh-root"),
        issue("bh-empty", issue_type="epic"),
    )
    real_plan = schedule.plan_schedule
    calls: list[tuple[list[dict], dict]] = []

    def recording_plan(beads, **kwargs):
        calls.append((beads, kwargs))
        return real_plan(beads, **kwargs)

    monkeypatch.setattr(state_stream_epic_schedule.schedule, "plan_schedule", recording_plan)

    projected = state_stream_epic_schedule.project_epic_schedules(source)
    by_epic = {item.epic_id: item for item in projected}

    assert set(by_epic) == {"bh-root", "bh-child-epic", "bh-empty"}
    assert by_epic["bh-empty"].groups == ()
    assert by_epic["bh-empty"].singletons == ()
    assert by_epic["bh-empty"].coordinators == ()
    assert by_epic["bh-child-epic"].groups == ()
    root = by_epic["bh-root"]
    assert root.id == state_stream.projection_id("epic-schedule", ("beadhive", "bh-root"))
    assert root.groups == (
        state_stream.ScheduleGroup("planner", "alpha", ("bh-z1", "bh-z2")),
        state_stream.ScheduleGroup("planner", "zeta", ("bh-a1", "bh-a2")),
        state_stream.ScheduleGroup("chain", None, ("bh-chain-1", "bh-chain-2")),
    )
    assert root.singletons == ("bh-single",)
    assert root.coordinators == ("bh-child-epic",)

    root_call = next(beads_and_kwargs for beads_and_kwargs in calls if beads_and_kwargs[0])
    beads, kwargs = root_call
    assert [bead["id"] for bead in beads] == sorted(bead["id"] for bead in beads)
    assert kwargs == {"max_size": 7}
    assert "bh-closed" not in {bead["id"] for bead in beads}
    assert "bh-grandchild" not in {bead["id"] for bead in beads}
    assert "bh-foreign" not in {bead["id"] for bead in beads}


def test_no_batch_independent_children_are_singletons_and_input_order_is_irrelevant() -> None:
    records = (
        issue("bh-root", issue_type="epic"),
        issue("bh-b", parent_id="bh-root"),
        issue("bh-a", parent_id="bh-root"),
    )

    forward = state_stream_epic_schedule.project_epic_schedules(accepted(*records))
    reverse = state_stream_epic_schedule.project_epic_schedules(accepted(*reversed(records)))

    assert forward == reverse
    assert forward[0].groups == ()
    assert forward[0].singletons == ("bh-a", "bh-b")


def test_runtime_only_scheduler_group_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        state_stream_epic_schedule.schedule,
        "plan_schedule",
        lambda _beads, **_kwargs: schedule.Schedule(
            groups=[schedule.Group("collapsed", ("bh-a",), "operator override")],
            singletons=[],
        ),
    )

    with pytest.raises(state_stream.StateStreamContractError, match="runtime-only"):
        state_stream_epic_schedule.project_epic_schedules(
            accepted(
                issue("bh-root", issue_type="epic"),
                issue("bh-a", parent_id="bh-root"),
            )
        )


def raw_issue(
    issue_id: str,
    *,
    issue_type: str = "task",
    parent_id: str | None = None,
    labels: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "_type": "issue",
        "id": issue_id,
        "title": f"Issue {issue_id}",
        "issue_type": issue_type,
        "status": "open",
        "priority": 2,
        "updated_at": "2026-08-24T00:00:00Z",
        "labels": list(labels),
        "dependencies": [],
        "parent_id": parent_id,
    }


class ExportBackend:
    def __init__(self, refreshes: list[list[dict[str, object]]]) -> None:
        self.refreshes = refreshes
        self.calls = 0

    def export_jsonl(self, _cwd, out_path, *, env=None):
        del env
        index = min(self.calls, len(self.refreshes) - 1)
        self.calls += 1
        out_path.write_text(
            "".join(f"{json.dumps(row)}\n" for row in self.refreshes[index]),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess([], 0, "", "")


def test_polling_composes_schedule_into_revision_delta_and_removal(tmp_path, monkeypatch) -> None:
    backend = ExportBackend(
        [
            [raw_issue("bh-root", issue_type="epic"), raw_issue("bh-a", parent_id="bh-root")],
            [
                raw_issue("bh-root", issue_type="epic"),
                raw_issue("bh-a", parent_id="bh-root", labels=("batch:ui",)),
                raw_issue("bh-b", parent_id="bh-root", labels=("batch:ui",)),
            ],
            [raw_issue("bh-a", parent_id="bh-root")],
        ]
    )
    adapter = state_stream_polling.PollingStateStreamProvider(
        {},
        backend=backend,
        poll_interval=0,
        now=lambda: datetime(2026, 8, 24, tzinfo=UTC),
    )
    monkeypatch.setattr(adapter, "_target", lambda _request: tmp_path)
    request = state_stream.StreamRequest("hive", hive="beadhive")

    snapshots = [adapter.refresh(request) for _ in range(3)]

    assert backend.calls == 3
    first, changed, removed = snapshots
    assert first.epic_schedules[0].singletons == ("bh-a",)
    assert changed.epic_schedules[0].groups == (
        state_stream.ScheduleGroup("planner", "ui", ("bh-a", "bh-b")),
    )
    assert first.revision != changed.revision != removed.revision

    provider = type(
        "ProviderDouble",
        (),
        {"name": "double", "updates": lambda _self, _request: iter(snapshots)},
    )()
    initial, replacement_delta, removal_delta = state_stream.stream_frames(provider, request)
    schedule_id = first.epic_schedules[0].id
    assert initial.epic_schedules == first.epic_schedules
    assert replacement_delta.epic_schedules_changed == changed.epic_schedules
    assert replacement_delta.epic_schedules_removed == ()
    assert removal_delta.epic_schedules_changed == ()
    assert removal_delta.epic_schedules_removed == (schedule_id,)
