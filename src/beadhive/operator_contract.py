"""Phase-one operator HTTP wire projections.

The browser contracts are intentionally represented as JSON-ready dictionaries here.  They
are a boundary owned by the unified host, not a second persistence model: beads, dispatch
summaries, and run journals retain their distinct authority and revision domains.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import operator_actions
from .agent_run_summary import AgentRunSummary, Freshness
from .public_readers import AgentRunSnapshot, Coverage, RunJournalFrame
from .state_stream import (
    Assignment,
    EpicSchedule,
    GateRequest,
    ProviderSnapshot,
    StreamIssue,
    WorkDependency,
)

SCHEMA_VERSION = 1
_MISSING_WORK_ITEM_DETAIL = (
    "The state stream does not expose description, molecule type, or lifecycle timestamps."
)


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
    opaque_ref = f"hive-sha256-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
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


def factory_hive_page_revision(items: Sequence[Mapping[str, object]]) -> str:
    """Return the opaque revision shared by ETags and snapshot-scoped cursors."""

    return _revision("factory-hives-v1", list(items))


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


def _gate(item: GateRequest, hive_id: str, revision: str, generated_at: int) -> dict:
    status = item.status.lower().replace("_", "-")
    if status in {"open", "pending"}:
        status = "pending"
    elif status not in {"approved", "changes-requested"}:
        status = "closed"
    kind = item.gate_kind if item.gate_kind in {"review", "security", "kickoff"} else "other"
    blocks = [_ref(hive_id, "work-item", block) for block in item.blocks]
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


def _schedule(item: EpicSchedule, hive_id: str, revision: str, generated_at: int) -> dict:
    groups = []
    for index, group in enumerate(item.groups):
        mode = "batch" if group.kind == "planner" else "single"
        groups.append(
            {
                "ref": _ref(hive_id, "schedule-group", f"{item.id}:group:{index}"),
                "revision": revision,
                "epicId": _scoped(hive_id, item.epic_id),
                "mode": mode,
                "workItemIds": [_scoped(hive_id, value) for value in group.issue_ids],
                "generatedAt": generated_at,
            }
        )
    for label, values in (("single", item.singletons), ("coordinator", item.coordinators)):
        for value in values:
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
    """Project independently-labelled real sources into the UI's direct snapshot contract."""

    hive_id = "/".join(str(entry[field]) for field in ("provider", "org", "repo"))
    generated_at = _millis(bead_state.as_of, fallback=observed_at)
    revision = _revision(bead_state.revision, runtime_state.revision)
    bead_detail = _MISSING_WORK_ITEM_DETAIL
    if bead_state.partial_reason:
        bead_detail = f"{bead_state.partial_reason}; {bead_detail}"
    beads_coverage = _source_coverage(
        state="partial",
        generated_at=generated_at,
        detail=bead_detail,
        system="beadhive.state-stream",
        instance=None,
        requested=None,
        returned=len(bead_state.issues),
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
    assignments = [
        _state_assignment(item, hive_id, bead_state.revision, generated_at)
        for item in bead_state.assignments
    ]
    assignments.extend(
        _runtime_assignment(summary, hive_id, runtime_state.revision, generated_at)
        for summary in runtime_state.summaries
        if summary.bead
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "hive": hive_info(entry, canonical_prefix=True),
        "revision": revision,
        "generatedAt": generated_at,
        "cursor": {
            "subscriptionId": f"hive:{hive_id}",
            "producerEpoch": producer_epoch,
            "sequence": sequence,
            "observedAt": observed_at,
        },
        "coverage": {
            "state": "partial",
            "generatedAt": generated_at,
            "sources": {"beads": beads_coverage, "runtime": runtime_coverage},
        },
        "workItems": [
            _work_item(item, hive_id, bead_state.revision, generated_at)
            for item in bead_state.issues
        ],
        "dependencies": [
            _dependency(item, hive_id, bead_state.revision, generated_at)
            for item in bead_state.work_dependencies
        ],
        "epics": _epics(bead_state.issues, hive_id, bead_state.revision),
        "gates": [
            _gate(item, hive_id, bead_state.revision, generated_at)
            for item in bead_state.gate_requests
        ],
        "agents": [
            _agent(item, hive_id, runtime_state.revision, generated_at)
            for item in runtime_state.summaries
        ],
        "assignments": assignments,
        "schedules": [
            _schedule(item, hive_id, bead_state.revision, generated_at)
            for item in bead_state.epic_schedules
        ],
        "evidence": [],
        "advertisedActions": [],
    }


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
) -> list[dict[str, object]]:
    """Map the journal allowlist without manufacturing transcript or message content."""

    first_at = int(records[0]["timestamp_ms"]) if records else 0
    envelopes = []
    for sequence, record in enumerate(records, start=1):
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
                "protocol": "beadhive.run-journal/v1",
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


def run_activity_frame(
    journal: RunJournalFrame,
    records: Sequence[Mapping[str, Any]],
    *,
    producer_epoch: str,
    base_sequence: int,
    kind: str,
) -> dict[str, object]:
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
        "resetReason": None,
        "activities": selected,
    }
