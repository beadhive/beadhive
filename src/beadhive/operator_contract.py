"""Phase-one operator HTTP wire projections.

The browser contracts are intentionally represented as JSON-ready dictionaries here.  They
are a boundary owned by the unified host, not a second persistence model: beads, dispatch
summaries, and run journals retain their distinct authority and revision domains.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import operator_actions
from .modules.state import (
    AgentRunSnapshot,
    AgentRunSummary,
    Assignment,
    Coverage,
    EpicSchedule,
    Freshness,
    GateRequest,
    ProviderSnapshot,
    RunJournalFrame,
    StreamIssue,
    WorkDependency,
)

SCHEMA_VERSION = 1
DEVELOPMENT_SNAPSHOT_MAX_BYTES = 896 * 1024
DEVELOPMENT_WORK_ITEM_LIMIT = 4_096
DEVELOPMENT_MAX_JSON_SAFE_INTEGER = 2**53 - 1
DEVELOPMENT_MAX_CURSOR_SEQUENCE = DEVELOPMENT_MAX_JSON_SAFE_INTEGER
DEVELOPMENT_SNAPSHOT_POLICY = "beadhive.snapshot-summary/v1"
DEVELOPMENT_MAX_LABELS = 12
DEVELOPMENT_MAX_LABEL_LENGTH = 256
DEVELOPMENT_MAX_ID_LENGTH = 256
DEVELOPMENT_MAX_TITLE_LENGTH = 4_096
_DEVELOPMENT_WORK_STATUSES = frozenset({"open", "in_progress", "blocked"})
_INTERNAL_WORK_ITEM_TYPES = frozenset({"event", "gate"})
_NON_BLOCKING_DEPENDENCIES = frozenset({"parent-child", "related", "discovered-from"})
_LIVE_AGENT_STATES = frozenset({"starting", "active", "waiting"})
_MISSING_WORK_ITEM_DETAIL = (
    "The state stream does not expose description, molecule type, or lifecycle timestamps."
)


class SnapshotProjectionUnavailable(RuntimeError):
    """The Development snapshot cannot satisfy its fail-closed disclosure bounds."""


class _WorstFirstIssue:
    """Heap entry whose root is the worst retained deterministic summary candidate."""

    __slots__ = ("issue", "key")

    def __init__(self, issue: StreamIssue, key: tuple[object, ...]) -> None:
        self.issue = issue
        self.key = key

    def __lt__(self, other: _WorstFirstIssue) -> bool:
        return self.key > other.key


def _millis(value: str | float | int | None, *, fallback: int = 0) -> int:
    if isinstance(value, int | float):
        return int(value * 1000)
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return fallback


def _revision(*parts: object) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def hive_subscription_id(hive_id: str) -> str:
    """Return the stable, path-independent stream identity for one canonical hive."""

    return f"hive-sha256-{hashlib.sha256(hive_id.encode('utf-8')).hexdigest()}"


def _ref(hive_id: str, kind: str, entity_id: str) -> dict[str, object]:
    return {"hiveId": hive_id, "kind": kind, "id": entity_id}


def _scoped(hive_id: str, entity_id: str) -> dict[str, str]:
    return {"hiveId": hive_id, "id": entity_id}


def _freshness(value: Freshness, generated_at: int) -> dict[str, object]:
    return {
        "state": value.state if value.state in {"fresh", "stale", "unknown"} else "unknown",
        "asOf": _millis(value.as_of, fallback=generated_at),
        "expiresAt": _millis(value.expires_at) if value.expires_at is not None else None,
        "detail": value.detail,
    }


def hive_info(entry: Mapping[str, object], *, canonical_prefix: bool) -> dict[str, str]:
    canonical = "/".join(str(entry[field]) for field in ("provider", "org", "repo"))
    return {
        "prefix": canonical if canonical_prefix else str(entry["prefix"]),
        "provider": str(entry["provider"]),
        "org": str(entry["org"]),
        "repo": str(entry["repo"]),
        "kind": str(entry.get("kind", "")),
    }


def factory_snapshot(
    entries: Sequence[Mapping[str, object]],
    *,
    generated_at: int,
    host_id: str,
    instance_id: str,
    ready: bool,
) -> dict[str, object]:
    """Return the flat FactorySnapshot-v1-compatible phase-one response.

    Only registry hives are authoritative in this slice.  Absolute workspace and worktree
    paths are deliberately not exposed by the unauthenticated loopback profile.
    """

    unavailable = {
        "state": "unavailable",
        "requested": None,
        "returned": None,
        "fromCache": 0,
        "detail": "This source is not exposed by the phase-one local read profile.",
    }
    complete_hives = {
        "state": "complete",
        "requested": len(entries),
        "returned": len(entries),
        "fromCache": 0,
        "detail": None,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "hives": [hive_info(entry, canonical_prefix=False) for entry in entries],
        "worktrees": [],
        "edges": [],
        "workspaceRoot": None,
        "generatedAt": generated_at,
        "coverage": {
            "hives": complete_hives,
            "worktrees": dict(unavailable),
            "hubIssues": dict(unavailable),
            "hubDeps": dict(unavailable),
        },
        "hostId": host_id,
        "serviceInstanceId": instance_id,
        "ready": ready,
    }


def factory_hive_summary(
    entry: Mapping[str, object],
    snapshot: ProviderSnapshot | None,
    *,
    unavailable_reason: str | None = None,
    advertised_at: int | None = None,
) -> dict[str, object]:
    """Project one reusable, path-safe factory hive summary.

    ``open`` is the literal number of issues whose canonical status is ``open``.
    ``ready`` is the subset of those issues with no known non-terminal prerequisite or open
    gate. ``active`` is the number whose status is ``in_progress``. ``blocked`` combines the
    canonical ``blocked`` status with open issues that have a known unresolved prerequisite.
    The categories intentionally overlap: ready and dependency-blocked issues are both open.

    Unavailable hives retain their registry identity and carry null counts.  That makes an
    unavailable source observably different from an available hive with four zero counts.
    """

    identity = "/".join(str(entry[field]) for field in ("provider", "org", "repo"))
    opaque_ref = hive_subscription_id(identity)
    base: dict[str, object] = {
        "id": identity,
        "displayLabel": str(entry.get("label") or entry.get("display_name") or entry["repo"]),
        "opaqueRef": opaque_ref,
        "prefix": str(entry["prefix"]),
        "provider": str(entry["provider"]),
        "org": str(entry["org"]),
        "repo": str(entry["repo"]),
        "kind": str(entry.get("kind", "")),
    }
    observed_at = (
        advertised_at
        if advertised_at is not None
        else (_millis(snapshot.as_of) if snapshot is not None else 0)
    )
    revision = snapshot.revision if snapshot is not None else None
    actions = operator_actions.hive_actions(
        hive_id=identity, revision=revision, advertised_at=observed_at
    )
    if snapshot is None:
        return {
            **base,
            "availability": {"state": "unavailable", "reason": unavailable_reason},
            "counts": {"open": None, "ready": None, "active": None, "blocked": None},
            "revision": None,
            "asOf": None,
            "coverage": {"state": "unavailable", "reason": unavailable_reason},
            "advertisedActions": actions,
        }

    issues = tuple(snapshot.issues)
    terminal = {"closed"}
    nonterminal_ids = {issue.id for issue in issues if issue.status.lower() not in terminal}
    open_gate_ids = {
        gate.gate_id
        for gate in snapshot.gate_requests
        if gate.status.lower() in {"open", "pending"}
    }
    canonical_blocked_ids = {issue.id for issue in issues if issue.status.lower() == "blocked"}
    blocked_ids = set(canonical_blocked_ids)
    for issue in issues:
        for edge in issue.dependencies:
            if edge.depends_on_id in nonterminal_ids or edge.depends_on_id in open_gate_ids:
                blocked_ids.add(issue.id)
    for edge in snapshot.work_dependencies:
        if edge.depends_on_id in nonterminal_ids or edge.depends_on_id in open_gate_ids:
            blocked_ids.add(edge.issue_id)

    open_ids = {issue.id for issue in issues if issue.status.lower() == "open"}
    counts = {
        "open": len(open_ids),
        "ready": len(open_ids - blocked_ids),
        "active": sum(issue.status.lower() == "in_progress" for issue in issues),
        "blocked": len(blocked_ids & (open_ids | canonical_blocked_ids)),
    }
    return {
        **base,
        "availability": {"state": "available", "reason": None},
        "counts": counts,
        "revision": snapshot.revision,
        "asOf": _millis(snapshot.as_of),
        "coverage": {
            "state": "partial" if snapshot.partial else "complete",
            "reason": snapshot.partial_reason,
        },
        "advertisedActions": actions,
    }


FACTORY_HIVE_FRESHNESS_STATES = ("fresh", "stale", "refreshing", "unknown")
FACTORY_HIVE_PENDING_REASON = "summary_pending"


def factory_hive_pending_summary(entry: Mapping[str, object]) -> dict[str, object]:
    """Project a registered hive whose summary has not been observed yet.

    A cold directory entry is not evidence that the hive is unavailable, so it keeps the
    ``available`` availability state with null counts and a ``partial`` coverage whose reason
    names the pending summary.  The additive ``freshness`` member carries the precise state.
    """

    summary = factory_hive_summary(entry, None)
    return {
        **summary,
        "availability": {"state": "available", "reason": FACTORY_HIVE_PENDING_REASON},
        "coverage": {"state": "partial", "reason": FACTORY_HIVE_PENDING_REASON},
    }


def factory_hive_page_revision(items: Sequence[Mapping[str, object]]) -> str:
    """Return the opaque content revision of one directory page's summaries."""

    return _revision("factory-hives-v1", list(items))


