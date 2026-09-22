"""Generated canonical Beads record models. Do not edit by hand.

Regenerate with ``uv run python scripts/generate_beads_models.py`` after intentionally capturing
a new pinned ``bd schema`` artifact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

SOURCE_BEADS_VERSION = "1.3.0"
SOURCE_BEADS_COMMIT = "f45b249ce6b40ba62aecc03949e6371e8f7c79d8"
SOURCE_SCHEMA_SHA256 = "caf8fe18e1117c067ff7bbd824484b24849677e2cbf86d793ddf6b64069a31a2"


class _StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BdDependencyRecord(_StrictRecord):
    id: str | None = None
    issue_id: str
    depends_on_id: str
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
    created_at: datetime
    created_by: str | None = None
    metadata: str | None = None
    thread_id: str | None = None


class BdIssueComment(_StrictRecord):
    id: str
    issue_id: str
    author: str
    text: str
    created_at: datetime


class BdIssueBond(_StrictRecord):
    source_id: str
    bond_type: str
    bond_point: str | None = None


class BdIssueRecord(_StrictRecord):
    id: str
    title: str
    description: str | None = None
    design: str | None = None
    acceptance_criteria: str | None = None
    notes: str | None = None
    spec_id: str | None = None
    status: (
        Literal["open", "in_progress", "blocked", "deferred", "closed", "pinned", "hooked"] | None
    ) = None
    priority: int
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
        | None
    ) = None
    is_blocked: bool | None = None
    assignee: str | None = None
    owner: str | None = None
    estimated_minutes: int | None = None
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime
    started_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: str | None = None
    closed_by_session: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    lease_granted_node: str | None = None
    due_at: datetime | None = None
    defer_until: datetime | None = None
    external_ref: str | None = None
    source_system: str | None = None
    metadata: Any | None = None
    compaction_level: int | None = None
    compacted_at: datetime | None = None
    compacted_at_commit: str | None = None
    original_size: int | None = None
    labels: list[str] | None = None
    dependencies: list[BdDependencyRecord] | None = None
    comments: list[BdIssueComment] | None = None
    sender: str | None = None
    ephemeral: bool | None = None
    no_history: bool | None = None
    wisp_type: str | None = None
    storage_class: str | None = None
    pinned: bool | None = None
    is_template: bool | None = None
    bonded_from: list[BdIssueBond] | None = None
    await_type: str | None = None
    await_id: str | None = None
    timeout: int | None = None
    waiters: list[str] | None = None
    source_formula: str | None = None
    source_location: str | None = None
    mol_type: str | None = None
    work_type: str | None = None
    event_kind: str | None = None
    actor: str | None = None
    target: str | None = None
    payload: str | None = None


class BdBriefIssue(_StrictRecord):
    id: str
    title: str
    spec_id: str | None = None
    status: (
        Literal["open", "in_progress", "blocked", "deferred", "closed", "pinned", "hooked"] | None
    ) = None
    priority: int
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
        | None
    ) = None
    is_blocked: bool | None = None
    assignee: str | None = None
    owner: str | None = None
    estimated_minutes: int | None = None
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime
    started_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: str | None = None
    closed_by_session: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    lease_granted_node: str | None = None
    due_at: datetime | None = None
    defer_until: datetime | None = None
    external_ref: str | None = None
    source_system: str | None = None
    metadata: Any | None = None
    compaction_level: int | None = None
    compacted_at: datetime | None = None
    compacted_at_commit: str | None = None
    original_size: int | None = None
    labels: list[str] | None = None
    dependencies: list[BdDependencyRecord] | None = None
    comments: list[BdIssueComment] | None = None
    sender: str | None = None
    ephemeral: bool | None = None
    no_history: bool | None = None
    wisp_type: str | None = None
    storage_class: str | None = None
    pinned: bool | None = None
    is_template: bool | None = None
    bonded_from: list[BdIssueBond] | None = None
    await_type: str | None = None
    await_id: str | None = None
    timeout: int | None = None
    source_formula: str | None = None
    source_location: str | None = None
    mol_type: str | None = None
    work_type: str | None = None
    event_kind: str | None = None
    actor: str | None = None
    target: str | None = None
