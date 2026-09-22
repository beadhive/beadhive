"""Generated canonical Beads record models. Do not edit by hand.

Regenerate with ``uv run python scripts/generate_beads_models.py`` after intentionally capturing
a new pinned ``bd schema`` artifact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
)
from pydantic.experimental.missing_sentinel import MISSING

SOURCE_BEADS_VERSION = "1.3.0"
SOURCE_BEADS_COMMIT = "f45b249ce6b40ba62aecc03949e6371e8f7c79d8"
SOURCE_SCHEMA_SHA256 = "caf8fe18e1117c067ff7bbd824484b24849677e2cbf86d793ddf6b64069a31a2"


def _require_datetime_string(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("date-time value must be a JSON string")
    return value


_JsonDateTime = Annotated[datetime, BeforeValidator(_require_datetime_string)]


class _StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BdDependencyRecord(_StrictRecord):
    id: StrictStr | MISSING = MISSING
    issue_id: StrictStr
    depends_on_id: StrictStr
    type: Literal[
        "blocks",
        "parent-child",
        "conditional-blocks",
        "waits-for",
        "related",
        "discovered-from",
        "replies-to",
        "relates-to",
        "duplicates",
        "supersedes",
        "authored-by",
        "assigned-to",
        "approved-by",
        "attests",
        "tracks",
        "until",
        "caused-by",
        "validates",
        "delegated-from",
    ]
    created_at: _JsonDateTime
    created_by: StrictStr | MISSING = MISSING
    metadata: StrictStr | MISSING = MISSING
    thread_id: StrictStr | MISSING = MISSING


class BdIssueComment(_StrictRecord):
    id: StrictStr
    issue_id: StrictStr
    author: StrictStr
    text: StrictStr
    created_at: _JsonDateTime


class BdIssueBond(_StrictRecord):
    source_id: StrictStr
    bond_type: StrictStr
    bond_point: StrictStr | MISSING = MISSING


class BdIssueRecord(_StrictRecord):
    id: StrictStr
    title: StrictStr
    description: StrictStr | MISSING = MISSING
    design: StrictStr | MISSING = MISSING
    acceptance_criteria: StrictStr | MISSING = MISSING
    notes: StrictStr | MISSING = MISSING
    spec_id: StrictStr | MISSING = MISSING
    status: (
        Literal[
            "open",
            "in_progress",
            "blocked",
            "deferred",
            "closed",
            "pinned",
            "hooked",
        ]
        | MISSING
    ) = MISSING
    priority: StrictInt
    issue_type: (
        Literal[
            "bug",
            "feature",
            "task",
            "epic",
            "chore",
            "decision",
            "message",
            "molecule",
            "gate",
            "spike",
            "story",
            "milestone",
        ]
        | MISSING
    ) = MISSING
    is_blocked: StrictBool | MISSING = MISSING
    assignee: StrictStr | MISSING = MISSING
    owner: StrictStr | MISSING = MISSING
    estimated_minutes: StrictInt | MISSING = MISSING
    created_at: _JsonDateTime
    created_by: StrictStr | MISSING = MISSING
    updated_at: _JsonDateTime
    started_at: _JsonDateTime | MISSING = MISSING
    closed_at: _JsonDateTime | MISSING = MISSING
    close_reason: StrictStr | MISSING = MISSING
    closed_by_session: StrictStr | MISSING = MISSING
    lease_expires_at: _JsonDateTime | MISSING = MISSING
    heartbeat_at: _JsonDateTime | MISSING = MISSING
    lease_granted_node: StrictStr | MISSING = MISSING
    due_at: _JsonDateTime | MISSING = MISSING
    defer_until: _JsonDateTime | MISSING = MISSING
    external_ref: StrictStr | MISSING = MISSING
    source_system: StrictStr | MISSING = MISSING
    metadata: Any | MISSING = MISSING
    compaction_level: StrictInt | MISSING = MISSING
    compacted_at: _JsonDateTime | MISSING = MISSING
    compacted_at_commit: StrictStr | MISSING = MISSING
    original_size: StrictInt | MISSING = MISSING
    labels: list[StrictStr] | MISSING = MISSING
    dependencies: list[BdDependencyRecord] | MISSING = MISSING
    comments: list[BdIssueComment] | MISSING = MISSING
    sender: StrictStr | MISSING = MISSING
    ephemeral: StrictBool | MISSING = MISSING
    no_history: StrictBool | MISSING = MISSING
    wisp_type: StrictStr | MISSING = MISSING
    storage_class: StrictStr | MISSING = MISSING
    pinned: StrictBool | MISSING = MISSING
    is_template: StrictBool | MISSING = MISSING
    bonded_from: list[BdIssueBond] | MISSING = MISSING
    await_type: StrictStr | MISSING = MISSING
    await_id: StrictStr | MISSING = MISSING
    timeout: StrictInt | MISSING = MISSING
    waiters: list[StrictStr] | MISSING = MISSING
    source_formula: StrictStr | MISSING = MISSING
    source_location: StrictStr | MISSING = MISSING
    mol_type: StrictStr | MISSING = MISSING
    work_type: StrictStr | MISSING = MISSING
    event_kind: StrictStr | MISSING = MISSING
    actor: StrictStr | MISSING = MISSING
    target: StrictStr | MISSING = MISSING
    payload: StrictStr | MISSING = MISSING


class BdBriefIssue(_StrictRecord):
    id: StrictStr
    title: StrictStr
    spec_id: StrictStr | MISSING = MISSING
    status: (
        Literal[
            "open",
            "in_progress",
            "blocked",
            "deferred",
            "closed",
            "pinned",
            "hooked",
        ]
        | MISSING
    ) = MISSING
    priority: StrictInt
    issue_type: (
        Literal[
            "bug",
            "feature",
            "task",
            "epic",
            "chore",
            "decision",
            "message",
            "molecule",
            "gate",
            "spike",
            "story",
            "milestone",
        ]
        | MISSING
    ) = MISSING
    is_blocked: StrictBool | MISSING = MISSING
    assignee: StrictStr | MISSING = MISSING
    owner: StrictStr | MISSING = MISSING
    estimated_minutes: StrictInt | MISSING = MISSING
    created_at: _JsonDateTime
    created_by: StrictStr | MISSING = MISSING
    updated_at: _JsonDateTime
    started_at: _JsonDateTime | MISSING = MISSING
    closed_at: _JsonDateTime | MISSING = MISSING
    close_reason: StrictStr | MISSING = MISSING
    closed_by_session: StrictStr | MISSING = MISSING
    lease_expires_at: _JsonDateTime | MISSING = MISSING
    heartbeat_at: _JsonDateTime | MISSING = MISSING
    lease_granted_node: StrictStr | MISSING = MISSING
    due_at: _JsonDateTime | MISSING = MISSING
    defer_until: _JsonDateTime | MISSING = MISSING
    external_ref: StrictStr | MISSING = MISSING
    source_system: StrictStr | MISSING = MISSING
    metadata: Any | MISSING = MISSING
    compaction_level: StrictInt | MISSING = MISSING
    compacted_at: _JsonDateTime | MISSING = MISSING
    compacted_at_commit: StrictStr | MISSING = MISSING
    original_size: StrictInt | MISSING = MISSING
    labels: list[StrictStr] | MISSING = MISSING
    dependencies: list[BdDependencyRecord] | MISSING = MISSING
    comments: list[BdIssueComment] | MISSING = MISSING
    sender: StrictStr | MISSING = MISSING
    ephemeral: StrictBool | MISSING = MISSING
    no_history: StrictBool | MISSING = MISSING
    wisp_type: StrictStr | MISSING = MISSING
    storage_class: StrictStr | MISSING = MISSING
    pinned: StrictBool | MISSING = MISSING
    is_template: StrictBool | MISSING = MISSING
    bonded_from: list[BdIssueBond] | MISSING = MISSING
    await_type: StrictStr | MISSING = MISSING
    await_id: StrictStr | MISSING = MISSING
    timeout: StrictInt | MISSING = MISSING
    source_formula: StrictStr | MISSING = MISSING
    source_location: StrictStr | MISSING = MISSING
    mol_type: StrictStr | MISSING = MISSING
    work_type: StrictStr | MISSING = MISSING
    event_kind: StrictStr | MISSING = MISSING
    actor: StrictStr | MISSING = MISSING
    target: StrictStr | MISSING = MISSING
