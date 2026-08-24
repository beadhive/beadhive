"""Lifecycle events and best-effort flow metrics used by ``bh work``.

All reads and emissions retain their historical failure semantics: missing event data skips the
affected metric, negative durations are discarded, and callers may wrap the complete emission so
telemetry can never block a merge.  ``beadhive.work`` re-exports these names for compatibility.
"""

from __future__ import annotations

import datetime

from . import bd, guard, otel, state, work_logic
from .work_guards import first


def hive(entry) -> str:
    return str(entry.get("prefix", "") or "")


def validation_result(rc: int, retryable_exit: int = 75) -> str:
    if rc == 0:
        return "pass"
    if rc == retryable_exit:
        return "retryable"
    return "fail"


def parse_ts(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)
    except (ValueError, TypeError):
        return None


def emit_delta(record_fn, end, start, attrs) -> None:
    if end is None or start is None:
        return
    delta = (end - start).total_seconds()
    if delta >= 0:
        record_fn(delta, attrs)


def flow_events(bead, cwd):
    rows = bd.json(["list", "--parent", bead, "--include-infra"], cwd)
    if not isinstance(rows, list):
        return None
    return [
        row for row in rows if isinstance(row, dict) and str(row.get("issue_type") or "") == "event"
    ]


def event_text(event) -> str:
    return " ".join(
        str(event.get(key) or "") for key in ("title", "description", "reason", "to_state", "state")
    ).lower()


def is_review_pending(event) -> bool:
    text = event_text(event)
    return "review" in text and "pending" in text


def is_changes_requested(event) -> bool:
    text = event_text(event)
    return "changes-requested" in text or "changes_requested" in text


def is_dispatch_cause(event, cause: str) -> bool:
    text = event_text(event)
    return "dispatch" in text and cause in text


def dispatch_cause_count(events, cause: str) -> int:
    if cause not in state.STATE_DIMENSIONS[state.DISPATCH_DIM]:
        raise ValueError(f"unknown dispatch cause: {cause!r}")
    return sum(1 for event in (events or []) if is_dispatch_cause(event, cause))


def record_dispatch_failure(bead, cause: str, reason: str, cwd, *, actor="") -> bool:
    if cause not in state.STATE_DIMENSIONS[state.DISPATCH_DIM]:
        raise ValueError(f"unknown dispatch cause: {cause!r}")
    result = bd.run(["set-state", bead, f"dispatch={cause}", "--reason", reason], cwd, actor=actor)
    return result.returncode == 0


def review_pending_at(events):
    for event in events:
        if is_review_pending(event):
            return parse_ts(first(event, "created_at", "created"))
    return None


def clear_review_label(bead, data, main, actor="") -> None:
    labels = data.get("labels") if isinstance(data, dict) else None
    for label in labels or []:
        if str(label).startswith("review:"):
            bd.run(["label", "remove", bead, str(label)], main, actor=actor)


def strip_review_pending(row, main, actor) -> int:
    bead = str(row.get("id") or "") if isinstance(row, dict) else ""
    if not bead:
        return 0
    result = bd.run(["label", "remove", bead, "review:pending"], main, actor=actor)
    return int(result.returncode == 0)


def backfill_stale_review_labels(main, actor="") -> int:
    rows = bd.json(["list", "--status", "closed", "--label", "review:pending"], main)
    if not isinstance(rows, list):
        return 0
    return sum(strip_review_pending(row, main, actor) for row in rows)


def open_gates(cwd) -> list:
    gates = bd.json(["gate", "list", "--all", "--limit", "0"], cwd)
    return gates if isinstance(gates, list) else []


def match_gate(gates, bead, matcher):
    return next(
        (gate for gate in gates if bd.names_bead(gate.get("description"), bead) and matcher(gate)),
        None,
    )


def security_gate(gates, bead):
    return match_gate(gates, bead, guard.is_security_gate)


def release_hold_gate(gates, bead):
    return match_gate(gates, bead, guard.is_release_hold_gate)


def stage_recorder(stage):
    return lambda seconds, attrs: otel.record_stage(stage, seconds, attrs)


def emit_cycle(data, attrs) -> None:
    now = datetime.datetime.now(datetime.UTC)
    created = parse_ts(first(data or {}, "created_at", "created"))
    started = parse_ts(first(data or {}, "started_at", "started"))
    emit_delta(otel.record_cycle_time, now, created, attrs)
    emit_delta(otel.record_cycle_time_active, now, started, attrs)


def emit_bead_flow(bead, data, main, attrs) -> None:
    emit_cycle(data, attrs)
    now = datetime.datetime.now(datetime.UTC)
    started = parse_ts(first(data or {}, "started_at", "started"))

    events = flow_events(bead, main)
    event_pending_at = None
    if events is not None:
        event_pending_at = review_pending_at(events)
        otel.record_rework(sum(1 for event in events if is_changes_requested(event)), attrs)

    open_review, resolved_review = work_logic.review_gates(bead, main)
    gate = open_review[0] if open_review else (resolved_review[-1] if resolved_review else None)
    gate_closed_at = parse_ts(first(gate or {}, "closed_at", "resolved_at")) if gate else None
    gate_opened_at = parse_ts(first(gate or {}, "created_at", "created")) if gate else None
    review_pending = event_pending_at or gate_opened_at

    emit_delta(stage_recorder("coding"), review_pending, started, attrs)
    emit_delta(stage_recorder("review_wait"), gate_closed_at, review_pending, attrs)
    emit_delta(stage_recorder("merge_latency"), now, gate_closed_at, attrs)
