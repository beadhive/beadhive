"""Bounded, backend-neutral work-item queue and exact-detail projections."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from . import config, operator_actions, release_order
from .agent_run_summary import AgentRunState
from .operator_sources import OperatorSourceError
from .public_readers import AgentRunSnapshot
from .state_stream import GateRequest, ProviderSnapshot, StreamIssue, WorkDependency

SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
QUEUES = frozenset({"ready", "active", "blocked", "recent"})
_NON_BLOCKING_DEPENDENCIES = frozenset({"parent-child", "related", "discovered-from"})
_LIVE_AGENT_STATES = frozenset(
    {AgentRunState.STARTING, AgentRunState.ACTIVE, AgentRunState.WAITING}
)


@dataclass(frozen=True)
class WorkItemQuery:
    queue: str
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None
    priorities: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    assignee: str | None = None
    issue_type: str | None = None
    parent: str | None = None
    ordering: str = "beadhive.work-items/v1"

    @property
    def scope(self) -> dict[str, object]:
        return {
            "queue": self.queue,
            "priorities": list(self.priorities),
            "labels": list(self.labels),
            "assignee": self.assignee,
            "type": self.issue_type,
            "parent": self.parent,
            "ordering": self.ordering,
        }


def _millis(value: str | float | int | None) -> int | None:
    if isinstance(value, int | float):
        return int(value * 1000)
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def projection_revision(beads: ProviderSnapshot, runtime: AgentRunSnapshot) -> str:
    value = json.dumps(
        [beads.revision, runtime.revision], separators=(",", ":"), ensure_ascii=True
    ).encode()
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def etag(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f'"sha256:{hashlib.sha256(canonical).hexdigest()}"'


def _encode_cursor(*, hive_id: str, revision: str, query: WorkItemQuery, offset: int) -> str:
    raw = json.dumps(
        {
            "v": 1,
            "hive": hive_id,
            "revision": revision,
            "scope": query.scope,
            "offset": offset,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorSourceError(
            "invalid_work_items_cursor",
            "The work-items cursor is malformed.",
            status_code=400,
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("v") != 1
        or not isinstance(value.get("hive"), str)
        or not isinstance(value.get("revision"), str)
        or not isinstance(value.get("scope"), dict)
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
    ):
        raise OperatorSourceError(
            "invalid_work_items_cursor",
            "The work-items cursor is malformed.",
            status_code=400,
        )
    return value


def _cursor_offset(cursor: str | None, *, hive_id: str, revision: str, query: WorkItemQuery) -> int:
    if cursor is None:
        return 0
    value = _decode_cursor(cursor)
    if value["hive"] != hive_id or value["scope"] != query.scope:
        raise OperatorSourceError(
            "work_items_cursor_scope_mismatch",
            "The work-items cursor belongs to a different hive, queue, filter, or ordering.",
            status_code=409,
        )
    if value["revision"] != revision:
        raise OperatorSourceError(
            "work_items_cursor_revision_mismatch",
            "The work-items snapshot changed; restart pagination without a cursor.",
            status_code=409,
        )
    return value["offset"]


def _priority(value: str) -> int:
    try:
        return int(value.removeprefix("P").removeprefix("p"))
    except ValueError:
        return 2


def _issue_index(beads: ProviderSnapshot) -> dict[str, StreamIssue]:
    return {item.id: item for item in beads.issues}


def _direct_blockers(
    issue: StreamIssue, beads: ProviderSnapshot, by_id: dict[str, StreamIssue]
) -> list[WorkDependency]:
    return sorted(
        (
            dependency
            for dependency in beads.work_dependencies
            if dependency.issue_id == issue.id
            and dependency.type not in _NON_BLOCKING_DEPENDENCIES
            and (
                dependency.depends_on_id not in by_id
                or by_id[dependency.depends_on_id].status != "closed"
            )
        ),
        key=lambda value: (value.depends_on_id, value.type),
    )


def _blocking_gates(issue: StreamIssue, beads: ProviderSnapshot) -> list[GateRequest]:
    return sorted(
        (
            gate
            for gate in beads.gate_requests
            if issue.id in gate.blocks and gate.status in {"open", "pending"}
        ),
        key=lambda value: value.id,
    )


def _readiness(
    issue: StreamIssue, blockers: list[WorkDependency], gates: list[GateRequest]
) -> tuple[str, str]:
    if issue.status == "closed":
        return "completed", "work item is closed"
    if issue.status == "blocked" or blockers or gates:
        reasons = []
        if blockers:
            reasons.append(f"{len(blockers)} unresolved direct dependency")
        if gates:
            reasons.append(f"{len(gates)} open gate")
        return "blocked", " and ".join(reasons) or "work item status is blocked"
    if issue.status == "open":
        return "ready", "open with no unresolved direct dependency or open gate"
    if issue.status == "in_progress":
        return "active", "work item is in progress"
    return "unavailable", f"work item status is {issue.status or 'unknown'}"


def _dependent_count(
    issue: StreamIssue, beads: ProviderSnapshot, by_id: dict[str, StreamIssue]
) -> int:
    return sum(
        1
        for dependency in beads.work_dependencies
        if dependency.depends_on_id == issue.id
        and dependency.type not in _NON_BLOCKING_DEPENDENCIES
        and dependency.issue_id in by_id
        and by_id[dependency.issue_id].status != "closed"
    )


def _agents(issue: StreamIssue, runtime: AgentRunSnapshot) -> list[dict[str, object]]:
    return [
        {
            "id": summary.session_id or f"waiting:{issue.id}",
            "state": summary.state.value,
            "ownerSeat": summary.owner_seat,
            "startedAt": _millis(summary.started_at),
            "updatedAt": _millis(summary.updated_at),
            "endedAt": _millis(summary.ended_at),
        }
        for summary in runtime.summaries
        if summary.bead == issue.id
    ]


def _row(
    issue: StreamIssue,
    *,
    hive_id: str,
    revision: str,
    beads: ProviderSnapshot,
    runtime: AgentRunSnapshot,
    by_id: dict[str, StreamIssue],
) -> dict[str, object]:
    blockers = _direct_blockers(issue, beads, by_id)
    gates = _blocking_gates(issue, beads)
    readiness, reason = _readiness(issue, blockers, gates)
    labels = list(issue.labels)
    agents = _agents(issue, runtime)
    return {
        "ref": {"hiveId": hive_id, "kind": "work-item", "id": issue.id},
        "revision": revision,
        "hiveId": hive_id,
        "id": issue.id,
        "title": issue.title,
        "issueType": issue.issue_type,
        "priority": _priority(issue.priority),
        "status": issue.status,
        "readiness": {"state": readiness, "reason": reason},
        "assignee": issue.assignee,
        "owner": issue.owner,
        "parentId": issue.parent_id,
        "blockerCount": len(blockers),
        "blockedDependentCount": _dependent_count(issue, beads, by_id),
        "labels": labels[:12],
        "remainingLabelCount": max(0, len(labels) - 12),
        "openGateCount": len(gates),
        "liveAgentCount": sum(item["state"] in _LIVE_AGENT_STATES for item in agents),
        "updatedAt": _millis(issue.updated_at),
    }


def _matches_queue(
    issue: StreamIssue,
    *,
    query: WorkItemQuery,
    beads: ProviderSnapshot,
    by_id: dict[str, StreamIssue],
) -> bool:
    blockers = _direct_blockers(issue, beads, by_id)
    gates = _blocking_gates(issue, beads)
    readiness = _readiness(issue, blockers, gates)[0]
    if readiness != query.queue and not (query.queue == "recent" and readiness == "completed"):
        return False
    if query.priorities and issue.priority.upper() not in query.priorities:
        return False
    if query.labels and not set(query.labels).issubset(issue.labels):
        return False
    if query.assignee is not None and issue.assignee != query.assignee:
        return False
    if query.issue_type is not None and issue.issue_type != query.issue_type:
        return False
    return query.parent is None or issue.parent_id == query.parent


def _sort_key(issue: StreamIssue, queue: str) -> tuple[object, ...]:
    if queue in {"active", "recent"}:
        timestamp = issue.closed_at or issue.updated_at
        return (-(_millis(timestamp) or 0), _priority(issue.priority), issue.id)
    return (_priority(issue.priority), -(_millis(issue.updated_at) or 0), issue.id)


def configured_ready_policy(
    *, cfg: dict, entry: dict[str, object]
) -> tuple[tuple[str, int] | None, str]:
    """Return the CLI's configured release-aware policy and a cursor scope token."""

    strategy = str(config.release_value(cfg, entry, "strategy", "") or "")
    if not strategy:
        return None, "beadhive.work-items/v1"
    budget = config.release_fix_churn_budget(cfg, entry)
    return (
        (strategy, budget),
        f"beadhive.work-items/v1;release={strategy};fix-churn-budget={budget}",
    )


