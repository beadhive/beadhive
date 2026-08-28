"""Generic advertised-action projections shared by operator clients.

Applicable actions are always present.  A proven policy refusal is ``forbidden``;
missing or stale evidence is ``unavailable``.  Inapplicable actions are omitted.
Every descriptor is declarative and intentionally contains no command line or
presentation vocabulary.
"""

from __future__ import annotations

from typing import Any

AVAILABILITIES = frozenset({"allowed", "confirmation-required", "forbidden", "unavailable"})
CONSEQUENCES = frozenset({"navigate", "read", "reversible-write", "approval", "destructive"})


def no_input() -> dict[str, object]:
    return {"transport": "none", "required": False, "schema": None}


def parameter_input(properties: dict[str, object]) -> dict[str, object]:
    return {
        "transport": "parameters",
        "required": False,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": properties,
        },
    }


def prompt_input(*, max_bytes: int) -> dict[str, object]:
    return {
        "transport": "stdin",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 1,
            "maxBytes": max_bytes,
            "contentMediaType": "text/plain",
            "sensitive": True,
        },
    }


def advertised_action(
    action_id: str,
    capability: str,
    target: dict[str, object],
    *,
    availability: str,
    reason_code: str | None,
    reason: str,
    consequence: str,
    advertised_at: int,
    source_revision: str | None,
    input_shape: dict[str, object] | None = None,
    require_revision: bool = False,
) -> dict[str, Any]:
    """Return one stable, JSON-ready action descriptor.

    ``require_revision`` describes optimistic concurrency at invocation time.  A
    mutation cannot be advertised as executable without a revision to compare.
    """

    if availability not in AVAILABILITIES:
        raise ValueError(f"unsupported action availability: {availability}")
    if consequence not in CONSEQUENCES:
        raise ValueError(f"unsupported action consequence: {consequence}")
    if (
        require_revision
        and source_revision is None
        and availability
        in {
            "allowed",
            "confirmation-required",
        }
    ):
        availability = "unavailable"
        reason_code = "source_revision_unavailable"
        reason = "a current source revision is required before this action can run"
    return {
        "id": action_id,
        "capability": capability,
        "target": target,
        "availability": availability,
        "reasonCode": reason_code,
        "reason": reason,
        "consequence": consequence,
        "advertisedAt": advertised_at,
        "sourceRevision": source_revision,
        "preconditions": {
            "sourceRevision": source_revision,
            "mustMatch": require_revision,
        },
        "input": input_shape if input_shape is not None else no_input(),
    }


def hive_actions(*, hive_id: str, revision: str | None, advertised_at: int) -> list[dict[str, Any]]:
    target = {"hiveId": hive_id, "kind": "hive", "id": hive_id}
    return [
        advertised_action(
            "hive.inspect",
            "inspect",
            target,
            availability="allowed",
            reason_code=None,
            reason="the hive is registered",
            consequence="navigate",
            advertised_at=advertised_at,
            source_revision=revision,
        ),
        advertised_action(
            "hive.refresh",
            "refresh",
            target,
            availability="allowed",
            reason_code=None,
            reason="the registered hive can be refreshed",
            consequence="read",
            advertised_at=advertised_at,
            source_revision=revision,
        ),
    ]


