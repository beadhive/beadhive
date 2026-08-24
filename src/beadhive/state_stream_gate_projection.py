"""Pure GateRequest projection for the bd-backed state stream.

The polling adapter owns the one scope-level gate read.  This module owns only the normative
mapping from complete gate rows plus canonical WorkDependency records to public GateRequest
entities, including retention and fail-closed aggregate hive identity.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import work_logic
from .state_stream import (
    GateRequest,
    StreamRequest,
    StreamScope,
    WorkDependency,
    projection_id,
)

RESOLVED_RETENTION = timedelta(hours=24)


@dataclass(frozen=True)
class GateProjection:
    gate_requests: tuple[GateRequest, ...]
    partial_reason: str | None = None


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _reason(gate: dict) -> str:
    if "reason" in gate and gate["reason"] is not None:
        return str(gate["reason"])
    description = str(gate.get("description") or "")
    marker = re.search(r"reason:", description, flags=re.IGNORECASE)
    return description[marker.end() :].strip() if marker else ""


def _kind(gate: dict) -> str:
    classified = work_logic._gate_kind(gate)
    return classified if classified in {"review", "security", "kickoff"} else "other"


def _gate_hive(
    gate_id: str,
    request: StreamRequest,
    dependencies: tuple[WorkDependency, ...],
) -> str | None:
    if request.scope is StreamScope.HIVE:
        return request.hive
    candidates = {
        dependency.hive
        for dependency in dependencies
        if dependency.type == "blocks" and dependency.depends_on_id == gate_id
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _blocks(gate_id: str, hive: str, dependencies: tuple[WorkDependency, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                dependency.issue_id
                for dependency in dependencies
                if dependency.hive == hive
                and dependency.type == "blocks"
                and dependency.depends_on_id == gate_id
            }
        )
    )


def project_gate_requests(
    raw_gates: Iterable[object],
    *,
    request: StreamRequest,
    work_dependencies: Iterable[WorkDependency],
    as_of: datetime,
) -> GateProjection:
    """Project all relevant gates, omitting only malformed or unidentifiable rows."""

    as_of = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    dependencies = tuple(work_dependencies)
    projected: list[GateRequest] = []
    partial_reason = None

    rows = tuple(raw_gates)
    gate_ids = [
        str(row.get("id") or "")
        for row in rows
        if isinstance(row, dict) and row.get("issue_type") == "gate"
    ]
    duplicates = {gate_id for gate_id, count in Counter(gate_ids).items() if gate_id and count > 1}

    for raw in rows:
        if not isinstance(raw, dict) or raw.get("issue_type") != "gate":
            partial_reason = partial_reason or "invalid_gate_record"
            continue
        gate_id = str(raw.get("id") or "")
        opened_at = str(raw.get("created_at") or "")
        if not gate_id or not opened_at or _timestamp(opened_at) is None or gate_id in duplicates:
            partial_reason = partial_reason or "invalid_gate_record"
            continue

        hive = _gate_hive(gate_id, request, dependencies)
        if not hive:
            partial_reason = partial_reason or "gate_hive_identity_unavailable"
            continue

        raw_status = str(raw.get("status") or "")
        status = "open" if raw_status == "open" else "resolved"
        closed_value = raw.get("closed_at")
        closed_at = str(closed_value) if closed_value is not None else None
        resolved = _timestamp(closed_at)
        if status == "resolved":
            if resolved is None:
                partial_reason = partial_reason or "gate_resolution_timestamp_unavailable"
                continue
            if not as_of - RESOLVED_RETENTION <= resolved <= as_of:
                continue
        elif closed_at is not None and resolved is None:
            # Preserve the contract's "an open gate is always present" rule without emitting an
            # invalid date-time from a contradictory backend row.
            closed_at = None
            partial_reason = partial_reason or "gate_resolution_timestamp_unavailable"

        projected.append(
            GateRequest(
                id=projection_id("gate-request", (hive, gate_id)),
                hive=hive,
                gate_id=gate_id,
                blocks=_blocks(gate_id, hive, dependencies),
                gate_type=(str(raw["await_type"]) if raw.get("await_type") is not None else None),
                gate_kind=_kind(raw),
                status=status,
                reason=_reason(raw),
                opened_at=opened_at,
                resolved_at=closed_at,
            )
        )

    return GateProjection(
        gate_requests=tuple(sorted(projected, key=lambda gate: gate.id)),
        partial_reason=partial_reason,
    )
