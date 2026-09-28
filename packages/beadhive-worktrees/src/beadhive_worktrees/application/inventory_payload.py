"""Pure builder for the versioned, bounded managed-worktree machine contract (bh-qdezo.7).

Moved verbatim (byte-identical behavior) from ``worktree_inventory.py``'s
``impl_inventory_payload`` and its private helpers. This module returns the INNER payload dict
only — the ``schema_version``/``command`` envelope wrapping (``beadhive.jsonout.envelope``) stays
a root concern, since ``jsonout`` is a root module this package must never import.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from ..policy.classification import WtClassification

#: Default/maximum page size for ``bh worktree list --json``.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_STATES = frozenset(str(state) for state in WtClassification)


def compute_digest(value: Any) -> str:
    """The one ``sha256:...`` content-digest helper shared by every inventory revision — a
    per-hive observation's own ``revision`` and the combined ``source_revision`` below use the
    exact same hash, so a consumer never has to learn two digest shapes."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _cursor(revision: str, scope: dict, offset: int) -> str:
    value = {"v": 1, "revision": revision, "scope": scope, "offset": offset}
    return (
        base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _cursor_offset(cursor: str | None, revision: str, scope: dict) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the worktree inventory cursor is malformed") from exc
    if (
        not isinstance(value, dict)
        or value.get("v") != 1
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
    ):
        raise ValueError("the worktree inventory cursor is malformed")
    if value.get("scope") != scope:
        raise ValueError("the worktree inventory cursor belongs to different filters")
    if value.get("revision") != revision:
        raise ValueError("the worktree inventory changed; restart without a cursor")
    return value["offset"]


def _item(observation: dict, status: Any) -> dict:
    hive_id = str(observation["hive_id"])
    classification = str(status.classification)
    return {
        "hive_id": hive_id,
        "hive_prefix": str(observation["hive_prefix"]),
        "bead_id": status.bead_id,
        "worktree_id": f"{hive_id}:{status.leaf}",
        "leaf": status.leaf,
        "branch": status.branch,
        "path": status.path,
        "state": classification,
        "retention": "reclaimable" if status.safe else "retained",
        "merged": status.merged,
        "dirty": status.dirty,
        "safe": status.safe,
        "underlying_state": str(status.underlying) if status.underlying else None,
        "unknown_reason": status.unknown_reason or None,
    }


def _coverage_state(observations: list[dict]) -> str:
    states = {str(observation.get("state", "unavailable")) for observation in observations}
    if not states or states == {"complete"}:
        return "complete"
    if states == {"unavailable"}:
        return "unavailable"
    if states == {"stale"}:
        return "stale"
    return "partial"


def build_inventory_payload(
    observations: list[dict],
    *,
    hive: str = "",
    states: tuple[str, ...] = (),
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    generated_at: int | None = None,
) -> dict:
    """Build the versioned, bounded managed-worktree contract from source observations.

    An observation has an exact ``hive_id``, its configured ``hive_prefix``, a coverage
    ``state`` (complete/partial/stale/unavailable), an optional ``reason`` and ``revision``, and
    zero or more classified ``statuses``. Keeping this fold pure makes the important count rule
    explicit: totals are numbers only when every covered source is complete. A partial page is
    safe because totals are computed over the complete filtered snapshot before paging.
    """
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be from 1 through {MAX_LIMIT}")
    requested_states = tuple(sorted(set(states)))
    invalid_states = sorted(set(requested_states) - _STATES)
    if invalid_states:
        raise ValueError(f"unknown worktree state: {invalid_states[0]}")

    normalized: list[dict] = []
    all_items: list[dict] = []
    warnings: list[dict] = []
    for raw in observations:
        observation = {
            "hive_id": str(raw["hive_id"]),
            "hive_prefix": str(raw["hive_prefix"]),
            "state": str(raw.get("state") or "unavailable"),
            "reason": str(raw.get("reason") or "") or None,
            "revision": raw.get("revision"),
            "statuses": list(raw.get("statuses") or []),
        }
        if observation["state"] == "complete" and any(
            str(status.classification) == "unknown" for status in observation["statuses"]
        ):
            observation["state"] = "partial"
            observation["reason"] = "one or more worktree states could not be resolved"
        normalized.append(observation)
        all_items.extend(_item(observation, status) for status in observation["statuses"])
        if observation["state"] != "complete":
            warnings.append(
                {
                    "code": f"worktree_source_{observation['state']}",
                    "hive_id": observation["hive_id"],
                    "detail": observation["reason"],
                }
            )

    all_items.sort(key=lambda item: (item["hive_id"], item["worktree_id"]))
    coverage_state = _coverage_state(normalized)
    coverage_complete = coverage_state == "complete"
    filtered = [
        item for item in all_items if not requested_states or item["state"] in requested_states
    ]
    source_revision = (
        None
        if coverage_state == "unavailable"
        else compute_digest(
            [
                {
                    "hive_id": observation["hive_id"],
                    "state": observation["state"],
                    "revision": observation["revision"],
                    "items": [
                        item for item in all_items if item["hive_id"] == observation["hive_id"]
                    ],
                }
                for observation in normalized
            ]
        )
    )
    scope = {"hive": hive or None, "states": list(requested_states)}
    offset = _cursor_offset(cursor, source_revision or "unavailable", scope)
    if offset > len(filtered):
        raise ValueError("the worktree inventory cursor is outside the collection")
    page = filtered[offset : offset + limit]
    next_offset = offset + len(page)
    truncated = next_offset < len(filtered)

    counts = None
    if coverage_complete:
        counts = []
        for observation in normalized:
            hive_items = [item for item in all_items if item["hive_id"] == observation["hive_id"]]
            by_state = {
                state: sum(item["state"] == state for item in hive_items)
                for state in sorted({item["state"] for item in hive_items})
            }
            counts.append(
                {
                    "hive_id": observation["hive_id"],
                    "hive_prefix": observation["hive_prefix"],
                    "total": len(hive_items),
                    "by_state": by_state,
                }
            )

    now = generated_at if generated_at is not None else time.time_ns() // 1_000_000
    freshness_state = (
        "stale"
        if any(observation["state"] == "stale" for observation in normalized)
        else "unknown"
        if coverage_state == "unavailable"
        else "fresh"
    )
    reason = (
        "; ".join(
            sorted(
                {str(observation["reason"]) for observation in normalized if observation["reason"]}
            )
        )
        or None
    )
    return {
        "source_revision": source_revision,
        "generated_at": now,
        "freshness": {
            "state": freshness_state,
            "as_of": now if freshness_state != "unknown" else None,
        },
        "coverage": {
            "state": coverage_state,
            "reason": reason,
            "sources": [
                {
                    "hive_id": observation["hive_id"],
                    "state": observation["state"],
                    "reason": observation["reason"],
                    "revision": observation["revision"],
                }
                for observation in normalized
            ],
        },
        "filters": scope,
        "worktrees": page,
        "returned": len(page),
        "total": len(filtered) if coverage_complete else None,
        "counts": counts,
        "limit": limit,
        "truncated": truncated,
        "next_cursor": (
            _cursor(source_revision or "unavailable", scope, next_offset) if truncated else None
        ),
        "warnings": warnings,
    }


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "build_inventory_payload", "compute_digest"]