def _coverage(beads: ProviderSnapshot, runtime: AgentRunSnapshot) -> dict[str, object]:
    warnings = []
    if beads.partial_reason:
        warnings.append(beads.partial_reason)
    if runtime.coverage_reason:
        warnings.append(runtime.coverage_reason)
    state = "partial" if warnings else "complete"
    return {
        "state": state,
        "sources": {
            "beads": {
                "state": "partial" if beads.partial else "complete",
                "detail": beads.partial_reason,
            },
            "runtime": {
                "state": runtime.coverage.value,
                "detail": runtime.coverage_reason,
            },
        },
    }


def queue_payload(
    *,
    hive_id: str,
    beads: ProviderSnapshot,
    runtime: AgentRunSnapshot,
    query: WorkItemQuery,
    ready_policy: tuple[str, int] | None = None,
) -> dict[str, object]:
    revision = projection_revision(beads, runtime)
    offset = _cursor_offset(query.cursor, hive_id=hive_id, revision=revision, query=query)
    by_id = _issue_index(beads)
    selected = sorted(
        (
            issue
            for issue in beads.issues
            if _matches_queue(issue, query=query, beads=beads, by_id=by_id)
        ),
        key=lambda issue: _sort_key(issue, query.queue),
    )
    if query.queue == "ready" and ready_policy is not None:
        strategy, budget = ready_policy
        ready_order = release_order.merge_sequence(
            [{"id": issue.id, "labels": list(issue.labels)} for issue in selected],
            strategy=strategy,
            fix_churn_budget=budget,
        )
        positions = {item_id: position for position, item_id in enumerate(ready_order)}
        selected.sort(key=lambda issue: (positions.get(issue.id, len(positions)), issue.id))
    page = selected[offset : offset + query.limit]
    next_offset = offset + len(page)
    truncated = next_offset < len(selected)
    warnings = [value for value in (beads.partial_reason, runtime.coverage_reason) if value]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "hiveId": hive_id,
        "queue": query.queue,
        "revision": revision,
        "generatedAt": _millis(beads.as_of),
        "freshness": {"state": "fresh", "asOf": _millis(beads.as_of)},
        "coverage": _coverage(beads, runtime),
        "limit": query.limit,
        "returned": len(page),
        "truncated": truncated,
        "nextCursor": (
            _encode_cursor(hive_id=hive_id, revision=revision, query=query, offset=next_offset)
            if truncated
            else None
        ),
        "items": [
            _row(
                issue,
                hive_id=hive_id,
                revision=revision,
                beads=beads,
                runtime=runtime,
                by_id=by_id,
            )
            for issue in page
        ],
        "warnings": warnings,
    }