def factory_hive_cursor_revision(hive_ids: Sequence[str], availability: str | None) -> str:
    """Return the membership/order revision that scopes directory cursors.

    Summaries refresh in the background, so cursors bind only to registry membership, its
    order, and the filter scope.  A summary refresh never invalidates an open cursor; a
    registry membership change does.
    """

    return _revision("factory-hives-cursor-v1", list(hive_ids), availability)


def _work_item(issue: StreamIssue, hive_id: str, revision: str, generated_at: int) -> dict:
    updated_at = _millis(issue.updated_at, fallback=generated_at)
    priority_text = issue.priority.removeprefix("P").removeprefix("p")
    try:
        priority = int(priority_text)
    except ValueError:
        priority = 2
    return {
        "ref": _ref(hive_id, "work-item", issue.id),
        "revision": revision,
        "record": {
            "id": issue.id,
            "title": issue.title,
            "description": "",
            "molType": None,
            "status": issue.status,
            "issueType": issue.issue_type,
            "priority": priority,
            "labels": list(issue.labels),
            "assignee": issue.assignee,
            "createdAt": None,
            "startedAt": None,
            "closedAt": None,
        },
        "createdAt": None,
        "updatedAt": updated_at,
        "freshness": {
            "state": "unknown",
            "asOf": generated_at,
            "expiresAt": None,
            "detail": _MISSING_WORK_ITEM_DETAIL,
        },
    }


