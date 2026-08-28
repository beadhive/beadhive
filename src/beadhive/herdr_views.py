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
    engine,
    hive_identity,
    hive_sync,
    host,
    jsonout,
    operator_actions,
    operator_contract,
    operator_work_items,
    registry,
    worktree,
)
from .operator_sources import OperatorSourceError, OperatorSources

SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
STREAM_DEFAULT_LIMIT = 50
PRESENTATION_TTL_MS = 15_000
PRESENTATION_PROTOCOL = "bh.plugin.herdr.presentation/v1"
PRESENTATION_SOURCE = "bh.plugin.herdr.presentation.v1"
PRESENTATION_TOKEN_LIMIT = 80
CREW_MAX_DEPTH = 12
CREW_MAX_DIAGNOSTICS = 256
CREW_TTL_MS = 15_000
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


def _locator(value: object) -> str | None:
    """Retain an exact Herdr locator only when it is bounded and control-free."""

    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    return value


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
    generated_at: int | None = None,
) -> dict[str, object]:
    return jsonout.envelope(
        f"plugin herdr view {view}",
        SCHEMA_VERSION,
        {
            "view": view,
            "revision": revision,
            "generated_at": _now_ms() if generated_at is None else generated_at,
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
    rendered_context = {
        "invoking_session": _token(context.get("invoking_session"), 80) or None,
        "workspace_id": _token(context.get("workspace_id"), 80) or None,
        "tab_id": _token(context.get("tab_id"), 80) or None,
        "pane_id": _token(context.get("pane_id"), 80) or None,
    }
    workspace_companion = hive is not None and bool(
        rendered_context["workspace_id"] or rendered_context["pane_id"]
    )
    if workspace_companion:
        deck_surface = {
            "placement": "split",
            "direction": "down" if variant == "narrow" else "right",
            "target_role": "agents",
            "lifecycle": "ordinary-pane",
            "close_behavior": "close",
            "reopen_behavior": "reopen-split",
            "focus": False,
            **deck,
        }
    else:
        # Additive v1 compatibility: callers that only supplied a viewport continue to receive
        # the original dedicated Deck tab.  A workspace or exact pane identity opts into the
        # companion-split contract without requiring a schema-version break.
        deck_surface = {
            "placement": "tab",
            "direction": None,
            "target_role": "board",
            "lifecycle": "ordinary-tab",
            "close_behavior": "close",
            "reopen_behavior": "reopen-tab",
            "focus": False,
            **deck,
        }
    revision = _revision("layout-v1", hive, width, height, rendered_context, deck_surface)
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
            "deck": deck_surface,
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
    payload["context"] = rendered_context
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
        ownership = str((agent.get("ownership") or {}).get("state") or "unknown")
        if ownership != "owned":
            counts["needs_attention"] += 1
        elif state in {"idle", "working"}:
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
    queue_scopes: Mapping[str, Mapping[str, object]] | None = None,
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
    correlated_beads = {
        str(agent.get("bead"))
        for agent in roster.get("agents", [])
        if isinstance(agent, dict) and agent.get("hive") == hive and agent.get("bead")
    }
    for item in queues["active"].get("items", []):
        if str(item.get("id")) not in correlated_beads:
            sections["running"].append(_work_row(item))
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
    queue_contract = {
        name: {
            "schema_version": queues[name].get("schemaVersion"),
            "query": dict((queue_scopes or {}).get(name) or {"queue": name}),
        }
        for name in ("ready", "active", "blocked")
    }
    revision = _revision(
        "deck-v1",
        {"schema_version": SCHEMA_VERSION, "queues": queue_contract},
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
                "facts": agent.get("facts"),
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


def _source_coverage(value: Mapping[str, object] | None) -> str:
    coverage = value.get("coverage") if isinstance(value, Mapping) else None
    if isinstance(coverage, Mapping):
        state = str(coverage.get("state") or "unknown")
        if state in {"complete", "partial", "stale", "unavailable"}:
            return state
    return "unknown"


def _workspace_correlation(
    snapshot: Mapping[str, object] | None, hive: str
) -> tuple[dict[str, object], dict[str, str | None]]:
    if snapshot is None:
        return (
            {
                "state": "unavailable",
                "reason_code": "supervisor_unavailable",
                "reason": "the authoritative bh-supervisor snapshot is unavailable",
            },
            {"session": _SESSION, "workspace_id": None},
        )
    matches = []
    workspaces = snapshot.get("workspaces")
    if isinstance(workspaces, list):
        for item in workspaces:
            if not isinstance(item, Mapping) or item.get("label") != f"bh:{hive}":
                continue
            workspace_id = _locator(item.get("workspace_id") or item.get("id"))
            if workspace_id is not None:
                matches.append(workspace_id)
    if len(matches) == 1:
        return (
            {
                "state": "exact",
                "reason_code": "exact_workspace",
                "reason": "one exact canonical hive workspace is live",
            },
            {"session": _SESSION, "workspace_id": matches[0]},
        )
    if not matches:
        return (
            {
                "state": "missing",
                "reason_code": "workspace_missing",
                "reason": "no exact canonical hive workspace is live",
            },
            {"session": _SESSION, "workspace_id": None},
        )
    return (
        {
            "state": "ambiguous",
            "reason_code": "workspace_ambiguous",
            "reason": "multiple canonical hive workspaces are live",
        },
        {"session": _SESSION, "workspace_id": None},
    )


def _presentation_counts(
    hive: str,
    queues: Mapping[str, Mapping[str, object]],
    roster: Mapping[str, object],
) -> dict[str, int | None]:
    queue_complete = all(
        _source_coverage(queues.get(name)) == "complete" for name in ("ready", "active", "blocked")
    )
    agents_complete = roster.get("revision") != "unavailable"
    if not queue_complete:
        ready: int | None = None
        running: int | None = None
        needs_you: int | None = None
    else:
        ready = len(queues.get("ready", {}).get("items", []))
        blocked = len(queues.get("blocked", {}).get("items", []))
        active_ids = {
            str(item.get("id"))
            for item in queues.get("active", {}).get("items", [])
            if isinstance(item, Mapping) and item.get("id")
        }
        seen_active: set[str] = set()
        agent_running = 0
        agent_attention = 0
        for agent in roster.get("agents", []):
            if not isinstance(agent, Mapping) or agent.get("hive") != hive:
                continue
            bead = str(agent.get("bead") or "")
            if bead in active_ids:
                seen_active.add(bead)
            ownership = (
                agent.get("ownership") if isinstance(agent.get("ownership"), Mapping) else {}
            )
            lifecycle = (
                agent.get("lifecycle") if isinstance(agent.get("lifecycle"), Mapping) else {}
            )
            state = str(lifecycle.get("state") or "unknown")
            if ownership.get("state") != "owned" or state in {"blocked", "failed"}:
                agent_attention += 1
            else:
                agent_running += 1
        running = len(active_ids - seen_active) + agent_running if agents_complete else None
        needs_you = blocked + agent_attention if agents_complete else None
    return {"ready": ready, "running": running, "needs_you": needs_you}


def _count_token(value: int | None) -> str:
    return "?" if value is None else str(value)


def _sidebar_count(value: object, unavailable: str) -> str:
    return str(value) if type(value) is int and value >= 0 else unavailable


def _dolt_tokens(comparison: Mapping[str, object] | None) -> tuple[dict[str, str], str]:
    coverage = comparison.get("coverage") if isinstance(comparison, Mapping) else None
    coverage_state = (
        str(coverage.get("state") or "unknown") if isinstance(coverage, Mapping) else "unavailable"
    )
    ahead = comparison.get("ahead") if isinstance(comparison, Mapping) else None
    behind = comparison.get("behind") if isinstance(comparison, Mapping) else None
    counts_known = (
        coverage_state == "complete"
        and type(ahead) is int
        and ahead >= 0
        and type(behind) is int
        and behind >= 0
    )
    return (
        {
            "bh_dolt_ahead": f"dolt ↑{ahead if counts_known else '-'}",
            "bh_dolt_behind": f"↓{behind if counts_known else '-'}",
        },
        coverage_state
        if coverage_state in {"complete", "partial", "stale", "unavailable"}
        else "unknown",
    )


def _crew_action(agent: Mapping[str, object], action_id: str) -> Mapping[str, object] | None:
    actions = agent.get("advertised_actions")
    if not isinstance(actions, list):
        return None
    matches = [
        action
        for action in actions
        if isinstance(action, Mapping) and action.get("id") == action_id
    ]
    return matches[0] if len(matches) == 1 else None


def _crew_locator(
    agent: Mapping[str, object], workspace_id: str | None
) -> tuple[dict[str, str] | None, str | None]:
    presentation = (
        agent.get("presentation") if isinstance(agent.get("presentation"), Mapping) else {}
    )
    ownership = agent.get("ownership") if isinstance(agent.get("ownership"), Mapping) else {}
    target = _locator(agent.get("target"))
    session = _locator(presentation.get("session"))
    workspace = _locator(presentation.get("workspace"))
    tab = _locator(presentation.get("tab"))
    pane = _locator(presentation.get("pane"))
    if session != _SESSION:
        return None, "locator-session-mismatch"
    if ownership.get("state") != "owned":
        return None, "ownership-not-exact"
    if not workspace_id or workspace != workspace_id:
        return None, "locator-workspace-mismatch"
    if not target or not tab or not pane:
        return None, "locator-incomplete"
    return (
        {
            "session": _SESSION,
            "workspace_id": workspace,
            "tab_id": tab,
            "pane_id": pane,
            "target": target,
        },
        None,
    )


def _crew_relation(agent: Mapping[str, object]) -> tuple[str, str | None, str | None]:
    facts = agent.get("facts") if isinstance(agent.get("facts"), Mapping) else {}
    parent = facts.get("parent") if isinstance(facts.get("parent"), Mapping) else {}
    relation = str(parent.get("relation") or "unknown")
    parent_target = _locator(parent.get("target"))
    role = str(facts.get("role") or "unknown")
    if relation == "root":
        if role == "dispatcher":
            return "root-dispatcher", None, None
        if role == "developer":
            return "direct-agent", None, None
        return "orphan", None, "role-unknown"
    if relation == "direct" and parent_target:
        if role not in {"developer", "dispatcher"}:
            return "orphan", None, "role-unknown"
        return (
            "direct-child-dispatcher" if role == "dispatcher" else "direct-child",
            parent_target,
            None,
        )
    if relation == "cycle":
        return "orphan", None, "topology-cycle"
    return "orphan", None, "missing-parent"


def crew_payload(
    hive: str,
    roster: Mapping[str, object],
    snapshot: Mapping[str, object] | None,
    *,
    limit: int,
    cursor: str | None = None,
    generated_at: int | None = None,
) -> dict[str, object]:
    """Project one bounded, read-only Crew forest from authoritative roster facts.

    Every locator names an existing real terminal.  Crew and Child Stage are desired plugin TUI
    surfaces only; this projection never creates, mirrors, reparents, focuses, or closes a pane.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise OperatorSourceError(
            "invalid_view_limit", "View limit must be from 1 through 200.", status_code=400
        )
    if cursor is not None:
        raise OperatorSourceError(
            "crew_cursor_unsupported",
            "Crew topology is an atomic bounded forest; refresh it without a cursor.",
            status_code=409,
        )
    if not all(_safe_identity(part) for part in hive.split("/")):
        raise OperatorSourceError(
            "invalid_hive_identity", "Hive identity is not canonical.", status_code=400
        )
    now = _now_ms() if generated_at is None else generated_at
    workspace_correlation, workspace_locator = _workspace_correlation(snapshot, hive)
    workspace_id = _locator(workspace_locator.get("workspace_id"))
    roster_revision = _locator(roster.get("revision"))
    roster_session = _locator(roster.get("session"))
    authoritative = roster.get("authoritative_session") is True
    diagnostics: list[dict[str, object]] = []

    def diagnose(code: str, *, target: str | None = None, detail: str) -> None:
        if len(diagnostics) < CREW_MAX_DIAGNOSTICS:
            diagnostics.append({"code": code, "target": target, "detail": _token(detail, 240)})

    if roster_revision is None or roster_revision == "unavailable":
        diagnose("agents-unavailable", detail="The authoritative Herdr roster is unavailable.")
    if roster_session != _SESSION or not authoritative:
        diagnose(
            "roster-session-mismatch",
            detail="The roster is not authoritative for the bh-supervisor session.",
        )
    if workspace_correlation["state"] != "exact" or workspace_id is None:
        diagnose(
            "workspace-not-exact",
            detail=str(workspace_correlation.get("reason") or "workspace correlation failed"),
        )

    candidates: dict[str, list[Mapping[str, object]]] = {}
    for value in roster.get("agents", []):
        if not isinstance(value, Mapping) or value.get("hive") != hive:
            continue
        target = _locator(value.get("target"))
        if target is None:
            diagnose("invalid-target", detail="A roster row has no bounded exact target.")
            continue
        candidates.setdefault(target, []).append(value)

    agents: dict[str, Mapping[str, object]] = {}
    duplicate_targets: set[str] = set()
    for target in sorted(candidates):
        rows = candidates[target]
        if len(rows) != 1:
            duplicate_targets.add(target)
            diagnose(
                "duplicate-target",
                target=target,
                detail="The target occurs more than once; one non-navigable row is retained.",
            )
        agents[target] = sorted(
            rows,
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), default=str),
        )[0]

    relations: dict[str, str] = {}
    parents: dict[str, str | None] = {}
    for target, agent in agents.items():
        relation, parent, issue = _crew_relation(agent)
        if target in duplicate_targets:
            relation, parent, issue = "orphan", None, "duplicate-target"
        elif parent is not None and parent not in agents:
            relation, parent, issue = "orphan", None, "missing-parent"
        relations[target] = relation
        parents[target] = parent
        if issue:
            diagnose(issue, target=target, detail="The authoritative parent relation is partial.")

    # Independently refuse cycles even when an upstream record incorrectly labels each edge direct.
    cycle_targets: set[str] = set()
    for target in sorted(agents):
        path: list[str] = []
        cursor_target: str | None = target
        while cursor_target is not None and cursor_target in agents:
            if cursor_target in path:
                cycle_targets.update(path[path.index(cursor_target) :])
                break
            path.append(cursor_target)
            cursor_target = parents.get(cursor_target)
    for target in sorted(cycle_targets):
        relations[target] = "orphan"
        parents[target] = None
        diagnose("topology-cycle", target=target, detail="The target participates in a cycle.")

    children: dict[str, list[str]] = {target: [] for target in agents}
    for target, parent in parents.items():
        if parent is not None:
            children[parent].append(target)
    for values in children.values():
        values.sort()

    emitted = 0
    truncated = False
    emitted_targets: set[str] = set()
    exact_locator_targets: set[str] = set()

    def descendant_count(target: str) -> int:
        return sum(1 + descendant_count(child) for child in children[target])

    def node(target: str, *, depth: int) -> dict[str, object] | None:
        nonlocal emitted, truncated
        if emitted >= limit:
            truncated = True
            diagnose(
                "topology-item-limit", target=target, detail="The Crew node limit was reached."
            )
            return None
        if depth > CREW_MAX_DEPTH:
            truncated = True
            diagnose(
                "topology-depth-limit", target=target, detail="The Crew depth limit was reached."
            )
            return None
        emitted += 1
        emitted_targets.add(target)
        agent = agents[target]
        facts = agent.get("facts") if isinstance(agent.get("facts"), Mapping) else {}
        work = facts.get("work") if isinstance(facts.get("work"), Mapping) else {}
        topology = facts.get("topology") if isinstance(facts.get("topology"), Mapping) else {}
        locator, locator_issue = _crew_locator(agent, workspace_id)
        if target in duplicate_targets:
            locator, locator_issue = None, "duplicate-target"
        if locator_issue:
            diagnose(locator_issue, target=target, detail="The exact terminal locator is refused.")
        topology_coverage = str(topology.get("coverage") or "unavailable")
        if topology_coverage != "complete":
            diagnose(
                "topology-partial",
                target=target,
                detail="The node's authoritative topology coverage is incomplete.",
            )
        declared_direct = topology.get("direct_active_children")
        declared_total = topology.get("total_active_descendants")
        if topology_coverage == "complete" and (
            declared_direct != len(children[target]) or declared_total != descendant_count(target)
        ):
            diagnose(
                "topology-count-mismatch",
                target=target,
                detail="The declared child counts do not match the exact parent forest.",
            )
        rendered_children = [
            rendered
            for child in children[target]
            if (rendered := node(child, depth=depth + 1)) is not None
        ]
        result: dict[str, object] = {
            "target": target,
            "relation": relations[target],
            "bead": _locator(agent.get("bead")),
            "harness": _token(facts.get("harness") or "unknown", 80),
            "role": _token(facts.get("role") or "unknown", 40),
            "work": {
                "operation": _token(work.get("operation") or "unknown", 80),
                "phase": _token(work.get("phase") or "unknown", 40),
            },
            "topology": {
                "coverage": topology_coverage,
                "direct_active_children": (
                    topology.get("direct_active_children")
                    if isinstance(topology.get("direct_active_children"), int)
                    else None
                ),
                "total_active_descendants": (
                    topology.get("total_active_descendants")
                    if isinstance(topology.get("total_active_descendants"), int)
                    else None
                ),
            },
            "safe_actions": [],
            "children": rendered_children,
        }
        if locator is not None:
            exact_locator_targets.add(target)
            result["locator"] = locator
            result["safe_actions"] = ["focus"]
            attach = _crew_action(agent, "agent.attach")
            if attach is not None and attach.get("availability") in {
                "allowed",
                "confirmation-required",
            }:
                result["safe_actions"].append("attach")
        role = str(facts.get("role") or "unknown")
        if role == "dispatcher" and rendered_children and locator is not None:
            result["stage"] = {"desired": True, "placement": "right", "role": "child-stage"}
        elif relations[target] == "direct-agent":
            result["layout"] = {"role": "direct-agent"}
        elif relations[target] == "direct-child-dispatcher":
            result["layout"] = {"role": "dispatcher"}

        reap = _crew_action(agent, "agent.reap")
        preconditions = (
            reap.get("preconditions")
            if isinstance(reap, Mapping) and isinstance(reap.get("preconditions"), Mapping)
            else {}
        )
        if (
            relations[target] in {"direct-child", "direct-child-dispatcher"}
            and locator is not None
            and not diagnostics
            and work.get("terminal_phase") is True
            and work.get("phase") == "terminal"
            and topology_coverage == "complete"
            and reap is not None
            and reap.get("availability") == "confirmation-required"
            and preconditions.get("mustMatch") is True
            and preconditions.get("sourceRevision") == roster_revision
        ):
            result["safe_removal"] = {
                "availability": "confirmation-required",
                "reason_code": "operator-confirmation-required",
                "source_revision": roster_revision,
            }
        return result

    root_order = {"root-dispatcher": 0, "direct-agent": 1, "orphan": 2}
    roots = [
        rendered
        for target in sorted(agents, key=lambda value: (root_order.get(relations[value], 9), value))
        if parents[target] is None and (rendered := node(target, depth=0)) is not None
    ]
    if len(roots) == 0 and agents:
        diagnose("topology-unrooted", detail="No safe Crew root could be established.")

    desired_tabs: list[dict[str, object]] = [{"role": "crew", "label": "Crew"}]
    for target in sorted(agents):
        if target not in emitted_targets or target not in exact_locator_targets:
            continue
        if relations[target] in {"root-dispatcher", "direct-child-dispatcher"}:
            desired_tabs.append(
                {
                    "role": "dispatcher",
                    "target": target,
                    "label": "Nested Dispatcher" if parents[target] else "Dispatcher",
                }
            )
    for target in sorted(agents):
        if (
            target in emitted_targets
            and target in exact_locator_targets
            and relations[target] == "direct-agent"
        ):
            desired_tabs.append({"role": "direct-agent", "target": target, "label": "Direct Agent"})

    if truncated:
        diagnose("projection-truncated", detail="The bounded Crew forest is incomplete.")
    if diagnostics:
        pending = list(roots)
        while pending:
            current = pending.pop()
            current.pop("safe_removal", None)
            current.pop("stage", None)
            current["safe_actions"] = []
            pending.extend(
                child for child in current.get("children", []) if isinstance(child, dict)
            )
    coverage_state = "complete" if not diagnostics else "partial"
    freshness_state = (
        "fresh" if roster_revision and roster_revision != "unavailable" else "unavailable"
    )
    diagnostic_codes = {str(item.get("code") or "") for item in diagnostics}
    if freshness_state == "unavailable":
        agents_source_state = "unavailable"
    elif diagnostic_codes - {"workspace-not-exact"}:
        agents_source_state = "partial"
    else:
        agents_source_state = "complete"
    source_revision = roster_revision or _revision("crew-v1-unavailable", hive, now)
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "plugin herdr view crew",
        "view": "crew",
        "revision": source_revision,
        "source_revision": source_revision,
        "generated_at": now,
        "hive_id": hive,
        "scope": {"hive": hive, "session": _SESSION},
        "freshness": {
            "state": freshness_state,
            "as_of": now if freshness_state == "fresh" else None,
            "expires_at": now + CREW_TTL_MS if freshness_state == "fresh" else None,
        },
        "coverage": {
            "state": coverage_state,
            "sources": {
                "agents": {"state": agents_source_state},
                "workspace": {
                    "state": "complete" if workspace_correlation["state"] == "exact" else "partial"
                },
            },
        },
        "workspace": {
            "locator": {"session": _SESSION, "workspace_id": workspace_id},
            "role": "hive",
            "desired_tabs": desired_tabs,
        },
        "roots": roots,
        "returned": emitted,
        "limit": limit,
        "truncated": truncated,
        "next_cursor": None,
        "diagnostics": diagnostics,
        "warnings": [
            _token(value, 240) for value in list(roster.get("warnings", []))[:MAX_LIMIT] if value
        ],
    }


def presentation_payload(
    hive: str,
    identity: Mapping[str, object],
    inventory: Mapping[str, object],
    queues: Mapping[str, Mapping[str, object]],
    roster: Mapping[str, object],
    snapshot: Mapping[str, object] | None,
    *,
    dolt: Mapping[str, object] | None = None,
    generated_at: int | None = None,
    sequence: int | None = None,
    ttl_ms: int = PRESENTATION_TTL_MS,
) -> dict[str, object]:
    """Return direct, display-only Herdr metadata patches for one exact hive.

    The projection never calls ``report-metadata``.  It only supplies exact live locators and
    bounded values that a Herdr plugin may submit.  An absent, ambiguous, or stale correlation
    is retained as evidence with a null report patch instead of being guessed.
    """

    if not 1 <= ttl_ms <= 86_400_000:
        raise OperatorSourceError(
            "invalid_presentation_ttl",
            "Presentation TTL must be from 1 through 86400000 milliseconds.",
            status_code=400,
        )
    now = _now_ms() if generated_at is None else generated_at
    seq = time.time_ns() if sequence is None else sequence
    if not 0 <= seq < 2**64:
        raise OperatorSourceError(
            "invalid_presentation_sequence",
            "Presentation sequence must be an unsigned 64-bit integer.",
            status_code=400,
        )

    workspace_correlation, workspace_locator = _workspace_correlation(snapshot, hive)
    counts = _presentation_counts(hive, queues, roster)
    inventory_items = {
        str(item.get("bead_id")): item
        for item in inventory.get("worktrees", [])
        if isinstance(item, Mapping)
        and item.get("hive_id") == hive
        and item.get("bead_id") is not None
    }
    worktree_total = inventory.get("total") if isinstance(inventory.get("total"), int) else None
    dolt_tokens, dolt_source_state = _dolt_tokens(dolt)

    agent_source_state = "unavailable" if roster.get("revision") == "unavailable" else "complete"
    if agent_source_state == "complete":
        for agent in roster.get("agents", []):
            if not isinstance(agent, Mapping) or agent.get("hive") != hive:
                continue
            facts = agent.get("facts") if isinstance(agent.get("facts"), Mapping) else {}
            topology = facts.get("topology") if isinstance(facts.get("topology"), Mapping) else {}
            if topology.get("coverage") != "complete":
                agent_source_state = "partial"
                break
    identity_complete = (
        identity.get("canonical_id") == hive
        and "/".join(
            str(identity.get(part) or "") for part in ("provider", "organization", "repository")
        )
        == hive
        and bool(identity.get("prefix"))
        and bool(identity.get("organization"))
        and bool(identity.get("repository"))
        and identity.get("affiliation") in {"maintainer", "contributor"}
    )
    source_states = {
        "identity": "complete" if identity_complete else "partial",
        "work_items": (
            "complete"
            if all(
                _source_coverage(queues.get(name)) == "complete"
                for name in ("ready", "active", "blocked")
            )
            else "partial"
        ),
        "worktrees": _source_coverage(inventory),
        "dolt": dolt_source_state,
        "agents": agent_source_state,
        "workspace": "complete" if workspace_correlation["state"] == "exact" else "partial",
    }
    overall_coverage = "complete" if set(source_states.values()) == {"complete"} else "partial"
    revision = _revision(
        "presentation-v1",
        hive,
        identity,
        inventory.get("source_revision"),
        dolt.get("sourceRevision") if isinstance(dolt, Mapping) else None,
        [queues.get(name, {}).get("revision") for name in ("ready", "active", "blocked")],
        roster.get("revision"),
        workspace_locator,
        counts,
    )
    hive_display = _token(
        f"{identity.get('organization')}/{identity.get('repository')}"
        if identity_complete
        else "hive -",
        PRESENTATION_TOKEN_LIMIT,
    )
    hive_prefix = _token(identity.get("prefix") if identity_complete else "-", 24)
    workspace_tokens = {
        "bh_space_title": _token(
            f"[{hive_prefix}] {hive_display}" if identity_complete else "hive -",
            PRESENTATION_TOKEN_LIMIT,
        ),
        "bh_affiliation": _token(
            identity.get("affiliation") if identity_complete else "role -", 24
        ),
        "bh_worktrees": _sidebar_count(worktree_total, "worktrees -"),
        **dolt_tokens,
    }
    workspace_report = None
    if workspace_correlation["state"] == "exact":
        workspace_report = {
            "source": PRESENTATION_SOURCE,
            "seq": seq,
            "ttl_ms": ttl_ms,
            "clearTokens": [
                "bh_hive",
                "bh_hive_id",
                "bh_ready",
                "bh_running",
                "bh_needs_you",
                "bh_coverage",
                "bh_revision",
            ],
            "tokens": workspace_tokens,
        }

    panes: list[dict[str, object]] = []
    correlated_beads: set[str] = set()
    for agent in roster.get("agents", []):
        if not isinstance(agent, Mapping) or agent.get("hive") != hive:
            continue
        presentation = (
            agent.get("presentation") if isinstance(agent.get("presentation"), Mapping) else {}
        )
        ownership = agent.get("ownership") if isinstance(agent.get("ownership"), Mapping) else {}
        facts = agent.get("facts") if isinstance(agent.get("facts"), Mapping) else {}
        work = facts.get("work") if isinstance(facts.get("work"), Mapping) else {}
        topology = facts.get("topology") if isinstance(facts.get("topology"), Mapping) else {}
        bead = _locator(agent.get("bead"))
        target = _locator(agent.get("target"))
        workspace_id = _locator(presentation.get("workspace"))
        tab_id = _locator(presentation.get("tab"))
        pane_id = _locator(presentation.get("pane"))
        if bead:
            correlated_beads.add(bead)
        locator_complete = bool(workspace_id and tab_id and pane_id)
        exact = (
            ownership.get("state") == "owned"
            and locator_complete
            and workspace_id == workspace_locator.get("workspace_id")
        )
        if exact:
            correlation_state = "exact"
            reason_code = "exact_agent_pane"
            reason = "owned agent and exact workspace, tab, and pane locators agree"
        elif workspace_correlation["state"] == "unavailable":
            correlation_state = "stale"
            reason_code = "supervisor_unavailable"
            reason = "the authoritative bh-supervisor snapshot is unavailable"
        elif locator_complete and workspace_id != workspace_locator.get("workspace_id"):
            correlation_state = "stale"
            reason_code = "locator_mismatch"
            reason = "the agent pane belongs to another hive workspace"
        elif ownership.get("state") == "stale":
            correlation_state = "stale"
            reason_code = "ownership_stale"
            reason = _token(ownership.get("reason") or "agent correlation is stale", 200)
        else:
            correlation_state = "missing"
            reason_code = "locator_missing"
            reason = _token(ownership.get("reason") or "agent correlation is incomplete", 200)
        worktree_item = inventory_items.get(bead or "")
        role = _token(facts.get("role") or "unknown", 24)
        harness = _token(facts.get("harness") or "unknown", 24)
        pane_tokens = {
            "bh_agent_title": _token(f"[{harness}] bh-{role}", PRESENTATION_TOKEN_LIMIT),
            "bh_bead": _token(bead or "unknown", PRESENTATION_TOKEN_LIMIT),
            "bh_phase": _token(work.get("phase") or "unknown", 24),
            "bh_operation": _token(work.get("operation") or "unknown", 32),
        }
        if role == "dispatcher":
            pane_tokens["bh_managed_agents"] = _sidebar_count(
                topology.get("direct_active_children")
                if topology.get("coverage") == "complete"
                and isinstance(topology.get("direct_active_children"), int)
                else None,
                "-",
            )
        report = None
        if exact:
            report = {
                "source": PRESENTATION_SOURCE,
                "seq": seq,
                "ttl_ms": ttl_ms,
                "clearTokens": [
                    "bh_hive_id",
                    "bh_role",
                    "bh_parent",
                    "bh_children",
                    "bh_state",
                    "bh_attention",
                    "bh_correlation",
                    "bh_worktree",
                    "bh_coverage",
                    *([] if role == "dispatcher" else ["bh_managed_agents"]),
                ],
                "tokens": pane_tokens,
            }
        panes.append(
            {
                "locator": {
                    "session": _SESSION,
                    "workspace_id": workspace_id,
                    "tab_id": tab_id,
                    "pane_id": pane_id,
                },
                "correlation": {
                    "state": correlation_state,
                    "reason_code": reason_code,
                    "reason": reason,
                    "hive_id": hive,
                    "bead_id": bead,
                    "target": target,
                    "worktree_id": (
                        worktree_item.get("worktree_id")
                        if isinstance(worktree_item, Mapping)
                        else None
                    ),
                },
                "report": report,
            }
        )

    for item in queues.get("active", {}).get("items", []):
        if not isinstance(item, Mapping):
            continue
        bead = _locator(item.get("id"))
        if bead is None or bead in correlated_beads:
            continue
        panes.append(
            {
                "locator": {
                    "session": _SESSION,
                    "workspace_id": workspace_locator.get("workspace_id"),
                    "tab_id": None,
                    "pane_id": None,
                },
                "correlation": {
                    "state": "missing",
                    "reason_code": "agent_correlation_missing",
                    "reason": "active Beadhive work has no correlated Herdr agent pane",
                    "hive_id": hive,
                    "bead_id": bead,
                    "target": None,
                    "worktree_id": (
                        inventory_items[bead].get("worktree_id")
                        if bead in inventory_items
                        else None
                    ),
                },
                "report": None,
            }
        )
    panes.sort(
        key=lambda item: (
            str(item["correlation"].get("bead_id") or ""),
            str(item["correlation"].get("target") or ""),
        )
    )
    truncated = len(panes) > MAX_LIMIT
    panes = panes[:MAX_LIMIT]
    if truncated:
        overall_coverage = "partial"
        source_states["agents"] = "partial"
        if workspace_report is not None:
            workspace_report["tokens"]["bh_coverage"] = "partial"

    warnings = []
    for source in (*queues.values(), inventory, roster):
        for warning in source.get("warnings", []):
            if isinstance(warning, Mapping):
                warnings.append(str(warning.get("code") or warning.get("detail") or "warning"))
            else:
                warnings.append(str(warning))
    freshness_state = (
        "stale"
        if "stale" in source_states.values()
        else "fresh"
        if overall_coverage == "complete"
        else "partial"
    )
    payload = _base(
        "presentation",
        revision,
        scope={"hive": hive},
        freshness={
            "state": freshness_state,
            "as_of": now,
            "expires_at": now + ttl_ms,
            "ttl_ms": ttl_ms,
        },
        coverage={
            "state": overall_coverage,
            "sources": {name: {"state": state} for name, state in source_states.items()},
        },
        warnings=warnings,
        generated_at=now,
    )
    payload.update(
        {
            "source_revision": revision,
            "policy": {
                "source": PRESENTATION_PROTOCOL,
                "sequence": seq,
                "sequence_scope": "monotonic per source and exact resource locator",
                "revision": revision,
                "ttl_ms": ttl_ms,
                "expiry_behavior": "remove this source's display-only metadata",
                "values_only": True,
                "theme_owner": "herdr-host",
                "lifecycle_authority": False,
                "report_agent_authority": False,
                "preserves_host_rows": [
                    "status_icon",
                    "git_branch",
                    "git_ahead_behind",
                ],
            },
            "summary": counts,
            "workspace": {
                "locator": workspace_locator,
                "correlation": {**workspace_correlation, "hive_id": hive},
                "report": workspace_report,
            },
            "panes": panes,
            "returned": len(panes),
            "limit": MAX_LIMIT,
            "truncated": truncated,
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
    _snapshot: dict | None = None
    _snapshot_checked: bool = False

    @classmethod
    def create(cls) -> ViewBackend:
        cfg = config.load()
        return cls(cfg=cfg, sources=OperatorSources(cfg=cfg, host_id=host.host_id()))

    def close(self) -> None:
        self.sources.close()

    def session_snapshot(self) -> dict | None:
        if self._snapshot_checked:
            return self._snapshot
        from . import herdr_plugin

        self._snapshot = herdr_plugin._session_snapshot()
        self._snapshot_checked = True
        return self._snapshot

    def roster(self) -> dict:
        if self._roster is not None:
            return self._roster
        from . import herdr_plugin

        snapshot = self.session_snapshot()
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

    def hive_facts(
        self, hive_id: str
    ) -> tuple[dict[str, dict], dict, dict[str, dict[str, object]]]:
        hive = self.sources.resolve_hive(hive_id)
        beads, runtime = self.sources.refresh_hive(hive)
        launch_preflight = self.launch_preflight(hive_id, hive.entry)
        ready_policy, ordering = operator_work_items.configured_ready_policy(
            cfg=self.cfg, entry=dict(hive.entry)
        )
        queues = {}
        queue_scopes = {}
        for name in ("ready", "active", "blocked"):
            query = operator_work_items.WorkItemQuery(
                queue=name,
                limit=operator_work_items.MAX_LIMIT,
                ordering=ordering if name == "ready" else "beadhive.work-items/v1",
            )
            queue_scopes[name] = query.scope
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
        return queues, self.roster(), queue_scopes

    def deck(self, hive: str, *, limit: int, cursor: str | None, width: int = 120) -> dict:
        queues, roster, queue_scopes = self.hive_facts(hive)
        return deck_payload(
            hive,
            queues,
            roster,
            limit=limit,
            cursor=cursor,
            width=width,
            queue_scopes=queue_scopes,
        )

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

    def dolt_comparison(self, hive_id: str, entry: Mapping[str, object]) -> Mapping[str, object]:
        """Read one bounded generic Dolt comparison without mutating sync state."""

        try:
            status = engine.get_engine(self.cfg).federation_status(
                registry.hive_dir(entry), timeout=engine.FEDERATION_TIMEOUT
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            status = engine.FederationStatus(ok=False, error="federation status unavailable")
        payload = hive_sync.comparison_payload([dict(entry)], [status])
        comparisons = payload.get("comparisons")
        if (
            isinstance(comparisons, list)
            and len(comparisons) == 1
            and isinstance(comparisons[0], Mapping)
            and comparisons[0].get("hive") == hive_id
        ):
            return comparisons[0]
        return hive_sync._comparison(
            hive_id, None, failure="a unique Dolt comparison is unavailable"
        )

    def crew(self, hive_id: str, *, limit: int, cursor: str | None) -> dict:
        self.sources.resolve_hive(hive_id)
        return crew_payload(
            hive_id,
            self.roster(),
            self.session_snapshot(),
            limit=limit,
            cursor=cursor,
        )

    def presentation(self, hive_id: str, *, ttl_ms: int = PRESENTATION_TTL_MS) -> dict:
        hive = self.sources.resolve_hive(hive_id)
        identity = hive_identity.identity_record(hive.entry)
        try:
            inventory = worktree.inventory_snapshot_payload(hive=hive_id, limit=MAX_LIMIT)
        except (OSError, RuntimeError, TypeError, ValueError, typer.Exit) as exc:
            inventory = worktree.inventory_payload(
                [
                    {
                        "hive_id": hive_id,
                        "hive_prefix": str(hive.entry.get("prefix") or ""),
                        "state": "unavailable",
                        "reason": f"worktree inventory unavailable: {exc}",
                        "revision": None,
                        "statuses": [],
                    }
                ],
                hive=hive_id,
                limit=MAX_LIMIT,
            )
        queues, roster, _queue_scopes = self.hive_facts(hive_id)
        return presentation_payload(
            hive_id,
            identity,
            inventory,
            queues,
            roster,
            self.session_snapshot(),
            dolt=self.dolt_comparison(hive_id, hive.entry),
            ttl_ms=ttl_ms,
        )


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


@cli.command("presentation")
def presentation_cmd(
    hive: str = typer.Option(..., "--hive"),
    ttl_ms: int = typer.Option(PRESENTATION_TTL_MS, "--ttl-ms", min=1, max=86_400_000),
    as_json: bool = typer.Option(False, "--json", help="accepted for the machine JSON contract"),
) -> None:
    """Return direct display-only workspace and pane metadata report patches."""
    del as_json
    backend = _backend()
    try:
        jsonout.emit(backend.presentation(hive, ttl_ms=ttl_ms))
    except OperatorSourceError as exc:
        _emit_error("presentation", exc)
    finally:
        backend.close()


@cli.command("crew")
def crew_cmd(
    hive: str = typer.Option(..., "--hive"),
    limit: int = typer.Option(MAX_LIMIT, "--limit"),
    cursor: str | None = typer.Option(None, "--cursor"),
    as_json: bool = typer.Option(False, "--json", help="accepted for the machine JSON contract"),
) -> None:
    """Return one bounded, exact-session Crew ownership and layout projection."""
    del as_json
    backend = _backend()
    try:
        jsonout.emit(backend.crew(hive, limit=_limit(limit), cursor=cursor))
    except OperatorSourceError as exc:
        _emit_error("crew", exc)
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