def complete_queue_payload(
    *,
    hive_id: str,
    beads: ProviderSnapshot,
    runtime: AgentRunSnapshot,
    query: WorkItemQuery,
    ready_policy: tuple[str, int] | None = None,
) -> dict[str, object]:
    """Project one complete queue once for an in-process composite view.

    Public HTTP callers remain capped by :func:`queue_payload` and the API's 200-row
    validation.  A composite projection already owns the full immutable provider snapshot,
    so paging that same snapshot internally would repeat its full selection and sort for every
    page.  Selecting at most the snapshot's issue count keeps that composition to one pass.
    """

    return queue_payload(
        hive_id=hive_id,
        beads=beads,
        runtime=runtime,
        query=replace(query, limit=max(1, len(beads.issues)), cursor=None),
        ready_policy=ready_policy,
    )


def _dependency_detail(
    dependency: WorkDependency, *, direction: str, by_id: dict[str, StreamIssue]
) -> dict[str, object]:
    other_id = dependency.depends_on_id if direction == "prerequisite" else dependency.issue_id
    other = by_id.get(other_id)
    return {
        "id": other_id,
        "title": other.title if other else None,
        "type": dependency.type,
        "state": other.status if other else "unknown",
        "direction": direction,
    }


def detail_payload(
    *,
    hive_id: str,
    bead_id: str,
    beads: ProviderSnapshot,
    runtime: AgentRunSnapshot,
) -> dict[str, object]:
    by_id = _issue_index(beads)
    issue = by_id.get(bead_id)
    if issue is None:
        raise OperatorSourceError(
            "work_item_not_found", "The exact work item was not found.", status_code=404
        )
    revision = projection_revision(beads, runtime)
    row = _row(
        issue,
        hive_id=hive_id,
        revision=revision,
        beads=beads,
        runtime=runtime,
        by_id=by_id,
    )
    dependencies = [
        _dependency_detail(dependency, direction="prerequisite", by_id=by_id)
        for dependency in sorted(
            (item for item in beads.work_dependencies if item.issue_id == issue.id),
            key=lambda item: (item.depends_on_id, item.type),
        )
    ]
    dependents = [
        _dependency_detail(dependency, direction="dependent", by_id=by_id)
        for dependency in sorted(
            (item for item in beads.work_dependencies if item.depends_on_id == issue.id),
            key=lambda item: (item.issue_id, item.type),
        )
    ]
    gates = [
        {
            "id": gate.gate_id,
            "kind": gate.gate_kind,
            "type": gate.gate_type,
            "status": gate.status,
            "reason": gate.reason,
            "openedAt": _millis(gate.opened_at),
            "resolvedAt": _millis(gate.resolved_at),
        }
        for gate in sorted(
            (gate for gate in beads.gate_requests if issue.id in gate.blocks),
            key=lambda gate: gate.id,
        )
    ]
    warnings = [value for value in (beads.partial_reason, runtime.coverage_reason) if value]
    advertised_at = _millis(beads.as_of) or 0
    return {
        "schemaVersion": SCHEMA_VERSION,
        "hiveId": hive_id,
        "revision": revision,
        "generatedAt": _millis(beads.as_of),
        "freshness": {"state": "fresh", "asOf": _millis(beads.as_of)},
        "coverage": _coverage(beads, runtime),
        "item": {
            **row,
            "description": issue.description,
            "design": issue.design,
            "acceptanceCriteria": issue.acceptance_criteria,
            "notes": issue.notes,
            "moleculeType": issue.mol_type,
            "labels": list(issue.labels),
            "remainingLabelCount": 0,
            "createdBy": issue.created_by,
            "createdAt": _millis(issue.created_at),
            "closedAt": _millis(issue.closed_at),
            "dueAt": _millis(issue.due_at),
            "deferUntil": _millis(issue.defer_until),
            "claim": {
                "actor": issue.assignee,
                "leaseExpiresAt": _millis(issue.lease_expires_at),
            },
            "dependencies": dependencies,
            "dependents": dependents,
            "gates": gates,
            "agents": _agents(issue, runtime),
            "advertisedActions": operator_actions.work_item_actions(
                target=row["ref"],
                readiness=str(row["readiness"]["state"]),
                readiness_reason=str(row["readiness"]["reason"]),
                partial=beads.partial,
                revision=revision,
                advertised_at=advertised_at,
            ),
        },
        "warnings": warnings,
    }