def _priority(value: str) -> int:
    try:
        priority = int(value.removeprefix("P").removeprefix("p"))
    except ValueError:
        raise SnapshotProjectionUnavailable("Development work-item priority is invalid") from None
    if priority not in range(5):
        raise SnapshotProjectionUnavailable("Development work-item priority is invalid")
    return priority


def _bounded_text(value: str, maximum: int) -> str:
    return value[:maximum]


def _bounded_optional_text(value: str | None, maximum: int) -> str | None:
    return None if value is None else _bounded_text(value, maximum)


def _summary_order(issue: StreamIssue) -> tuple[object, ...]:
    status_rank = {"in_progress": 0, "blocked": 1, "open": 2}[issue.status.lower()]
    return (
        status_rank,
        _priority(issue.priority),
        -_millis(issue.updated_at),
        issue.id,
    )


def _bounded_summary_candidates(
    issues: Sequence[StreamIssue], hive_id: str
) -> tuple[tuple[StreamIssue, ...], int]:
    """Return the best bounded candidates without materializing an unbounded summary list."""

    retained: list[_WorstFirstIssue] = []
    eligible = 0
    for issue in issues:
        if (
            issue.hive != hive_id
            or issue.status.lower() not in _DEVELOPMENT_WORK_STATUSES
            or issue.issue_type.lower() in _INTERNAL_WORK_ITEM_TYPES
        ):
            continue
        eligible += 1
        ranked = _WorstFirstIssue(issue, _summary_order(issue))
        if len(retained) < DEVELOPMENT_WORK_ITEM_LIMIT:
            heapq.heappush(retained, ranked)
        elif ranked.key < retained[0].key:
            heapq.heapreplace(retained, ranked)
    selected = tuple(item.issue for item in sorted(retained, key=lambda item: item.key))
    if len({issue.id for issue in selected}) != len(selected):
        raise SnapshotProjectionUnavailable("Development work-item identity is ambiguous")
    return selected, eligible


