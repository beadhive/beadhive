# ruff: noqa: E501
"""Nearly-rendered, Herdr-specific views over generic Beadhive facts.

This module owns presentation policy only.  Queue membership, readiness, blocker reasons,
action availability, and live pane ownership are consumed from the generic operator and Herdr
roster contracts.  Lifecycle commands remain the mutation authority.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import typer

from . import (
    config,
    host,
    jsonout,
    operator_actions,
    operator_contract,
    operator_work_items,
)
from .operator_sources import OperatorSourceError, OperatorSources

SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
STREAM_DEFAULT_LIMIT = 50
_SESSION = "bh-supervisor"
_SAFE_ID = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")
_ACTION_LABELS = {
    "hive.inspect": "Open Hive Deck",
    "hive.refresh": "Refresh",
    "work-item.inspect": "View detail",
    "work-item.refresh": "Refresh detail",
    "work-item.launch": "Launch default",
    "agent.inspect": "Inspect agent",
    "agent.attach": "Attach instructions",
    "agent.dispatch": "Instruct…",
    "agent.watch": "Watch until attention",
    "agent.reap": "Reap pane…",
}
_STYLE = {
    "ready": ("READY", ">"),
    "active": ("RUNNING", "●"),
    "blocked": ("BLOCKED", "!"),
    "completed": ("FINISHED", "✓"),
    "unavailable": ("UNAVAILABLE", "?"),
    "working": ("WORKING", "●"),
    "idle": ("IDLE", "○"),
    "done": ("FINISHED", "✓"),
    "failed": ("FAILED", "×"),
    "unknown": ("UNKNOWN", "?"),
    "stale": ("STALE", "?"),
}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _token(value: object, limit: int = 120) -> str:
    """Collapse untrusted text into one bounded, control-free terminal token."""
    text = str(value or "")
    safe = "".join(
        " " if character.isspace() else character
        for character in text
        if character.isspace() or not unicodedata.category(character).startswith("C")
    )
    return " ".join(safe.split())[:limit]


def _safe_identity(value: object) -> bool:
    text = str(value or "")
    return bool(text) and len(text) <= 256 and all(character in _SAFE_ID for character in text)


def _revision(*values: object) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _cursor_encode(view: str, revision: str, scope: Mapping[str, object], offset: int) -> str:
    raw = json.dumps(
        {"v": 1, "view": view, "revision": revision, "scope": scope, "offset": offset},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_offset(
    cursor: str | None, *, view: str, revision: str, scope: Mapping[str, object]
) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorSourceError(
            "invalid_view_cursor", "The Herdr view cursor is malformed.", status_code=400
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("v") != 1
        or value.get("view") != view
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
    ):
        raise OperatorSourceError(
            "invalid_view_cursor", "The Herdr view cursor is malformed.", status_code=400
        )
    if value.get("scope") != dict(scope):
        raise OperatorSourceError(
            "view_cursor_scope_mismatch",
            "The Herdr view cursor belongs to a different view or scope.",
            status_code=409,
        )
    if value.get("revision") != revision:
        raise OperatorSourceError(
            "view_cursor_revision_mismatch",
            "The projection changed; restart without a cursor.",
            status_code=409,
        )
    return value["offset"]


def _page(
    items: list[dict],
    *,
    view: str,
    revision: str,
    scope: Mapping[str, object],
    limit: int,
    cursor: str | None,
) -> tuple[list[dict], bool, str | None]:
    offset = _cursor_offset(cursor, view=view, revision=revision, scope=scope)
    if offset > len(items):
        raise OperatorSourceError(
            "invalid_view_cursor",
            "The Herdr view cursor is outside the collection.",
            status_code=400,
        )
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    truncated = next_offset < len(items)
    return (
        selected,
        truncated,
        _cursor_encode(view, revision, scope, next_offset) if truncated else None,
    )


def _base(
    view: str,
    revision: str,
    *,
    scope: dict[str, object],
    freshness: dict[str, object],
    coverage: dict[str, object],
    warnings: Iterable[str] = (),
) -> dict[str, object]:
    return jsonout.envelope(
        f"plugin herdr view {view}",
        SCHEMA_VERSION,
        {
            "view": view,
            "revision": revision,
            "generated_at": _now_ms(),
            "scope": scope,
            "freshness": freshness,
            "coverage": coverage,
            "warnings": [_token(value, 240) for value in warnings if value],
        },
    )


def _view_freshness(value: Mapping[str, object] | None) -> dict[str, object]:
    value = dict(value or {})
    return {
        "state": str(value.get("state") or "unknown"),
        "as_of": value.get("as_of", value.get("asOf")),
        "expires_at": value.get("expires_at", value.get("expiresAt")),
        "detail": _token(value.get("detail"), 240) or None,
    }


def _layout_variant(width: int) -> str:
    if width < 80:
        return "narrow"
    if width < 120:
        return "medium"
    return "wide"


def layout_payload(hive: str | None, context: Mapping[str, object] | None = None) -> dict:
    context = dict(context or {})
    allowed = {"width", "height", "invoking_session", "workspace_id", "tab_id", "pane_id"}
    unknown = sorted(set(context) - allowed)
    if unknown:
        raise OperatorSourceError(
            "invalid_layout_context",
            f"Unknown layout context field: {unknown[0]}.",
            status_code=400,
        )
    width = context.get("width", 120)
    height = context.get("height", 40)
    if not isinstance(width, int) or not 40 <= width <= 1000:
        raise OperatorSourceError(
            "invalid_layout_context",
            "Layout width must be an integer from 40 through 1000.",
            status_code=400,
        )
    if not isinstance(height, int) or not 12 <= height <= 500:
        raise OperatorSourceError(
            "invalid_layout_context",
            "Layout height must be an integer from 12 through 500.",
            status_code=400,
        )
    if hive is not None and not all(_safe_identity(part) for part in hive.split("/")):
        raise OperatorSourceError(
            "invalid_hive_identity", "Hive identity is not canonical.", status_code=400
        )
    variant = _layout_variant(width)
    if variant == "wide":
        deck = {"variant": variant, "section_mode": "columns", "columns": 3, "inspector": "below"}
    elif variant == "medium":
        deck = {"variant": variant, "section_mode": "tabs", "columns": 1, "inspector": "below"}
    else:
        deck = {
            "variant": variant,
            "section_mode": "single-list",
            "columns": 1,
            "inspector": "overlay",
            "section_order": ["needs-you", "running", "ready"],
        }
    revision = _revision("layout-v1", hive, width, height)
    payload = _base(
        "layout",
        revision,
        scope={"hive": hive},
        freshness={"state": "fresh", "as_of": _now_ms()},
        coverage={"state": "complete", "sources": {"layout": {"state": "complete"}}},
    )
    payload["layout"] = {
        "session": _SESSION,
        "cross_session_focus": False,
        "workspace_label": f"bh:{hive}" if hive else None,
        "workspace_tokens": {"bh.hive_id": hive} if hive else {},
        "tabs": [
            {"role": "board", "label": "Board", "owns_agents": False},
            {"role": "agents", "label": "Agents", "owns_agents": True},
        ],
        "surfaces": {
            "picker": {
                "placement": "popup",
                "width": "80%",
                "height": "70%",
                "lifecycle": "session-modal",
                "pane_id": None,
                "close_behavior": "exit-after-handoff-or-cancel",
            },
            "deck": {"placement": "tab", "focus": False, **deck},
            "agent_actions": {
                "placement": "popup",
                "lifecycle": "session-modal",
                "pane_id": None,
            },
            "activity_tray": {
                "placement": "split",
                "direction": "right",
                "ratio": 0.28,
                "lifecycle": "ordinary-pane",
                "hide_behavior": "close",
                "show_behavior": "reopen-split",
                "native_collapsible": False,
            },
        },
        "viewport": {"width": width, "height": height},
    }
    payload["context"] = {
        "invoking_session": _token(context.get("invoking_session"), 80) or None,
        "workspace_id": _token(context.get("workspace_id"), 80) or None,
        "tab_id": _token(context.get("tab_id"), 80) or None,
        "pane_id": _token(context.get("pane_id"), 80) or None,
    }
    return payload


def _action_key(action: Mapping[str, object]) -> str:
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    return f"{target.get('kind', 'entity')}:{target.get('id', '')}:{action.get('id', '')}"


def _invoke_for(action: Mapping[str, object]) -> tuple[list[str] | None, str]:
    action_id = str(action.get("id") or "")
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    hive = str(target.get("hiveId") or "")
    entity = str(target.get("id") or "")
    if not all(_safe_identity(part) for part in hive.split("/")):
        return None, "none"
    if action_id in {"hive.inspect", "hive.refresh"}:
        if entity != hive:
            return None, "none"
        return ["bh", "plugin", "herdr", "view", "deck", "--hive", hive, "--json"], "none"
    if not _safe_identity(entity):
        return None, "none"
    if action_id in {"work-item.inspect", "work-item.refresh"}:
        return [
            "bh",
            "plugin",
            "herdr",
            "view",
            "bead",
            "--hive",
            hive,
            "--bead",
            entity,
            "--json",
        ], "none"
    if action_id == "work-item.launch":
        return [
            "bh",
            "plugin",
            "herdr",
            "launch",
            entity,
            "--hive",
            hive,
            "--no-focus",
            "--json",
        ], "parameters"
    if action_id == "agent.inspect":
        return ["bh", "plugin", "herdr", "view", "agent", "--target", entity, "--json"], "none"
    lifecycle = {
        "agent.attach": ["attach", entity, "--json"],
        "agent.dispatch": ["dispatch", entity, "--stdin", "--json"],
        "agent.watch": ["watch", entity, "--timeout", "600", "--json"],
        "agent.reap": ["reap", entity, "--json"],
    }.get(action_id)
    if lifecycle is None:
        return None, "none"
    transport = "stdin" if action_id == "agent.dispatch" else "parameters"
    return ["bh", "plugin", "herdr", *lifecycle], transport


def render_action(action: Mapping[str, object]) -> dict[str, object]:
    availability = str(action.get("availability") or "unavailable")
    argv, transport = _invoke_for(action)
    reason = _token(action.get("reason"), 240)
    reason_code = action.get("reasonCode")
    if argv is None and availability in {"allowed", "confirmation-required"}:
        availability = "unavailable"
        reason_code = "unsafe_entity_identity"
        reason = "entity identity cannot be delegated safely"
    invoke = None
    if argv is not None and availability in {"allowed", "confirmation-required"}:
        invoke = {"argv": argv, "input": transport, "shell": False}
    return {
        "id": _action_key(action),
        "source_action": action.get("id"),
        "label": _ACTION_LABELS.get(str(action.get("id")), _token(action.get("id"), 80)),
        "availability": availability,
        "reason_code": reason_code,
        "reason": reason,
        "consequence": action.get("consequence"),
        "preconditions": action.get("preconditions") or {},
        "input": action.get("input") or {},
        "invoke": invoke,
    }


def _disable_mutations(row: dict, actions: list[dict], reason_code: str, reason: str) -> None:
    """Keep disconnected data visible without leaving a stale mutation executable."""
    for action in actions:
        if action["consequence"] not in {"reversible-write", "approval", "destructive"}:
            continue
        action["availability"] = "unavailable"
        action["reason_code"] = reason_code
        action["reason"] = reason
        action["invoke"] = None
    if row.get("primary_action") and not any(
        action["id"] == row["primary_action"] and action["invoke"] is not None for action in actions
    ):
        row["primary_action"] = next(
            (
                action["id"]
                for action in actions
                if action["invoke"] is not None and action["consequence"] in {"navigate", "read"}
            ),
            None,
        )


def _work_row(item: Mapping[str, object]) -> tuple[dict, list[dict]]:
    readiness = item.get("readiness") if isinstance(item.get("readiness"), dict) else {}
    state = str(readiness.get("state") or "unavailable")
    label, glyph = _STYLE.get(state, _STYLE["unavailable"])
    priority = f"P{item.get('priority', 2)}"
    key = f"work-item:{item.get('hiveId')}:{item.get('id')}"
    badges = [
        {"text": priority, "style": "priority"},
        {"text": label, "style": state},
    ]
    rendered = [render_action(action) for action in item.get("advertisedActions", [])]
    action_ids = [action["id"] for action in rendered]
    primary = None
    preferred = "work-item.launch" if state == "ready" else "work-item.inspect"
    for action in rendered:
        if action["source_action"] == preferred and action["availability"] in {
            "allowed",
            "confirmation-required",
        }:
            primary = action["id"]
            break
    secondary_parts = [label.lower(), _token(readiness.get("reason"), 120)]
    if item.get("blockerCount") is not None:
        secondary_parts.append(f"{item.get('blockerCount')} blockers")
    return (
        {
            "key": key,
            "primary": _token(f"{priority} {item.get('id')} {item.get('title')}", 160),
            "secondary": _token(" · ".join(value for value in secondary_parts if value), 200),
            "style": state,
            "glyph": glyph,
            "badges": badges,
            "entity": {"kind": "work-item", "hive": item.get("hiveId"), "id": item.get("id")},
            "tokens": {
                "priority": priority,
                "id": _token(item.get("id"), 80),
                "title": _token(item.get("title"), 120),
                "state": label,
                "issue_type": _token(item.get("issueType"), 40),
                "status": _token(item.get("status"), 40),
            },
            "primary_action": primary,
            "actions": action_ids,
        },
        rendered,
    )


def _agent_row(agent: Mapping[str, object]) -> tuple[dict, list[dict]]:
    lifecycle = agent.get("lifecycle") if isinstance(agent.get("lifecycle"), dict) else {}
    ownership = agent.get("ownership") if isinstance(agent.get("ownership"), dict) else {}
    state = str(lifecycle.get("state") or "unknown")
    if ownership.get("state") not in {"owned", None}:
        style = "stale"
    else:
        style = state if state in _STYLE else "unknown"
    label, glyph = _STYLE.get(style, _STYLE["unknown"])
    target = agent.get("target")
    key = f"agent:{agent.get('hive')}:{target}"
    rendered = [render_action(item) for item in agent.get("advertised_actions", [])]
    enabled = [item["id"] for item in rendered]
    primary = None
    preferred = "agent.dispatch" if state == "blocked" else "agent.inspect"
    for action in rendered:
        if action["source_action"] == preferred and action["availability"] in {
            "allowed",
            "confirmation-required",
        }:
            primary = action["id"]
            break
    return (
        {
            "key": key,
            "primary": _token(
                f"{target or 'unmanaged'} {agent.get('bead') or 'unassociated'}", 160
            ),
            "secondary": _token(
                f"{label.lower()} · {ownership.get('reason') or 'ownership unknown'}", 200
            ),
            "style": style,
            "glyph": glyph,
            "badges": [
                {"text": label, "style": style},
                {"text": str(ownership.get("state") or "unknown").upper(), "style": "ownership"},
            ],
            "entity": {"kind": "agent", "hive": agent.get("hive"), "id": target},
            "tokens": {
                "target": _token(target, 80),
                "bead": _token(agent.get("bead"), 80),
                "state": label,
                "hive": _token(agent.get("hive"), 160),
                "ownership": _token(ownership.get("state"), 40),
            },
            "primary_action": primary,
            "actions": enabled,
        },
        rendered,
    )


def picker_payload(
    summaries: list[dict],
    roster: Mapping[str, object],
    *,
    limit: int,
    cursor: str | None,
) -> dict:
    agents_complete = roster.get("revision") != "unavailable"
    agent_counts: dict[str, dict[str, int]] = {}
    for agent in roster.get("agents", []):
        if not isinstance(agent, dict) or not agent.get("hive"):
            continue
        counts = agent_counts.setdefault(str(agent["hive"]), {"running": 0, "needs_attention": 0})
        state = str((agent.get("lifecycle") or {}).get("state") or "unknown")
        if state in {"idle", "working"}:
            counts["running"] += 1
        elif state in {"blocked", "failed"}:
            counts["needs_attention"] += 1
    rows = []
    actions = []
    for summary in summaries:
        hive = str(summary["id"])
        counts = dict(summary.get("counts") or {})
        counts.update(
            agent_counts.get(
                hive,
                {
                    "running": 0 if agents_complete else None,
                    "needs_attention": 0 if agents_complete else None,
                },
            )
        )
        ready_count = counts.get("ready") if counts.get("ready") is not None else "?"
        running_count = counts.get("running") if counts.get("running") is not None else "?"
        attention_count = (
            counts.get("needs_attention") if counts.get("needs_attention") is not None else "?"
        )
        rendered_actions = [render_action(item) for item in summary.get("advertisedActions", [])]
        action_ids = [item["id"] for item in rendered_actions]
        availability = str((summary.get("availability") or {}).get("state") or "unavailable")
        rows.append(
            {
                "key": f"hive:{hive}",
                "primary": _token(f"{summary.get('displayLabel')}  {hive}", 160),
                "secondary": _token(
                    f"{availability} · ready {ready_count} · running {running_count} · needs you {attention_count}",
                    200,
                ),
                "style": availability,
                "glyph": "●" if availability == "available" else "!",
                "badges": [
                    {"text": availability.upper(), "style": availability},
                    {"text": f"READY {ready_count}", "style": "ready"},
                    {"text": f"NEEDS YOU {attention_count}", "style": "blocked"},
                ],
                "entity": {"kind": "hive", "id": hive},
                "tokens": {
                    "label": _token(summary.get("displayLabel"), 80),
                    "identity": _token(hive, 160),
                    "availability": availability.upper(),
                    "provider": _token(summary.get("provider"), 40),
                    "organization": _token(summary.get("org"), 80),
                    "repository": _token(summary.get("repo"), 80),
                    "prefix": _token(summary.get("prefix"), 40),
                },
                "primary_action": action_ids[0] if action_ids else None,
                "actions": action_ids,
                "counts": counts,
            }
        )
        actions.extend(rendered_actions)
    rows.sort(
        key=lambda row: (
            -int(row["counts"].get("needs_attention") or 0),
            -int(row["counts"].get("running") or 0),
            str(row["entity"]["id"]),
        )
    )
    revision = _revision("picker-v1", summaries, roster.get("revision"))
    selected, truncated, next_cursor = _page(
        rows,
        view="picker",
        revision=revision,
        scope={},
        limit=limit,
        cursor=cursor,
    )
    selected_action_ids = {item for row in selected for item in row["actions"]}
    factory_complete = all(
        str((summary.get("coverage") or {}).get("state") or "complete") == "complete"
        and str((summary.get("availability") or {}).get("state") or "unavailable") == "available"
        for summary in summaries
    )
    payload = _base(
        "picker",
        revision,
        scope={},
        freshness={"state": "fresh", "as_of": _now_ms()},
        coverage={
            "state": "complete" if factory_complete and agents_complete else "partial",
            "sources": {
                "factory": {"state": "complete" if factory_complete else "partial"},
                "agents": {"state": "complete" if agents_complete else "unavailable"},
            },
        },
        warnings=roster.get("warnings", []),
    )
    payload.update(
        {
            "surface": {
                "placement": "popup",
                "width": "80%",
                "height": "70%",
                "lifecycle": "session-modal",
            },
            "rows": selected,
            "actions": [item for item in actions if item["id"] in selected_action_ids],
            "returned": len(selected),
            "limit": limit,
            "truncated": truncated,
            "next_cursor": next_cursor,
            "empty": {"state": "empty", "message": "No registered hives."} if not rows else None,
        }
    )
    return payload


def deck_payload(
    hive: str,
    queues: Mapping[str, Mapping[str, object]],
    roster: Mapping[str, object],
    *,
    limit: int,
    cursor: str | None,
    width: int = 120,
) -> dict:
    sections: dict[str, list[tuple[dict, list[dict]]]] = {
        "ready": [],
        "running": [],
        "needs-you": [],
    }
    agents_complete = roster.get("revision") != "unavailable"
    for item in queues["ready"].get("items", []):
        rendered = _work_row(item)
        if not agents_complete:
            _disable_mutations(
                *rendered,
                "herdr_session_unavailable",
                "the authoritative bh-supervisor session is unavailable",
            )
        sections["ready"].append(rendered)
    for agent in roster.get("agents", []):
        if not isinstance(agent, dict) or agent.get("hive") != hive:
            continue
        state = str((agent.get("lifecycle") or {}).get("state") or "unknown")
        rendered = _agent_row(agent)
        sections["needs-you" if state in {"blocked", "failed"} else "running"].append(rendered)
    for item in queues["blocked"].get("items", []):
        sections["needs-you"].append(_work_row(item))

    ordering = ["ready", "running", "needs-you"]
    if _layout_variant(width) == "narrow":
        ordering = ["needs-you", "running", "ready"]
    flat = [
        (section, row, action_rows)
        for section in ordering
        for row, action_rows in sections[section]
    ]
    revision = _revision(
        "deck-v1",
        hive,
        [queues[name].get("revision") for name in ("ready", "active", "blocked")],
        roster.get("revision"),
        width,
    )
    page_rows, truncated, next_cursor = _page(
        [
            {"section": section, "row": row, "actions": action_rows}
            for section, row, action_rows in flat
        ],
        view="deck",
        revision=revision,
        scope={"hive": hive, "width": width},
        limit=limit,
        cursor=cursor,
    )
    grouped: dict[str, list[dict]] = {name: [] for name in ordering}
    actions: dict[str, dict] = {}
    for item in page_rows:
        grouped[item["section"]].append(item["row"])
        for action in item["actions"]:
            actions[action["id"]] = action
    rendered_sections = [
        {
            "id": name,
            "label": name.replace("-", " ").upper(),
            "rows": grouped[name],
            "empty": {"state": "empty", "message": f"No {name.replace('-', ' ')} items."}
            if not grouped[name]
            else None,
        }
        for name in ordering
    ]
    warnings = []
    for source in (*queues.values(), roster):
        warnings.extend(source.get("warnings", []))
    coverage_states = [
        str(source.get("coverage", {}).get("state") or "unknown") for source in queues.values()
    ]
    coverage_state = (
        "complete" if set(coverage_states) == {"complete"} and agents_complete else "partial"
    )
    as_of = queues["ready"].get("generatedAt")
    payload = _base(
        "deck",
        revision,
        scope={"hive": hive},
        freshness={"state": "fresh" if as_of else "unknown", "as_of": as_of},
        coverage={
            "state": coverage_state,
            "sources": {
                **{name: queues[name].get("coverage") for name in queues},
                "agents": {"state": "complete" if agents_complete else "unavailable"},
            },
        },
        warnings=warnings,
    )
    payload.update(
        {
            "layout": layout_payload(hive, {"width": width, "height": 40})["layout"],
            "sections": rendered_sections,
            "actions": list(actions.values()),
            "returned": len(page_rows),
            "limit": limit,
            "truncated": truncated,
            "next_cursor": next_cursor,
            "key_hints": [
                {"key": "Enter", "action": "primary"},
                {"key": "A", "action": "actions"},
                {"key": "D", "action": "detail"},
                {"key": "R", "action": "refresh"},
            ],
        }
    )
    return payload


def bead_payload(detail: Mapping[str, object], roster: Mapping[str, object]) -> dict:
    item = detail["item"]
    hive = str(detail["hiveId"])
    bead = str(item["id"])
    row, _ = _work_row(item)
    generic_actions = [render_action(action) for action in item.get("advertisedActions", [])]
    if roster.get("revision") == "unavailable":
        _disable_mutations(
            row,
            generic_actions,
            "herdr_session_unavailable",
            "the authoritative bh-supervisor session is unavailable",
        )
    agents = [
        agent
        for agent in roster.get("agents", [])
        if isinstance(agent, dict) and agent.get("hive") == hive and agent.get("bead") == bead
    ]
    agent_rows = [_agent_row(agent)[0] for agent in agents]
    payload = _base(
        "bead",
        str(detail["revision"]),
        scope={"hive": hive, "bead": bead},
        freshness=_view_freshness(detail.get("freshness")),
        coverage=dict(detail.get("coverage") or {}),
        warnings=detail.get("warnings", []),
    )
    payload.update(
        {
            "surface": {"placement": "overlay", "fallback": "zoomed"},
            "row": row,
            "detail": {
                "title": _token(item.get("title"), 160),
                "description": _token(item.get("description"), 4000),
                "design": _token(item.get("design"), 4000),
                "acceptance_criteria": _token(item.get("acceptanceCriteria"), 4000),
                "notes": _token(item.get("notes"), 4000),
                "labels": [_token(value, 80) for value in item.get("labels", [])],
                "dependencies": item.get("dependencies", []),
                "dependents": item.get("dependents", []),
                "gates": item.get("gates", []),
                "claim": item.get("claim"),
                "agents": agent_rows,
            },
            "actions": generic_actions,
        }
    )
    return payload


def agent_payload(agent: Mapping[str, object], roster: Mapping[str, object]) -> dict:
    row, actions = _agent_row(agent)
    revision = str(agent.get("revision") or roster.get("revision") or _revision(agent))
    payload = _base(
        "agent",
        revision,
        scope={"hive": agent.get("hive"), "bead": agent.get("bead"), "target": agent.get("target")},
        freshness={"state": "fresh", "as_of": roster.get("observed_at")},
        coverage={"state": "complete", "sources": {"herdr": {"state": "complete"}}},
        warnings=roster.get("warnings", []),
    )
    payload.update(
        {
            "surface": {"placement": "popup", "lifecycle": "session-modal", "pane_id": None},
            "row": row,
            "detail": {
                "lifecycle": agent.get("lifecycle"),
                "worktree": agent.get("worktree"),
                "presentation": agent.get("presentation"),
                "ownership": agent.get("ownership"),
                "capabilities": agent.get("capabilities"),
            },
            "actions": actions,
        }
    )
    return payload


def stream_frames(
    deck: Mapping[str, object], *, hive: str, since: str | None, limit: int
) -> list[dict]:
    revision = str(deck["revision"])
    scope = {"hive": hive}
    cursor = _cursor_encode("stream", revision, scope, 0)
    resync = False
    reason = None
    if since:
        try:
            _cursor_offset(since, view="stream", revision=revision, scope=scope)
        except OperatorSourceError as exc:
            resync = True
            reason = exc.code
    frames = [
        {
            "schema_version": SCHEMA_VERSION,
            "command": "plugin herdr view stream",
            "type": "snapshot",
            "sequence": 0,
            "cursor": cursor,
            "resync_required": resync,
            "resync_reason": reason,
            "snapshot": deck,
        }
    ]
    observations = []
    for section in deck.get("sections", []):
        for row in section.get("rows", []):
            if row.get("entity", {}).get("kind") == "agent":
                observations.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "command": "plugin herdr view stream",
                        "type": "agent-observed",
                        "sequence": len(observations) + 1,
                        "cursor": cursor,
                        "entity": row["entity"],
                        "style": row.get("style"),
                        "badges": row.get("badges", []),
                    }
                )
    frames.extend(observations[: max(0, limit - 1)])
    return frames


def _constrain_launch(actions: list[dict[str, object]], preflight: Mapping[str, object]) -> None:
    """Further restrict a generically allowed launch using Herdr-local proof."""
    for action in actions:
        if action.get("id") == "work-item.launch" and action.get("availability") == "allowed":
            action.update(preflight)


@dataclass
class ViewBackend:
    cfg: dict
    sources: OperatorSources
    _roster: dict | None = None

    @classmethod
    def create(cls) -> ViewBackend:
        cfg = config.load()
        return cls(cfg=cfg, sources=OperatorSources(cfg=cfg, host_id=host.host_id()))

    def close(self) -> None:
        self.sources.close()

    def roster(self) -> dict:
        if self._roster is not None:
            return self._roster
        from . import herdr_plugin

        snapshot = herdr_plugin._session_snapshot()
        if snapshot is None:
            self._roster = {
                "revision": "unavailable",
                "observed_at": None,
                "agents": [],
                "warnings": ["Herdr bh-supervisor session is unavailable."],
            }
        else:
            self._roster = herdr_plugin._roster_payload(snapshot, self.cfg)
        return self._roster

    def launch_preflight(self, hive: str, entry: Mapping[str, object]) -> dict[str, object]:
        """Return Herdr-specific launch capability facts for a generic ready bead."""
        from . import herdr_plugin

        if not herdr_plugin._has_cli():
            return {
                "availability": "unavailable",
                "reasonCode": "herdr_cli_unavailable",
                "reason": "Herdr CLI is not available on this host.",
            }
        kinds = herdr_plugin.supported_kinds()
        if not kinds:
            return {
                "availability": "unavailable",
                "reasonCode": "herdr_kinds_unavailable",
                "reason": "Herdr did not report any supported agent kinds.",
            }
        configured = config.herdr_kind(self.cfg, entry)
        if configured is not None:
            kind = configured
        else:
            harness = config.harness_name(self.cfg, entry)
            kind = harness if harness in kinds else "claude" if "claude" in kinds else None
        if kind is None or kind not in kinds:
            return {
                "availability": "unavailable",
                "reasonCode": "herdr_kind_unavailable",
                "reason": "No configured or deterministic default Herdr agent kind is available.",
            }
        integrated, detail = herdr_plugin._integration_ready(kind)
        if not integrated:
            return {
                "availability": "unavailable",
                "reasonCode": "herdr_integration_unavailable",
                "reason": f"Herdr integration for {kind} is unavailable: {_token(detail, 160)}",
            }
        if self.roster().get("revision") == "unavailable":
            return {
                "availability": "unavailable",
                "reasonCode": "herdr_session_unavailable",
                "reason": "The authoritative bh-supervisor session is unavailable.",
            }
        from . import guard

        try:
            lease_state = guard.primary_state(hive, cfg=self.cfg, entry=entry)
        except Exception:  # noqa: BLE001 - preflight degrades instead of authorizing by guess
            return {
                "availability": "unavailable",
                "reasonCode": "host_lease_unavailable",
                "reason": "Current host lease state could not be proven.",
            }
        if lease_state is not None:
            _prefix, this_host, lease = lease_state
            if not lease.held_by(this_host):
                if not lease.is_expired():
                    return {
                        "availability": "forbidden",
                        "reasonCode": "active_foreign_host_lease",
                        "reason": "An active foreign host lease prevents launch.",
                    }
                return {
                    "availability": "confirmation-required",
                    "reasonCode": "expired_host_lease_adoption_required",
                    "reason": "Launch requires explicit non-forced adoption of the expired lease.",
                }
        return {
            "availability": "allowed",
            "reasonCode": None,
            "reason": f"Herdr launch preflight is available for {kind}.",
        }

    def picker(self, *, limit: int, cursor: str | None) -> dict:
        hives = self.sources.registered_hives()

        def summary(hive):
            try:
                state = self.sources.refresh_hive_state(hive)
            except OperatorSourceError as exc:
                return operator_contract.factory_hive_summary(
                    hive.entry, None, unavailable_reason=exc.code
                )
            return operator_contract.factory_hive_summary(hive.entry, state)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(hives)))) as pool:
            summaries = list(pool.map(summary, hives))
        return picker_payload(summaries, self.roster(), limit=limit, cursor=cursor)

    def hive_facts(self, hive_id: str) -> tuple[dict[str, dict], dict]:
        hive = self.sources.resolve_hive(hive_id)
        beads, runtime = self.sources.refresh_hive(hive)
        launch_preflight = self.launch_preflight(hive_id, hive.entry)
        ready_policy, ordering = operator_work_items.configured_ready_policy(
            cfg=self.cfg, entry=dict(hive.entry)
        )
        queues = {}
        for name in ("ready", "active", "blocked"):
            query = operator_work_items.WorkItemQuery(
                queue=name,
                limit=operator_work_items.MAX_LIMIT,
                ordering=ordering if name == "ready" else "beadhive.work-items/v1",
            )
            queues[name] = operator_work_items.complete_queue_payload(
                hive_id=hive_id,
                beads=beads,
                runtime=runtime,
                query=query,
                ready_policy=ready_policy if name == "ready" else None,
            )
            for item in queues[name]["items"]:
                item["advertisedActions"] = operator_actions.work_item_actions(
                    target=item["ref"],
                    readiness=str(item["readiness"]["state"]),
                    readiness_reason=str(item["readiness"]["reason"]),
                    partial=beads.partial,
                    revision=str(queues[name]["revision"]),
                    advertised_at=int(queues[name]["generatedAt"] or 0),
                )
                _constrain_launch(item["advertisedActions"], launch_preflight)
        return queues, self.roster()

    def deck(self, hive: str, *, limit: int, cursor: str | None, width: int = 120) -> dict:
        queues, roster = self.hive_facts(hive)
        return deck_payload(hive, queues, roster, limit=limit, cursor=cursor, width=width)

    def bead(self, hive_id: str, bead_id: str) -> dict:
        hive = self.sources.resolve_hive(hive_id)
        beads, runtime = self.sources.refresh_hive(hive)
        detail = operator_work_items.detail_payload(
            hive_id=hive_id, bead_id=bead_id, beads=beads, runtime=runtime
        )
        _constrain_launch(
            detail["item"]["advertisedActions"], self.launch_preflight(hive_id, hive.entry)
        )
        return bead_payload(detail, self.roster())

    def agent(self, target: str) -> dict:
        roster = self.roster()
        matches = [agent for agent in roster.get("agents", []) if agent.get("target") == target]
        if not matches:
            raise OperatorSourceError(
                "agent_not_found", "The exact Herdr target was not found.", status_code=404
            )
        if len(matches) != 1:
            raise OperatorSourceError(
                "agent_target_ambiguous", "The exact Herdr target is ambiguous.", status_code=409
            )
        return agent_payload(matches[0], roster)


def _limit(value: int) -> int:
    if not 1 <= value <= MAX_LIMIT:
        raise OperatorSourceError(
            "invalid_view_limit", "View limit must be from 1 through 200.", status_code=400
        )
    return value


def _emit_error(view: str, exc: OperatorSourceError) -> None:
    jsonout.emit(
        jsonout.envelope(
            f"plugin herdr view {view}",
            SCHEMA_VERSION,
            {
                "view": view,
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "status": exc.status_code,
                },
            },
        )
    )
    raise typer.Exit(1 if exc.status_code >= 500 else 2 if exc.status_code == 400 else 1)


def _backend() -> ViewBackend:
    return ViewBackend.create()


cli = typer.Typer(no_args_is_help=True, help="Herdr-specific, nearly-rendered view projections.")


@cli.command("picker")
def picker_cmd(
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    cursor: str | None = typer.Option(None, "--cursor"),
    as_json: bool = typer.Option(False, "--json", help="accepted for the machine JSON contract"),
) -> None:
    """Render a bounded global hive picker."""
    del as_json
    backend = _backend()
    try:
        jsonout.emit(backend.picker(limit=_limit(limit), cursor=cursor))
    except OperatorSourceError as exc:
        _emit_error("picker", exc)
    finally:
        backend.close()


@cli.command("deck")
def deck_cmd(
    hive: str = typer.Option(..., "--hive"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    cursor: str | None = typer.Option(None, "--cursor"),
    width: int = typer.Option(120, "--width", min=40, max=1000),
    as_json: bool = typer.Option(False, "--json", help="accepted for the machine JSON contract"),
) -> None:
    """Render Ready, Running, and Needs You sections for one exact hive."""
    del as_json
    backend = _backend()
    try:
        jsonout.emit(backend.deck(hive, limit=_limit(limit), cursor=cursor, width=width))
    except OperatorSourceError as exc:
        _emit_error("deck", exc)
    finally:
        backend.close()


@cli.command("bead")
def bead_cmd(
    hive: str = typer.Option(..., "--hive"),
    bead: str = typer.Option(..., "--bead"),
    as_json: bool = typer.Option(False, "--json", help="accepted for the machine JSON contract"),
) -> None:
    """Render the exact generic bead detail as a Herdr inspector."""
    del as_json
    backend = _backend()
    try:
        jsonout.emit(backend.bead(hive, bead))
    except OperatorSourceError as exc:
        _emit_error("bead", exc)
    finally:
        backend.close()


@cli.command("agent")
def agent_cmd(
    target: str = typer.Option(..., "--target"),
    as_json: bool = typer.Option(False, "--json", help="accepted for the machine JSON contract"),
) -> None:
    """Render one exact correlated Herdr agent inspector."""
    del as_json
    backend = _backend()
    try:
        jsonout.emit(backend.agent(target))
    except OperatorSourceError as exc:
        _emit_error("agent", exc)
    finally:
        backend.close()


@cli.command("layout")
def layout_cmd(
    hive: str | None = typer.Option(None, "--hive"),
    context_json: str = typer.Option("", "--context-json"),
    as_json: bool = typer.Option(False, "--json", help="accepted for the machine JSON contract"),
) -> None:
    """Return deterministic Herdr popup, tab, split, and responsive layout intent."""
    del as_json
    try:
        if len(context_json.encode()) > 16_384:
            raise OperatorSourceError(
                "invalid_layout_context", "Layout context exceeds 16384 bytes.", status_code=400
            )
        context = json.loads(context_json) if context_json else {}
        if not isinstance(context, dict):
            raise OperatorSourceError(
                "invalid_layout_context", "Layout context must be a JSON object.", status_code=400
            )
        jsonout.emit(layout_payload(hive, context))
    except json.JSONDecodeError:
        _emit_error(
            "layout",
            OperatorSourceError(
                "invalid_layout_context", "Layout context is not valid JSON.", status_code=400
            ),
        )
    except OperatorSourceError as exc:
        _emit_error("layout", exc)


@cli.command("stream")
def stream_cmd(
    hive: str = typer.Option(..., "--hive"),
    since: str | None = typer.Option(None, "--since"),
    limit: int = typer.Option(STREAM_DEFAULT_LIMIT, "--limit"),
    width: int = typer.Option(120, "--width", min=40, max=1000),
) -> None:
    """Emit a bounded snapshot-first NDJSON projection stream."""
    backend = _backend()
    try:
        stream_limit = _limit(limit)
        deck = backend.deck(hive, limit=MAX_LIMIT, cursor=None, width=width)
        for frame in stream_frames(deck, hive=hive, since=since, limit=stream_limit):
            typer.echo(json.dumps(frame, separators=(",", ":"), default=str))
    except OperatorSourceError as exc:
        typer.echo(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "command": "plugin herdr view stream",
                    "type": "error",
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "retryable": exc.retryable,
                    },
                },
                separators=(",", ":"),
            )
        )
        raise typer.Exit(1) from exc
    finally:
        backend.close()
