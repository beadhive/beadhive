"""Bounded, backend-neutral work-item queue and exact-detail projections."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import config, daemon_contract, operator_actions, operator_contract, release_order
from .modules.state import (
    AgentRunSnapshot,
    AgentRunState,
    ProviderSnapshot,
    StreamIssue,
    WorkDependency,
)
from .operator_sources import OperatorSourceError

SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_PRIORITIES = 5
MAX_LABEL_FILTERS = 8
MAX_LABEL_FILTER_BYTES = 64
MAX_SCALAR_FILTER_BYTES = 256
MAX_CURSOR_BYTES = 4_096
QUEUE_MAX_BYTES = 896 * 1024
DETAIL_MAX_BYTES = 896 * 1024
DETAIL_TEXT_MAX_BYTES = 128 * 1024
DETAIL_SHORT_TEXT_MAX_BYTES = 4_096
DETAIL_LABEL_MAX_BYTES = 256
DETAIL_MAX_LABELS = 256
DETAIL_MAX_DEPENDENCIES = 1_024
DETAIL_MAX_GATES = 256
DETAIL_MAX_AGENTS = 256
DETAIL_MAX_ACTIONS = 64
REMOTE_WARNING_MAX_BYTES = 4_096
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


@dataclass(frozen=True)
class _QueueIndexes:
    issues: dict[str, StreamIssue]
    blocker_counts: dict[str, int]
    blocked_dependent_counts: dict[str, int]
    open_gate_counts: dict[str, int]


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
    bead_revision = beads.content_revision or beads.revision
    value = json.dumps(
        [bead_revision, runtime.revision], separators=(",", ":"), ensure_ascii=True
    ).encode()
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def etag(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f'"sha256:{hashlib.sha256(canonical).hexdigest()}"'


def encoded_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _bounded_text(value: str | None, maximum: int, *, field: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise OperatorSourceError(
            "work_item_detail_too_large",
            "The exact work-item detail exceeds its disclosure limit.",
            status_code=413,
        )
    return value


def _bounded_optional_text(value: str | None, maximum: int, *, field: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, maximum, field=field)


def _bounded_queue_text(value: str, maximum: int) -> str:
    if len(value.encode("utf-8")) > maximum:
        raise OperatorSourceError(
            "work_items_page_too_large",
            "The work-items page exceeds its disclosure limit.",
            status_code=413,
        )
    return value


def _bounded_collection(values: Iterable[object], maximum: int) -> list[object]:
    bounded: list[object] = []
    for value in values:
        if len(bounded) == maximum:
            raise OperatorSourceError(
                "work_item_detail_too_large",
                "The exact work-item detail exceeds its disclosure limit.",
                status_code=413,
            )
        bounded.append(value)
    return bounded


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


def _queue_indexes(beads: ProviderSnapshot) -> _QueueIndexes:
    issues = {item.id: item for item in beads.issues}
    blocker_counts: dict[str, int] = {}
    blocked_dependent_counts: dict[str, int] = {}
    open_gate_counts: dict[str, int] = {}
    for dependency in beads.work_dependencies:
        if dependency.type in _NON_BLOCKING_DEPENDENCIES:
            continue
        prerequisite = issues.get(dependency.depends_on_id)
        if prerequisite is None or prerequisite.status != "closed":
            blocker_counts[dependency.issue_id] = blocker_counts.get(dependency.issue_id, 0) + 1
        dependent = issues.get(dependency.issue_id)
        if dependent is not None and dependent.status != "closed":
            blocked_dependent_counts[dependency.depends_on_id] = (
                blocked_dependent_counts.get(dependency.depends_on_id, 0) + 1
            )
    for gate in beads.gate_requests:
        if gate.status not in {"open", "pending"}:
            continue
        for issue_id in dict.fromkeys(gate.blocks):
            open_gate_counts[issue_id] = open_gate_counts.get(issue_id, 0) + 1
    return _QueueIndexes(
        issues=issues,
        blocker_counts=blocker_counts,
        blocked_dependent_counts=blocked_dependent_counts,
        open_gate_counts=open_gate_counts,
    )


def _readiness(issue: StreamIssue, blocker_count: int, open_gate_count: int) -> tuple[str, str]:
    if issue.status == "closed":
        return "completed", "work item is closed"
    if issue.status == "blocked" or blocker_count or open_gate_count:
        reasons = []
        if blocker_count:
            reasons.append(f"{blocker_count} unresolved direct dependency")
        if open_gate_count:
            reasons.append(f"{open_gate_count} open gate")
        return "blocked", " and ".join(reasons) or "work item status is blocked"
    if issue.status == "open":
        return "ready", "open with no unresolved direct dependency or open gate"
    if issue.status == "in_progress":
        return "active", "work item is in progress"
    return "unavailable", f"work item status is {issue.status or 'unknown'}"


def _legacy_agent_details(
    issue: StreamIssue, runtime: AgentRunSnapshot
) -> Iterable[dict[str, object]]:
    for summary in runtime.summaries:
        if summary.bead != issue.id:
            continue
        yield {
            "id": summary.session_id or f"waiting:{issue.id}",
            "state": summary.state.value,
            "ownerSeat": summary.owner_seat,
            "startedAt": _millis(summary.started_at),
            "updatedAt": _millis(summary.updated_at),
            "endedAt": _millis(summary.ended_at),
        }


def _remote_agent_details(
    issue: StreamIssue, runtime: AgentRunSnapshot
) -> Iterable[dict[str, object]]:
    for summary in runtime.summaries:
        if summary.bead != issue.id:
            continue
        yield {
            "id": _bounded_text(
                summary.session_id or f"waiting:{issue.id}",
                DETAIL_SHORT_TEXT_MAX_BYTES,
                field="agent id",
            ),
            "state": _bounded_text(summary.state.value, 128, field="agent state"),
            "ownerSeat": _bounded_optional_text(
                summary.owner_seat, DETAIL_SHORT_TEXT_MAX_BYTES, field="agent owner seat"
            ),
            "startedAt": _millis(summary.started_at),
            "updatedAt": _millis(summary.updated_at),
            "endedAt": _millis(summary.ended_at),
        }


def _row(
    issue: StreamIssue,
    *,
    generated_at: int,
    runtime: AgentRunSnapshot,
    indexes: _QueueIndexes,
) -> dict[str, object]:
    blocker_count = indexes.blocker_counts.get(issue.id, 0)
    open_gate_count = indexes.open_gate_counts.get(issue.id, 0)
    live_agent_count = sum(
        summary.bead == issue.id and summary.state in _LIVE_AGENT_STATES
        for summary in runtime.summaries
    )
    return operator_contract.work_item_summary(
        issue,
        generated_at=generated_at,
        blocker_count=blocker_count,
        open_gate_count=open_gate_count,
        live_agent_count=live_agent_count,
    )


def _legacy_row(
    issue: StreamIssue,
    *,
    hive_id: str,
    revision: str,
    runtime: AgentRunSnapshot,
    indexes: _QueueIndexes,
) -> dict[str, object]:
    """Keep the in-process Herdr composite shape separate from the remote DTO."""

    blocker_count = indexes.blocker_counts.get(issue.id, 0)
    open_gate_count = indexes.open_gate_counts.get(issue.id, 0)
    readiness, reason = _readiness(issue, blocker_count, open_gate_count)
    labels = list(issue.labels)
    agents = list(_legacy_agent_details(issue, runtime))
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
        "blockerCount": blocker_count,
        "blockedDependentCount": indexes.blocked_dependent_counts.get(issue.id, 0),
        "labels": labels[:12],
        "remainingLabelCount": max(0, len(labels) - 12),
        "openGateCount": open_gate_count,
        "liveAgentCount": sum(
            item["state"] in {state.value for state in _LIVE_AGENT_STATES} for item in agents
        ),
        "updatedAt": _millis(issue.updated_at),
    }


def _matches_queue(
    issue: StreamIssue,
    *,
    query: WorkItemQuery,
    indexes: _QueueIndexes,
) -> bool:
    readiness = _readiness(
        issue,
        indexes.blocker_counts.get(issue.id, 0),
        indexes.open_gate_counts.get(issue.id, 0),
    )[0]
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
    """Retain the published loopback queue shape for existing consumers."""

    revision = projection_revision(beads, runtime)
    offset = _cursor_offset(query.cursor, hive_id=hive_id, revision=revision, query=query)
    indexes = _queue_indexes(beads)
    selected = sorted(
        (issue for issue in beads.issues if _matches_queue(issue, query=query, indexes=indexes)),
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
    generated_at = _millis(beads.as_of)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "hiveId": hive_id,
        "queue": query.queue,
        "revision": revision,
        "generatedAt": generated_at,
        "freshness": {"state": "fresh", "asOf": generated_at},
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
            _legacy_row(
                issue,
                hive_id=hive_id,
                revision=revision,
                runtime=runtime,
                indexes=indexes,
            )
            for issue in page
        ],
        "warnings": [value for value in (beads.partial_reason, runtime.coverage_reason) if value],
    }


def remote_queue_payload(
    *,
    hive_id: str,
    beads: ProviderSnapshot,
    runtime: AgentRunSnapshot,
    query: WorkItemQuery,
    ready_policy: tuple[str, int] | None = None,
) -> dict[str, object]:
    revision = projection_revision(beads, runtime)
    offset = _cursor_offset(query.cursor, hive_id=hive_id, revision=revision, query=query)
    generated_at = _millis(beads.as_of)
    if generated_at is None:
        raise OperatorSourceError(
            "work_items_source_unavailable",
            "The authoritative work-items source is unavailable.",
            status_code=503,
            retryable=True,
        )
    indexes = _queue_indexes(beads)
    selected = sorted(
        (issue for issue in beads.issues if _matches_queue(issue, query=query, indexes=indexes)),
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
    rows = [
        _row(
            issue,
            generated_at=generated_at,
            runtime=runtime,
            indexes=indexes,
        )
        for issue in page
    ]
    warnings = [
        _bounded_queue_text(value, REMOTE_WARNING_MAX_BYTES)
        for value in (beads.partial_reason, runtime.coverage_reason)
        if value
    ]

    def build(current: list[dict[str, object]]) -> dict[str, object]:
        next_offset = offset + len(current)
        truncated = next_offset < len(selected)
        next_cursor = (
            _encode_cursor(hive_id=hive_id, revision=revision, query=query, offset=next_offset)
            if truncated
            else None
        )
        source_coverage = _coverage(beads, runtime)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "projectionPolicy": operator_contract.DEVELOPMENT_SNAPSHOT_POLICY,
            "hiveId": hive_id,
            "queue": query.queue,
            "revision": revision,
            "generatedAt": generated_at,
            "limits": {"maxBytes": QUEUE_MAX_BYTES, "maxItems": MAX_LIMIT},
            "filters": {
                "priorities": list(query.priorities),
                "labels": list(query.labels),
                "assignee": query.assignee,
                "type": query.issue_type,
                "parent": query.parent,
                "ordering": query.ordering,
            },
            "coverage": {
                **source_coverage,
                "eligible": len(selected),
                "returned": len(current),
                "truncated": truncated,
                "nextCursor": next_cursor,
            },
            "limit": query.limit,
            "returned": len(current),
            "truncated": truncated,
            "nextCursor": next_cursor,
            "items": current,
            "warnings": warnings,
        }

    low = 0
    high = len(rows)
    payload = build(rows)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = build(rows[:middle])
        if len(encoded_bytes(candidate)) <= QUEUE_MAX_BYTES:
            low = middle
            payload = candidate
        else:
            high = middle - 1
    if low != len(rows):
        payload = build(rows[:low])
    if low == 0 and offset < len(selected):
        raise OperatorSourceError(
            "work_items_page_too_large",
            "The work-items page exceeds its disclosure limit.",
            status_code=413,
        )
    daemon_contract.RemoteWorkItemQueue.model_validate(payload)
    return payload


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

    revision = projection_revision(beads, runtime)
    indexes = _queue_indexes(beads)
    selected = sorted(
        (issue for issue in beads.issues if _matches_queue(issue, query=query, indexes=indexes)),
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
    generated_at = _millis(beads.as_of)
    warnings = [value for value in (beads.partial_reason, runtime.coverage_reason) if value]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "hiveId": hive_id,
        "queue": query.queue,
        "revision": revision,
        "generatedAt": generated_at,
        "freshness": {"state": "fresh", "asOf": generated_at},
        "coverage": _coverage(beads, runtime),
        "limit": len(selected),
        "returned": len(selected),
        "truncated": False,
        "nextCursor": None,
        "items": [
            _legacy_row(
                issue,
                hive_id=hive_id,
                revision=revision,
                runtime=runtime,
                indexes=indexes,
            )
            for issue in selected
        ],
        "warnings": warnings,
    }


def _legacy_dependency_detail(
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


def _remote_dependency_detail(
    dependency: WorkDependency, *, direction: str, by_id: dict[str, StreamIssue]
) -> dict[str, object]:
    other_id = dependency.depends_on_id if direction == "prerequisite" else dependency.issue_id
    other = by_id.get(other_id)
    return {
        "id": _bounded_text(other_id, DETAIL_SHORT_TEXT_MAX_BYTES, field="dependency id"),
        "title": _bounded_optional_text(
            other.title if other else None, DETAIL_SHORT_TEXT_MAX_BYTES, field="dependency title"
        ),
        "type": _bounded_text(
            dependency.type, DETAIL_SHORT_TEXT_MAX_BYTES, field="dependency type"
        ),
        "state": _bounded_text(
            other.status if other else "unknown",
            DETAIL_SHORT_TEXT_MAX_BYTES,
            field="dependency state",
        ),
        "direction": direction,
    }


def detail_payload(
    *,
    hive_id: str,
    bead_id: str,
    beads: ProviderSnapshot,
    runtime: AgentRunSnapshot,
) -> dict[str, object]:
    """Retain the published loopback exact-detail shape for existing consumers."""

    indexes = _queue_indexes(beads)
    by_id = indexes.issues
    issue = by_id.get(bead_id)
    if issue is None:
        raise OperatorSourceError(
            "work_item_not_found", "The exact work item was not found.", status_code=404
        )
    revision = projection_revision(beads, runtime)
    row = _legacy_row(
        issue,
        hive_id=hive_id,
        revision=revision,
        runtime=runtime,
        indexes=indexes,
    )
    dependencies = [
        _legacy_dependency_detail(dependency, direction="prerequisite", by_id=by_id)
        for dependency in sorted(
            (item for item in beads.work_dependencies if item.issue_id == issue.id),
            key=lambda item: (item.depends_on_id, item.type),
        )
    ]
    dependents = [
        _legacy_dependency_detail(dependency, direction="dependent", by_id=by_id)
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
    generated_at = _millis(beads.as_of)
    readiness = row["readiness"]
    assert isinstance(readiness, dict)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "hiveId": hive_id,
        "revision": revision,
        "generatedAt": generated_at,
        "freshness": {"state": "fresh", "asOf": generated_at},
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
            "agents": list(_legacy_agent_details(issue, runtime)),
            "advertisedActions": operator_actions.work_item_actions(
                target=row["ref"],
                readiness=str(readiness["state"]),
                readiness_reason=str(readiness["reason"]),
                partial=beads.partial,
                revision=revision,
                advertised_at=generated_at or 0,
            ),
        },
        "warnings": [value for value in (beads.partial_reason, runtime.coverage_reason) if value],
    }


def remote_detail_payload(
    *,
    hive_id: str,
    bead_id: str,
    beads: ProviderSnapshot,
    runtime: AgentRunSnapshot,
) -> dict[str, object]:
    indexes = _queue_indexes(beads)
    by_id = indexes.issues
    issue = by_id.get(bead_id)
    if issue is None:
        raise OperatorSourceError(
            "work_item_not_found", "The exact work item was not found.", status_code=404
        )
    revision = projection_revision(beads, runtime)
    generated_at = _millis(beads.as_of)
    if generated_at is None:
        raise OperatorSourceError(
            "work_item_source_unavailable",
            "The authoritative exact work-item source is unavailable.",
            status_code=503,
            retryable=True,
        )
    summary = _row(
        issue,
        generated_at=generated_at,
        runtime=runtime,
        indexes=indexes,
    )
    dependencies = sorted(
        _bounded_collection(
            (
                _remote_dependency_detail(dependency, direction="prerequisite", by_id=by_id)
                for dependency in beads.work_dependencies
                if dependency.issue_id == issue.id
            ),
            DETAIL_MAX_DEPENDENCIES,
        ),
        key=lambda item: (item["id"], item["type"]),
    )
    dependents = sorted(
        _bounded_collection(
            (
                _remote_dependency_detail(dependency, direction="dependent", by_id=by_id)
                for dependency in beads.work_dependencies
                if dependency.depends_on_id == issue.id
            ),
            DETAIL_MAX_DEPENDENCIES,
        ),
        key=lambda item: (item["id"], item["type"]),
    )
    gates = sorted(
        _bounded_collection(
            (
                {
                    "id": _bounded_text(gate.gate_id, DETAIL_SHORT_TEXT_MAX_BYTES, field="gate id"),
                    "kind": _bounded_text(
                        gate.gate_kind, DETAIL_SHORT_TEXT_MAX_BYTES, field="gate kind"
                    ),
                    "type": _bounded_optional_text(
                        gate.gate_type, DETAIL_SHORT_TEXT_MAX_BYTES, field="gate type"
                    ),
                    "status": _bounded_text(
                        gate.status, DETAIL_SHORT_TEXT_MAX_BYTES, field="gate status"
                    ),
                    "reason": _bounded_text(
                        gate.reason, DETAIL_TEXT_MAX_BYTES, field="gate reason"
                    ),
                    "openedAt": _millis(gate.opened_at),
                    "resolvedAt": _millis(gate.resolved_at),
                }
                for gate in beads.gate_requests
                if issue.id in gate.blocks
            ),
            DETAIL_MAX_GATES,
        ),
        key=lambda item: item["id"],
    )
    agents = _bounded_collection(_remote_agent_details(issue, runtime), DETAIL_MAX_AGENTS)
    labels = _bounded_collection(
        (_bounded_text(label, DETAIL_LABEL_MAX_BYTES, field="label") for label in issue.labels),
        DETAIL_MAX_LABELS,
    )
    warnings = [
        _bounded_text(value, REMOTE_WARNING_MAX_BYTES, field="warning")
        for value in (beads.partial_reason, runtime.coverage_reason)
        if value
    ]
    advertised_at = _millis(beads.as_of) or 0
    target = {"hiveId": hive_id, "kind": "work-item", "id": issue.id}
    readiness, readiness_reason = _readiness(
        issue,
        indexes.blocker_counts.get(issue.id, 0),
        indexes.open_gate_counts.get(issue.id, 0),
    )
    actions = _bounded_collection(
        operator_actions.work_item_actions(
            target=target,
            readiness=readiness,
            readiness_reason=readiness_reason,
            partial=beads.partial,
            revision=revision,
            advertised_at=advertised_at,
        ),
        DETAIL_MAX_ACTIONS,
    )
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "projectionPolicy": operator_contract.DEVELOPMENT_SNAPSHOT_POLICY,
        "hiveId": hive_id,
        "revision": revision,
        "generatedAt": generated_at,
        "limits": {"maxBytes": DETAIL_MAX_BYTES},
        "freshness": {"state": "fresh", "asOf": generated_at},
        "coverage": _coverage(beads, runtime),
        "item": {
            **summary,
            "ref": target,
            "revision": revision,
            "readinessReason": _bounded_text(
                readiness_reason, DETAIL_SHORT_TEXT_MAX_BYTES, field="readiness reason"
            ),
            "parentId": _bounded_optional_text(
                issue.parent_id, DETAIL_SHORT_TEXT_MAX_BYTES, field="parent id"
            ),
            "blockedDependentCount": indexes.blocked_dependent_counts.get(issue.id, 0),
            "description": _bounded_text(
                issue.description, DETAIL_TEXT_MAX_BYTES, field="description"
            ),
            "design": _bounded_text(issue.design, DETAIL_TEXT_MAX_BYTES, field="design"),
            "acceptanceCriteria": _bounded_text(
                issue.acceptance_criteria, DETAIL_TEXT_MAX_BYTES, field="acceptance criteria"
            ),
            "notes": _bounded_text(issue.notes, DETAIL_TEXT_MAX_BYTES, field="notes"),
            "moleculeType": _bounded_optional_text(
                issue.mol_type, DETAIL_SHORT_TEXT_MAX_BYTES, field="molecule type"
            ),
            "labels": labels,
            "remainingLabelCount": 0,
            "createdBy": _bounded_optional_text(
                issue.created_by, DETAIL_SHORT_TEXT_MAX_BYTES, field="created by"
            ),
            "createdAt": _millis(issue.created_at),
            "closedAt": _millis(issue.closed_at),
            "dueAt": _millis(issue.due_at),
            "deferUntil": _millis(issue.defer_until),
            "claim": {
                "actor": _bounded_optional_text(
                    issue.assignee, DETAIL_SHORT_TEXT_MAX_BYTES, field="claim actor"
                ),
                "leaseExpiresAt": _millis(issue.lease_expires_at),
            },
            "dependencies": dependencies,
            "dependents": dependents,
            "gates": gates,
            "agents": agents,
            "advertisedActions": actions,
        },
        "warnings": warnings,
    }
    if len(encoded_bytes(payload)) > DETAIL_MAX_BYTES:
        raise OperatorSourceError(
            "work_item_detail_too_large",
            "The exact work-item detail exceeds its disclosure limit.",
            status_code=413,
        )
    daemon_contract.RemoteWorkItemDetail.model_validate(payload)
    return payload