def _summary_counts(
    bead_state: ProviderSnapshot,
    runtime_state: AgentRunSnapshot,
    hive_id: str,
    retained_issue_ids: frozenset[str],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    status_by_id = {
        issue.id: issue.status.lower() for issue in bead_state.issues if issue.hive == hive_id
    }
    blocker_counts: dict[str, int] = {}
    for dependency in bead_state.work_dependencies:
        if (
            dependency.hive != hive_id
            or dependency.issue_id not in retained_issue_ids
            or dependency.type in _NON_BLOCKING_DEPENDENCIES
            or status_by_id.get(dependency.depends_on_id) == "closed"
        ):
            continue
        blocker_counts[dependency.issue_id] = min(
            DEVELOPMENT_MAX_JSON_SAFE_INTEGER,
            blocker_counts.get(dependency.issue_id, 0) + 1,
        )
    gate_counts: dict[str, int] = {}
    for gate in bead_state.gate_requests:
        if gate.hive != hive_id or gate.status.lower() not in {"open", "pending"}:
            continue
        for issue_id in set(gate.blocks).intersection(retained_issue_ids):
            gate_counts[issue_id] = min(
                DEVELOPMENT_MAX_JSON_SAFE_INTEGER,
                gate_counts.get(issue_id, 0) + 1,
            )
    live_agent_counts: dict[str, int] = {}
    for summary in runtime_state.summaries:
        if summary.bead not in retained_issue_ids or summary.state.value not in _LIVE_AGENT_STATES:
            continue
        live_agent_counts[summary.bead] = min(
            DEVELOPMENT_MAX_JSON_SAFE_INTEGER,
            live_agent_counts.get(summary.bead, 0) + 1,
        )
    return blocker_counts, gate_counts, live_agent_counts


def work_item_summary(
    issue: StreamIssue,
    *,
    generated_at: int,
    blocker_count: int,
    open_gate_count: int,
    live_agent_count: int,
) -> dict[str, object]:
    if not issue.id or len(issue.id) > DEVELOPMENT_MAX_ID_LENGTH:
        raise SnapshotProjectionUnavailable("Development work-item id exceeds its bound")
    if not issue.issue_type or len(issue.issue_type) > 128:
        raise SnapshotProjectionUnavailable("Development work-item type exceeds its bound")
    if any(not label for label in issue.labels):
        raise SnapshotProjectionUnavailable("Development work-item label is invalid")
    updated_at = _millis(issue.updated_at)
    if not 0 <= updated_at <= DEVELOPMENT_MAX_JSON_SAFE_INTEGER:
        raise SnapshotProjectionUnavailable("Development work-item timestamp exceeds its bound")
    if issue.status.lower() == "in_progress":
        readiness = "active"
    elif issue.status.lower() == "closed":
        readiness = "completed"
    elif issue.status.lower() == "blocked" or blocker_count or open_gate_count:
        readiness = "blocked"
    elif issue.status.lower() == "open":
        readiness = "ready"
    else:
        readiness = "unavailable"
    labels = [
        _bounded_text(label, DEVELOPMENT_MAX_LABEL_LENGTH)
        for label in issue.labels[:DEVELOPMENT_MAX_LABELS]
    ]
    return {
        "id": issue.id,
        "title": _bounded_text(issue.title, DEVELOPMENT_MAX_TITLE_LENGTH),
        "status": _bounded_text(issue.status, 128),
        "readiness": readiness,
        "issueType": issue.issue_type,
        "priority": _priority(issue.priority),
        "labels": labels[:DEVELOPMENT_MAX_LABELS],
        "remainingLabelCount": min(
            DEVELOPMENT_MAX_JSON_SAFE_INTEGER,
            max(0, len(issue.labels) - DEVELOPMENT_MAX_LABELS),
        ),
        "assignee": _bounded_optional_text(issue.assignee or None, DEVELOPMENT_MAX_ID_LENGTH),
        "owner": _bounded_optional_text(issue.owner or None, DEVELOPMENT_MAX_ID_LENGTH),
        "updatedAt": updated_at,
        "blockerCount": blocker_count,
        "openGateCount": open_gate_count,
        "liveAgentCount": live_agent_count,
    }


def _dependency(
    item: WorkDependency, hive_id: str, revision: str, generated_at: int
) -> dict[str, object]:
    created_at = _millis(item.created_at, fallback=generated_at)
    return {
        "ref": _ref(hive_id, "dependency", item.id),
        "revision": revision,
        "dependentId": _scoped(hive_id, item.issue_id),
        "prerequisiteId": _scoped(hive_id, item.depends_on_id),
        "dependencyType": item.type,
        "createdAt": created_at,
        "updatedAt": generated_at,
    }


def _epics(issues: Sequence[StreamIssue], hive_id: str, revision: str) -> list[dict]:
    children: dict[str, list[str]] = {}
    for issue in issues:
        if issue.parent_id:
            children.setdefault(issue.parent_id, []).append(issue.id)
    out = []
    for issue in issues:
        if issue.issue_type not in {"epic", "molecule"}:
            continue
        updated_at = _millis(issue.updated_at)
        out.append(
            {
                "ref": _ref(hive_id, "epic", issue.id),
                "revision": revision,
                "title": issue.title,
                "status": issue.status,
                "childIds": [
                    _scoped(hive_id, child) for child in sorted(children.get(issue.id, ()))
                ],
                "createdAt": None,
                "updatedAt": updated_at,
            }
        )
    return out


def _gate(
    item: GateRequest,
    hive_id: str,
    revision: str,
    generated_at: int,
    retained_issue_ids: frozenset[str],
) -> dict:
    status = item.status.lower().replace("_", "-")
    if status in {"open", "pending"}:
        status = "pending"
    elif status not in {"approved", "changes-requested"}:
        status = "closed"
    kind = item.gate_kind if item.gate_kind in {"review", "security", "kickoff"} else "other"
    blocks = [
        _ref(hive_id, "work-item", block) for block in item.blocks if block in retained_issue_ids
    ]
    target = blocks[0] if blocks else _ref(hive_id, "gate", item.gate_id)
    requested_at = _millis(item.opened_at, fallback=generated_at)
    return {
        "ref": _ref(hive_id, "gate", item.id),
        "revision": revision,
        "gateKind": kind,
        "status": status,
        "target": target,
        "requestedBy": None,
        "requestedAt": requested_at,
        "updatedAt": _millis(item.resolved_at, fallback=generated_at),
        "blocks": blocks,
    }


def _schedule(
    item: EpicSchedule,
    hive_id: str,
    revision: str,
    generated_at: int,
    retained_issue_ids: frozenset[str],
) -> dict:
    groups = []
    for index, group in enumerate(item.groups):
        issue_ids = [value for value in group.issue_ids if value in retained_issue_ids]
        if not issue_ids:
            continue
        mode = "batch" if group.kind == "planner" else "single"
        groups.append(
            {
                "ref": _ref(hive_id, "schedule-group", f"{item.id}:group:{index}"),
                "revision": revision,
                "epicId": _scoped(hive_id, item.epic_id),
                "mode": mode,
                "workItemIds": [_scoped(hive_id, value) for value in issue_ids],
                "generatedAt": generated_at,
            }
        )
    for label, values in (("single", item.singletons), ("coordinator", item.coordinators)):
        for value in values:
            if value not in retained_issue_ids:
                continue
            groups.append(
                {
                    "ref": _ref(hive_id, "schedule-group", f"{item.id}:{label}:{value}"),
                    "revision": revision,
                    "epicId": _scoped(hive_id, item.epic_id),
                    "mode": "single",
                    "workItemIds": [_scoped(hive_id, value)],
                    "generatedAt": generated_at,
                }
            )
    return {
        "ref": _ref(hive_id, "schedule", item.id),
        "revision": revision,
        "epicId": _scoped(hive_id, item.epic_id),
        "groups": groups,
        "generatedAt": generated_at,
    }


def _state_assignment(
    item: Assignment, hive_id: str, revision: str, generated_at: int
) -> dict[str, object]:
    return {
        "ref": _ref(hive_id, "assignment", item.id),
        "revision": revision,
        "workItemId": _scoped(hive_id, item.issue_id),
        "assigneeAgentId": None,
        "seat": item.seat,
        "assignedAt": generated_at,
        "updatedAt": generated_at,
    }


def _agent(
    summary: AgentRunSummary, hive_id: str, revision: str, generated_at: int
) -> dict[str, object]:
    entity_id = summary.session_id or f"waiting:{summary.bead}"
    return {
        "ref": _ref(hive_id, "agent-run", entity_id),
        "revision": revision,
        "runtime": "beadhive.dispatch",
        "state": summary.state.value,
        "ownerSeat": summary.owner_seat,
        "startedAt": _millis(summary.started_at) if summary.started_at is not None else None,
        "updatedAt": _millis(summary.updated_at, fallback=generated_at),
        "endedAt": _millis(summary.ended_at) if summary.ended_at is not None else None,
        "freshness": _freshness(summary.freshness, generated_at),
    }


def _runtime_assignment(
    summary: AgentRunSummary, hive_id: str, revision: str, generated_at: int
) -> dict[str, object]:
    entity_id = summary.session_id or f"waiting:{summary.bead}"
    return {
        "ref": _ref(hive_id, "assignment", f"runtime:{entity_id}:bead:{summary.bead}"),
        "revision": revision,
        "workItemId": _scoped(hive_id, summary.bead),
        "assigneeAgentId": _scoped(hive_id, entity_id),
        "seat": summary.owner_seat or "unknown",
        "assignedAt": _millis(summary.started_at, fallback=generated_at),
        "updatedAt": _millis(summary.updated_at, fallback=generated_at),
    }


def _source_coverage(
    *,
    state: str,
    generated_at: int,
    detail: str | None,
    system: str,
    instance: str | None,
    requested: int | None,
    returned: int | None,
) -> dict[str, object]:
    return {
        "state": state,
        "requested": requested,
        "returned": returned,
        "fromCache": 0,
        "detail": detail,
        "generatedAt": generated_at,
        "provenance": {
            "system": system,
            "instance": instance,
            "runId": None,
            "documentRef": None,
        },
    }


def hive_operator_snapshot(
    entry: Mapping[str, object],
    bead_state: ProviderSnapshot,
    runtime_state: AgentRunSnapshot,
    *,
    producer_epoch: str,
    sequence: int,
    observed_at: int,
) -> dict[str, object]:
    """Project one byte-bounded, compact, deterministic Development seed snapshot."""

    hive_id = "/".join(str(entry[field]) for field in ("provider", "org", "repo"))
    if type(sequence) is not int or not 0 <= sequence <= DEVELOPMENT_MAX_CURSOR_SEQUENCE:
        raise SnapshotProjectionUnavailable("Development cursor sequence limit exceeded")
    if type(observed_at) is not int or not 0 <= observed_at <= DEVELOPMENT_MAX_JSON_SAFE_INTEGER:
        raise SnapshotProjectionUnavailable("Development cursor timestamp limit exceeded")
    generated_at = _millis(bead_state.as_of, fallback=observed_at)
    if not 0 <= generated_at <= DEVELOPMENT_MAX_JSON_SAFE_INTEGER:
        raise SnapshotProjectionUnavailable("Development generated timestamp limit exceeded")
    revision = _revision(bead_state.revision, runtime_state.revision)
    retained_issues, eligible_count = _bounded_summary_candidates(bead_state.issues, hive_id)
    retained_issue_ids = frozenset(issue.id for issue in retained_issues)
    blocker_counts, gate_counts, live_agent_counts = _summary_counts(
        bead_state, runtime_state, hive_id, retained_issue_ids
    )
    summaries = tuple(
        work_item_summary(
            issue,
            generated_at=generated_at,
            blocker_count=blocker_counts.get(issue.id, 0),
            open_gate_count=gate_counts.get(issue.id, 0),
            live_agent_count=live_agent_counts.get(issue.id, 0),
        )
        for issue in retained_issues
    )
    runtime_state_name = {
        Coverage.COMPLETE: "complete",
        Coverage.PARTIAL: "partial",
        Coverage.DEGRADED: "partial",
        Coverage.UNKNOWN: "unavailable",
    }[runtime_state.coverage]
    runtime_detail = runtime_state.coverage_reason or runtime_state.freshness.detail
    runtime_coverage = _source_coverage(
        state=runtime_state_name,
        generated_at=generated_at,
        detail=runtime_detail,
        system="beadhive.dispatch-summary",
        instance=f"{runtime_state.host_id}:{runtime_state.source_id}",
        requested=None,
        returned=len(runtime_state.summaries),
    )
    limits = {
        "maxBytes": DEVELOPMENT_SNAPSHOT_MAX_BYTES,
        "maxWorkItems": DEVELOPMENT_WORK_ITEM_LIMIT,
    }

    def projected(returned_count: int, *, reserve_cursor_width: bool) -> dict[str, object]:
        selection_reason: str | None
        if returned_count < len(summaries):
            selection_reason = "byte_budget"
        elif eligible_count > len(summaries):
            selection_reason = "structural_cap"
        else:
            selection_reason = None
        beads_detail_parts = []
        if bead_state.partial_reason:
            beads_detail_parts.append(bead_state.partial_reason)
        if selection_reason:
            beads_detail_parts.append(selection_reason)
        beads_coverage = _source_coverage(
            state="partial" if bead_state.partial or selection_reason else "complete",
            generated_at=generated_at,
            detail="; ".join(beads_detail_parts) or None,
            system="beadhive.state-stream",
            instance=None,
            requested=eligible_count,
            returned=returned_count,
        )
        return {
            "schemaVersion": SCHEMA_VERSION,
            "hive": hive_info(entry, canonical_prefix=True),
            "revision": revision,
            "generatedAt": generated_at,
            "cursor": {
                "subscriptionId": hive_subscription_id(hive_id),
                "producerEpoch": producer_epoch,
                "sequence": (DEVELOPMENT_MAX_CURSOR_SEQUENCE if reserve_cursor_width else sequence),
                "observedAt": (
                    DEVELOPMENT_MAX_JSON_SAFE_INTEGER if reserve_cursor_width else observed_at
                ),
            },
            "projectionPolicy": DEVELOPMENT_SNAPSHOT_POLICY,
            "limits": limits,
            "coverage": {
                "state": "partial" if selection_reason else "complete",
                "generatedAt": generated_at,
                "eligible": eligible_count,
                "returned": returned_count,
                "reason": selection_reason,
                "policy": DEVELOPMENT_SNAPSHOT_POLICY,
                "sourceRevision": revision,
                "limits": limits,
                "workItemRetrieval": {
                    "contract": "beadhive.work-items/v1",
                    "revision": revision,
                    "views": ["ready", "active", "blocked", "recent"],
                    "maxPageItems": 200,
                    "maxPageBytes": 917_504,
                    "maxDetailBytes": 917_504,
                },
                "sources": {"beads": beads_coverage, "runtime": runtime_coverage},
            },
            "workItems": list(summaries[:returned_count]),
        }

    def encoded_size(returned_count: int) -> int:
        return len(
            json.dumps(
                projected(returned_count, reserve_cursor_width=True),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    if encoded_size(0) > DEVELOPMENT_SNAPSHOT_MAX_BYTES:
        raise SnapshotProjectionUnavailable("Development snapshot envelope byte limit exceeded")
    low, high = 0, len(summaries)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(middle) <= DEVELOPMENT_SNAPSHOT_MAX_BYTES:
            low = middle
        else:
            high = middle - 1
    return projected(low, reserve_cursor_width=False)


def _activity_kind(name: str) -> str:
    lowered = name.lower()
    if "permission" in lowered:
        return "permission"
    if "tool" in lowered or lowered.startswith("process."):
        return "tool"
    if lowered.startswith("provider.") or lowered.startswith("baml."):
        return "provider-event"
    return "hook"


def run_activity_envelopes(
    records: Sequence[Mapping[str, Any]],
    *,
    producer_epoch: str,
    sequence_offset: int = 0,
    first_occurred_at: int | None = None,
) -> list[dict[str, object]]:
    """Map the journal allowlist without manufacturing transcript or message content."""

    first_at = (
        int(first_occurred_at)
        if first_occurred_at is not None
        else (int(records[0]["timestamp_ms"]) if records else 0)
    )
    envelopes = []
    for sequence, record in enumerate(records, start=sequence_offset + 1):
        activity = dict(record["activity"])
        name = str(activity.get("kind", "activity"))
        occurred_at = int(record["timestamp_ms"])
        envelopes.append(
            {
                "schemaVersion": SCHEMA_VERSION,
                "hiveId": str(record["hive"]),
                "runId": str(record["run_id"]),
                "beadId": record.get("bead"),
                "providerSessionId": record.get("provider_continuation"),
                "driver": str(record["driver"]),
                "provider": str(record["provider"]),
                "protocol": str(record.get("version", "beadhive.run-journal/v1")),
                "occurredAt": occurred_at,
                "elapsedMs": max(0, occurred_at - first_at),
                "sourceRevision": str(record["source_revision"]),
                "producerEpoch": producer_epoch,
                "sequence": sequence,
                "payload": {
                    "kind": _activity_kind(name),
                    "name": name,
                    "text": None,
                    "detail": {"writer": record["writer"], "activity": activity},
                },
            }
        )
    return envelopes


def run_activity_page_frame(
    journal: RunJournalFrame,
    records: Sequence[Mapping[str, Any]],
    *,
    producer_epoch: str,
    base_sequence: int,
    kind: str,
    reset_reason: str | None = None,
) -> dict[str, object]:
    """Map one bounded history page while retaining absolute run-sequence truth."""

    if kind not in {"snapshot", "delta", "reset"}:
        raise ValueError("activity frame kind must be snapshot, delta, or reset")
    if (kind == "reset") != (reset_reason is not None):
        raise ValueError("activity reset frames require exactly one reset reason")
    first_at = int(journal.records[0]["timestamp_ms"]) if journal.records else None
    activities = run_activity_envelopes(
        records,
        producer_epoch=producer_epoch,
        sequence_offset=base_sequence,
        first_occurred_at=first_at,
    )
    coverage = {
        Coverage.COMPLETE: "complete",
        Coverage.PARTIAL: "partial",
        Coverage.DEGRADED: "partial",
        Coverage.UNKNOWN: "unavailable",
    }[journal.coverage]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": kind,
        "hiveId": str(journal.records[0]["hive"]),
        "runId": journal.run_id,
        "producerEpoch": producer_epoch,
        "sequence": base_sequence + len(records),
        "baseSequence": base_sequence if kind == "delta" else 0,
        "sourceRevision": str(journal.source_revision),
        "coverage": {"state": coverage, "detail": journal.coverage_reason},
        "resetReason": reset_reason,
        "activities": activities,
    }


def run_activity_frame(
    journal: RunJournalFrame,
    records: Sequence[Mapping[str, Any]],
    *,
    producer_epoch: str,
    base_sequence: int,
    kind: str,
    reset_reason: str | None = None,
) -> dict[str, object]:
    if kind not in {"snapshot", "delta", "reset"}:
        raise ValueError("activity frame kind must be snapshot, delta, or reset")
    if (kind == "reset") != (reset_reason is not None):
        raise ValueError("activity reset frames require exactly one reset reason")
    all_envelopes = run_activity_envelopes(records, producer_epoch=producer_epoch)
    selected = all_envelopes[base_sequence:] if kind == "delta" else all_envelopes
    coverage = {
        Coverage.COMPLETE: "complete",
        Coverage.PARTIAL: "partial",
        Coverage.DEGRADED: "partial",
        Coverage.UNKNOWN: "unavailable",
    }[journal.coverage]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": kind,
        "hiveId": str(records[0]["hive"]),
        "runId": journal.run_id,
        "producerEpoch": producer_epoch,
        "sequence": len(records),
        "baseSequence": base_sequence if kind == "delta" else 0,
        "sourceRevision": str(journal.source_revision),
        "coverage": {"state": coverage, "detail": journal.coverage_reason},
        "resetReason": reset_reason,
        "activities": selected,
    }
