"""Pure ``EpicSchedule`` projection over one accepted state-stream export.

The polling adapter owns the backend read and normalization boundary.  This module consumes only
that accepted carrier, then delegates all grouping decisions to :mod:`beadhive.schedule`.  It must
therefore stay free of configuration, model-routing, git, and lifecycle-command reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import schedule
from .state_stream import EpicSchedule, ScheduleGroup, StateStreamContractError, projection_id

if TYPE_CHECKING:
    from .state_stream import StreamIssue
    from .state_stream_polling import AcceptedExport


def _scheduler_bead(issue: StreamIssue) -> dict[str, object]:
    """Return the smallest normalized input accepted by ``schedule.plan_schedule``."""

    return {
        "id": issue.id,
        "issue_type": issue.issue_type,
        "labels": list(issue.labels),
        "dependencies": [
            {
                "issue_id": dependency.issue_id,
                "depends_on_id": dependency.depends_on_id,
                "type": dependency.type,
            }
            for dependency in issue.dependencies
        ],
    }


def _schedule_groups(
    planned: schedule.Schedule, by_id: dict[str, dict[str, object]]
) -> tuple[ScheduleGroup, ...]:
    planner: list[ScheduleGroup] = []
    chains: list[ScheduleGroup] = []
    for group in planned.groups:
        if group.kind == "planner":
            members = tuple(sorted(group.ids))
            batch = schedule.batch_group(by_id[members[0]]) if members else ""
            if not members or not batch:
                raise StateStreamContractError("scheduler returned an invalid planner group")
            planner.append(ScheduleGroup(kind="planner", batch=batch, issue_ids=members))
        elif group.kind == "chain":
            if not group.ids:
                raise StateStreamContractError("scheduler returned an empty chain group")
            chains.append(ScheduleGroup(kind="chain", batch=None, issue_ids=tuple(group.ids)))
        else:
            raise StateStreamContractError(
                f"scheduler returned runtime-only group kind {group.kind!r}"
            )
    planner.sort(key=lambda group: group.batch or "")
    chains.sort(key=lambda group: group.issue_ids[0])
    return tuple((*planner, *chains))


def project_epic_schedules(accepted: AcceptedExport) -> tuple[EpicSchedule, ...]:
    """Derive one deterministic schedule for every epic in the accepted scope.

    Closed epics remain observable.  Only non-closed direct children in the same hive are passed
    to the scheduler, in lexical ID order, with a non-restrictive projection-owned size cap.
    """

    records = tuple(accepted.records)
    epics = sorted(
        (record.issue for record in records if record.issue.issue_type == "epic"),
        key=lambda issue: (issue.hive, issue.id),
    )
    projected: list[EpicSchedule] = []
    for epic in epics:
        candidates = sorted(
            (
                record.issue
                for record in records
                if record.issue.hive == epic.hive
                and record.issue.parent_id == epic.id
                and record.issue.status != "closed"
            ),
            key=lambda issue: issue.id,
        )
        scheduler_beads = [_scheduler_bead(issue) for issue in candidates]
        by_id = {str(bead["id"]): bead for bead in scheduler_beads}
        leaf_count = sum(not schedule.is_epic(bead) for bead in scheduler_beads)
        planned = schedule.plan_schedule(
            scheduler_beads,
            max_size=max(1, leaf_count),
        )
        projected.append(
            EpicSchedule(
                id=projection_id("epic-schedule", (epic.hive, epic.id)),
                hive=epic.hive,
                epic_id=epic.id,
                groups=_schedule_groups(planned, by_id),
                singletons=tuple(sorted(planned.singletons)),
                coordinators=tuple(sorted(planned.coordinators)),
            )
        )
    return tuple(sorted(projected, key=lambda item: item.id))