def work_item_actions(
    *,
    target: dict[str, object],
    readiness: str,
    readiness_reason: str,
    partial: bool,
    revision: str,
    advertised_at: int,
) -> list[dict[str, Any]]:
    if partial:
        launch_state = "unavailable"
        launch_code = "work_item_projection_partial"
        launch_reason = "authoritative launch prerequisites are only partially observed"
    elif readiness == "ready":
        launch_state = "allowed"
        launch_code = None
        launch_reason = readiness_reason
    elif readiness in {"blocked", "completed"}:
        launch_state = "forbidden"
        launch_code = f"work_item_{readiness}"
        launch_reason = readiness_reason
    elif readiness == "active":
        launch_state = "unavailable"
        launch_code = "work_item_claim_ownership_required"
        launch_reason = "the current caller's claim ownership must be proven before reuse"
    else:
        launch_state = "unavailable"
        launch_code = "work_item_state_unavailable"
        launch_reason = readiness_reason

    return [
        advertised_action(
            "work-item.inspect",
            "inspect",
            target,
            availability="allowed",
            reason_code=None,
            reason="the exact work item is present in the projection",
            consequence="navigate",
            advertised_at=advertised_at,
            source_revision=revision,
        ),
        advertised_action(
            "work-item.refresh",
            "refresh",
            target,
            availability="allowed",
            reason_code=None,
            reason="the exact work item can be refreshed",
            consequence="read",
            advertised_at=advertised_at,
            source_revision=revision,
        ),
        advertised_action(
            "work-item.launch",
            "launch",
            target,
            availability=launch_state,
            reason_code=launch_code,
            reason=launch_reason,
            consequence="reversible-write",
            advertised_at=advertised_at,
            source_revision=revision,
            require_revision=True,
            input_shape=parameter_input(
                {
                    "actor": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "minLength": 1},
                    "direction": {"enum": ["right", "down"], "default": "right"},
                    "focus": {"type": "boolean", "default": False},
                    "adoptExpired": {"type": "boolean", "default": False},
                }
            ),
        ),
    ]


def agent_action_availability(
    ownership_state: str, lifecycle_state: str, reason: str
) -> tuple[str, str | None, str]:
    """Map the roster's authoritative ownership/lifecycle proof to command policy."""

    if ownership_state == "owned" and lifecycle_state in {"idle", "working", "blocked"}:
        return "allowed", None, "current bh-owned live pane is proven"
    if ownership_state == "foreign":
        return "forbidden", "agent_not_bh_managed", reason
    if ownership_state in {"stale", "unknown"}:
        return "unavailable", "agent_ownership_unproven", reason
    return "unavailable", "agent_not_live", reason


def agent_actions(
    *,
    target: dict[str, object],
    ownership_state: str,
    lifecycle_state: str,
    reason: str,
    revision: str,
    advertised_at: int,
    max_prompt_bytes: int,
) -> list[dict[str, Any]]:
    availability, reason_code, detail = agent_action_availability(
        ownership_state, lifecycle_state, reason
    )
    common = {
        "availability": availability,
        "reason_code": reason_code,
        "reason": detail,
        "advertised_at": advertised_at,
        "source_revision": revision,
        "require_revision": True,
    }
    operation_id = {"operationId": {"type": "string", "minLength": 1, "maxLength": 128}}
    return [
        advertised_action(
            "agent.inspect",
            "inspect",
            target,
            availability="allowed",
            reason_code=None,
            reason="the agent is present in the current roster",
            consequence="navigate",
            advertised_at=advertised_at,
            source_revision=revision,
        ),
        advertised_action(
            "agent.attach",
            "attach",
            target,
            consequence="approval",
            input_shape=parameter_input(operation_id),
            **common,
        ),
        advertised_action(
            "agent.dispatch",
            "safe-instruction",
            target,
            consequence="reversible-write",
            input_shape=prompt_input(max_bytes=max_prompt_bytes),
            **common,
        ),
        advertised_action(
            "agent.watch",
            "watch",
            target,
            consequence="read",
            input_shape=parameter_input(
                {
                    **operation_id,
                    "timeoutSeconds": {"type": "integer", "minimum": 1},
                }
            ),
            **common,
        ),
        advertised_action(
            "agent.reap",
            "reap",
            target,
            availability=("confirmation-required" if availability == "allowed" else availability),
            reason_code=reason_code,
            reason=detail,
            consequence="destructive",
            advertised_at=advertised_at,
            source_revision=revision,
            require_revision=True,
            input_shape=parameter_input(operation_id),
        ),
    ]
