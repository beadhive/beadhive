from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.bond_ref import BondRef
    from ..models.comment import Comment
    from ..models.issue_details_metadata import IssueDetailsMetadata
    from ..models.issue_with_dependency_metadata import IssueWithDependencyMetadata


T = TypeVar("T", bound="IssueDetails")


@_attrs_define
class IssueDetails:
    """An `Issue` with its labels, dependency edges and cardinalities — the body of `GET /v0/beads/issues/{id}`.
    `dependencies` and `dependents` carry FULL issue objects plus the edge type, not bare edges. Property semantics are
    documented on `Issue`.

        Attributes:
            id (str):
            title (str):
            priority (int):
            created_at (datetime.datetime):
            updated_at (datetime.datetime):
            revision (str): The row's optimistic-concurrency token, and THE READ THAT SOURCES A GUARD. Every
                `expected_version` on this surface is composed from a token some response carried; until this member existed the
                only responses that carried one were WRITES, so a caller's first guarded write had to be preceded by a write it
                did not want to make. This is that member, and it is the one the write responses' "when a read publishes one,
                this member is what it will agree with" was written against.

                IT IS EQUALITY-ONLY: compare it, never order or interpret it. A change signals the row was mutated since you
                read it, and nothing more — it is a random value the engine rewrites, not a counter. `ApplyItemResult.revision`
                states the full rule and this member carries it verbatim.

                ITS COVERAGE IS PARTIAL, on the same inherited terms: the token is rewritten by claim, close, unclaim and the
                generic update path, and NOT by the direct-update paths that rewrite text without touching it. A client needing
                complete change detection combines it with `updated_at`, `status` and the label set.

                It is ALWAYS PRESENT, including as `"0"` — a legacy row backfilled and not mutated since — because an absent
                member would be ambiguous between a legacy-zero row and a server that does not publish the token.

                IT IS THE ANCHOR ROW'S ALONE. The issues nested under `dependencies` and `dependents` are
                `IssueWithDependencyMetadata` and carry no token: a caller that means to guard a NEIGHBOUR reads that neighbour
                with its own `GET /v0/beads/issues/{id}`, and a token lifted off an embedded relation would be one the response
                never promised was fresh.

                IT IS NOT ON THE LIST ROWS, and that is a decision rather than an oversight — see `GET /v0/beads/issues`.

                IT IS A STRING, the token's decimal spelling — `"-3819021935081927"`, or `"0"` for a legacy migration-0054 row.
                Send it back verbatim as an `expected_version`; do not parse it into a number. A JSON number would not survive
                the trip: the token spans the FULL 64-bit range, and an IEEE-754-double parser — JavaScript's `JSON.parse`, Go's
                `any`, Python's `float` — rounds anything past 2^53 to a value NEAR the token that is not it, so a guard
                composed from it is refused against a row nothing else touched. A string round-trips exactly in every consumer.
            description (str | Unset):
            design (str | Unset):
            acceptance_criteria (str | Unset):
            notes (str | Unset):
            spec_id (str | Unset):
            status (str | Unset):
            issue_type (str | Unset):
            is_blocked (bool | Unset):
            assignee (str | Unset):
            owner (str | Unset):
            estimated_minutes (int | Unset):
            created_by (str | Unset):
            started_at (datetime.datetime | Unset):
            closed_at (datetime.datetime | Unset):
            close_reason (str | Unset):
            closed_by_session (str | Unset):
            lease_expires_at (datetime.datetime | Unset):
            heartbeat_at (datetime.datetime | Unset):
            lease_granted_node (str | Unset):
            due_at (datetime.datetime | Unset):
            defer_until (datetime.datetime | Unset):
            external_ref (str | Unset):
            source_system (str | Unset):
            metadata (IssueDetailsMetadata | Unset):
            compaction_level (int | Unset):
            compacted_at (datetime.datetime | Unset):
            compacted_at_commit (str | Unset):
            original_size (int | Unset):
            sender (str | Unset):
            ephemeral (bool | Unset):
            no_history (bool | Unset):
            wisp_type (str | Unset):
            storage_class (str | Unset):
            pinned (bool | Unset):
            is_template (bool | Unset):
            bonded_from (list[BondRef] | Unset):
            await_type (str | Unset):
            await_id (str | Unset):
            timeout (int | Unset):
            waiters (list[str] | Unset):
            source_formula (str | Unset):
            source_location (str | Unset):
            mol_type (str | Unset):
            work_type (str | Unset):
            event_kind (str | Unset):
            actor (str | Unset):
            target (str | Unset):
            payload (str | Unset):
            labels (list[str] | Unset):
            dependencies (list[IssueWithDependencyMetadata] | Unset): Issues this one depends on, each carrying its edge
                type.
            comments (list[Comment] | Unset): The issue's comment bodies. Populated only when the request sets `getIssue`'s
                `include_comments`; absent otherwise, which is what `comment_count` and `comments_omitted` report about.
            dependents (list[IssueWithDependencyMetadata] | Unset): Issues that depend on this one, each carrying its edge
                type. Populated only when the request sets `getIssue`'s `include_dependents`; absent otherwise, where
                `dependent_count` is the cardinality.
            parent (str | Unset): Parent issue id, computed from the parent-child edge.
            dependency_count (int | Unset):
            dependent_count (int | Unset):
            comment_count (int | Unset):
            comments_omitted (bool | Unset): True when `comment_count` is nonzero and `comments` was left out — every such
                issue on a request that did not set `include_comments`. Without it, an absent `comments` key is ambiguous
                between "no comments" and "comments not included in this response".
            epic_total_children (int | Unset):
            epic_closed_children (int | Unset):
            epic_closeable (bool | Unset):
    """

    id: str
    title: str
    priority: int
    created_at: datetime.datetime
    updated_at: datetime.datetime
    revision: str
    description: str | Unset = UNSET
    design: str | Unset = UNSET
    acceptance_criteria: str | Unset = UNSET
    notes: str | Unset = UNSET
    spec_id: str | Unset = UNSET
    status: str | Unset = UNSET
    issue_type: str | Unset = UNSET
    is_blocked: bool | Unset = UNSET
    assignee: str | Unset = UNSET
    owner: str | Unset = UNSET
    estimated_minutes: int | Unset = UNSET
    created_by: str | Unset = UNSET
    started_at: datetime.datetime | Unset = UNSET
    closed_at: datetime.datetime | Unset = UNSET
    close_reason: str | Unset = UNSET
    closed_by_session: str | Unset = UNSET
    lease_expires_at: datetime.datetime | Unset = UNSET
    heartbeat_at: datetime.datetime | Unset = UNSET
    lease_granted_node: str | Unset = UNSET
    due_at: datetime.datetime | Unset = UNSET
    defer_until: datetime.datetime | Unset = UNSET
    external_ref: str | Unset = UNSET
    source_system: str | Unset = UNSET
    metadata: IssueDetailsMetadata | Unset = UNSET
    compaction_level: int | Unset = UNSET
    compacted_at: datetime.datetime | Unset = UNSET
    compacted_at_commit: str | Unset = UNSET
    original_size: int | Unset = UNSET
    sender: str | Unset = UNSET
    ephemeral: bool | Unset = UNSET
    no_history: bool | Unset = UNSET
    wisp_type: str | Unset = UNSET
    storage_class: str | Unset = UNSET
    pinned: bool | Unset = UNSET
    is_template: bool | Unset = UNSET
    bonded_from: list[BondRef] | Unset = UNSET
    await_type: str | Unset = UNSET
    await_id: str | Unset = UNSET
    timeout: int | Unset = UNSET
    waiters: list[str] | Unset = UNSET
    source_formula: str | Unset = UNSET
    source_location: str | Unset = UNSET
    mol_type: str | Unset = UNSET
    work_type: str | Unset = UNSET
    event_kind: str | Unset = UNSET
    actor: str | Unset = UNSET
    target: str | Unset = UNSET
    payload: str | Unset = UNSET
    labels: list[str] | Unset = UNSET
    dependencies: list[IssueWithDependencyMetadata] | Unset = UNSET
    comments: list[Comment] | Unset = UNSET
    dependents: list[IssueWithDependencyMetadata] | Unset = UNSET
    parent: str | Unset = UNSET
    dependency_count: int | Unset = UNSET
    dependent_count: int | Unset = UNSET
    comment_count: int | Unset = UNSET
    comments_omitted: bool | Unset = UNSET
    epic_total_children: int | Unset = UNSET
    epic_closed_children: int | Unset = UNSET
    epic_closeable: bool | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        title = self.title

        priority = self.priority

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

        revision = self.revision

        description = self.description

        design = self.design

        acceptance_criteria = self.acceptance_criteria

        notes = self.notes

        spec_id = self.spec_id

        status = self.status

        issue_type = self.issue_type

        is_blocked = self.is_blocked

        assignee = self.assignee

        owner = self.owner

        estimated_minutes = self.estimated_minutes

        created_by = self.created_by

        started_at: str | Unset = UNSET
        if not isinstance(self.started_at, Unset):
            started_at = self.started_at.isoformat()

        closed_at: str | Unset = UNSET
        if not isinstance(self.closed_at, Unset):
            closed_at = self.closed_at.isoformat()

        close_reason = self.close_reason

        closed_by_session = self.closed_by_session

        lease_expires_at: str | Unset = UNSET
        if not isinstance(self.lease_expires_at, Unset):
            lease_expires_at = self.lease_expires_at.isoformat()

        heartbeat_at: str | Unset = UNSET
        if not isinstance(self.heartbeat_at, Unset):
            heartbeat_at = self.heartbeat_at.isoformat()

        lease_granted_node = self.lease_granted_node

        due_at: str | Unset = UNSET
        if not isinstance(self.due_at, Unset):
            due_at = self.due_at.isoformat()

        defer_until: str | Unset = UNSET
        if not isinstance(self.defer_until, Unset):
            defer_until = self.defer_until.isoformat()

        external_ref = self.external_ref

        source_system = self.source_system

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        compaction_level = self.compaction_level

        compacted_at: str | Unset = UNSET
        if not isinstance(self.compacted_at, Unset):
            compacted_at = self.compacted_at.isoformat()

        compacted_at_commit = self.compacted_at_commit

        original_size = self.original_size

        sender = self.sender

        ephemeral = self.ephemeral

        no_history = self.no_history

        wisp_type = self.wisp_type

        storage_class = self.storage_class

        pinned = self.pinned

        is_template = self.is_template

        bonded_from: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.bonded_from, Unset):
            bonded_from = []
            for bonded_from_item_data in self.bonded_from:
                bonded_from_item = bonded_from_item_data.to_dict()
                bonded_from.append(bonded_from_item)

        await_type = self.await_type

        await_id = self.await_id

        timeout = self.timeout

        waiters: list[str] | Unset = UNSET
        if not isinstance(self.waiters, Unset):
            waiters = self.waiters

        source_formula = self.source_formula

        source_location = self.source_location

        mol_type = self.mol_type

        work_type = self.work_type

        event_kind = self.event_kind

        actor = self.actor

        target = self.target

        payload = self.payload

        labels: list[str] | Unset = UNSET
        if not isinstance(self.labels, Unset):
            labels = self.labels

        dependencies: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.dependencies, Unset):
            dependencies = []
            for dependencies_item_data in self.dependencies:
                dependencies_item = dependencies_item_data.to_dict()
                dependencies.append(dependencies_item)

        comments: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.comments, Unset):
            comments = []
            for comments_item_data in self.comments:
                comments_item = comments_item_data.to_dict()
                comments.append(comments_item)

        dependents: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.dependents, Unset):
            dependents = []
            for dependents_item_data in self.dependents:
                dependents_item = dependents_item_data.to_dict()
                dependents.append(dependents_item)

        parent = self.parent

        dependency_count = self.dependency_count

        dependent_count = self.dependent_count

        comment_count = self.comment_count

        comments_omitted = self.comments_omitted

        epic_total_children = self.epic_total_children

        epic_closed_children = self.epic_closed_children

        epic_closeable = self.epic_closeable

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "priority": priority,
                "created_at": created_at,
                "updated_at": updated_at,
                "revision": revision,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if design is not UNSET:
            field_dict["design"] = design
        if acceptance_criteria is not UNSET:
            field_dict["acceptance_criteria"] = acceptance_criteria
        if notes is not UNSET:
            field_dict["notes"] = notes
        if spec_id is not UNSET:
            field_dict["spec_id"] = spec_id
        if status is not UNSET:
            field_dict["status"] = status
        if issue_type is not UNSET:
            field_dict["issue_type"] = issue_type
        if is_blocked is not UNSET:
            field_dict["is_blocked"] = is_blocked
        if assignee is not UNSET:
            field_dict["assignee"] = assignee
        if owner is not UNSET:
            field_dict["owner"] = owner
        if estimated_minutes is not UNSET:
            field_dict["estimated_minutes"] = estimated_minutes
        if created_by is not UNSET:
            field_dict["created_by"] = created_by
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if closed_at is not UNSET:
            field_dict["closed_at"] = closed_at
        if close_reason is not UNSET:
            field_dict["close_reason"] = close_reason
        if closed_by_session is not UNSET:
            field_dict["closed_by_session"] = closed_by_session
        if lease_expires_at is not UNSET:
            field_dict["lease_expires_at"] = lease_expires_at
        if heartbeat_at is not UNSET:
            field_dict["heartbeat_at"] = heartbeat_at
        if lease_granted_node is not UNSET:
            field_dict["lease_granted_node"] = lease_granted_node
        if due_at is not UNSET:
            field_dict["due_at"] = due_at
        if defer_until is not UNSET:
            field_dict["defer_until"] = defer_until
        if external_ref is not UNSET:
            field_dict["external_ref"] = external_ref
        if source_system is not UNSET:
            field_dict["source_system"] = source_system
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if compaction_level is not UNSET:
            field_dict["compaction_level"] = compaction_level
        if compacted_at is not UNSET:
            field_dict["compacted_at"] = compacted_at
        if compacted_at_commit is not UNSET:
            field_dict["compacted_at_commit"] = compacted_at_commit
        if original_size is not UNSET:
            field_dict["original_size"] = original_size
        if sender is not UNSET:
            field_dict["sender"] = sender
        if ephemeral is not UNSET:
            field_dict["ephemeral"] = ephemeral
        if no_history is not UNSET:
            field_dict["no_history"] = no_history
        if wisp_type is not UNSET:
            field_dict["wisp_type"] = wisp_type
        if storage_class is not UNSET:
            field_dict["storage_class"] = storage_class
        if pinned is not UNSET:
            field_dict["pinned"] = pinned
        if is_template is not UNSET:
            field_dict["is_template"] = is_template
        if bonded_from is not UNSET:
            field_dict["bonded_from"] = bonded_from
        if await_type is not UNSET:
            field_dict["await_type"] = await_type
        if await_id is not UNSET:
            field_dict["await_id"] = await_id
        if timeout is not UNSET:
            field_dict["timeout"] = timeout
        if waiters is not UNSET:
            field_dict["waiters"] = waiters
        if source_formula is not UNSET:
            field_dict["source_formula"] = source_formula
        if source_location is not UNSET:
            field_dict["source_location"] = source_location
        if mol_type is not UNSET:
            field_dict["mol_type"] = mol_type
        if work_type is not UNSET:
            field_dict["work_type"] = work_type
        if event_kind is not UNSET:
            field_dict["event_kind"] = event_kind
        if actor is not UNSET:
            field_dict["actor"] = actor
        if target is not UNSET:
            field_dict["target"] = target
        if payload is not UNSET:
            field_dict["payload"] = payload
        if labels is not UNSET:
            field_dict["labels"] = labels
        if dependencies is not UNSET:
            field_dict["dependencies"] = dependencies
        if comments is not UNSET:
            field_dict["comments"] = comments
        if dependents is not UNSET:
            field_dict["dependents"] = dependents
        if parent is not UNSET:
            field_dict["parent"] = parent
        if dependency_count is not UNSET:
            field_dict["dependency_count"] = dependency_count
        if dependent_count is not UNSET:
            field_dict["dependent_count"] = dependent_count
        if comment_count is not UNSET:
            field_dict["comment_count"] = comment_count
        if comments_omitted is not UNSET:
            field_dict["comments_omitted"] = comments_omitted
        if epic_total_children is not UNSET:
            field_dict["epic_total_children"] = epic_total_children
        if epic_closed_children is not UNSET:
            field_dict["epic_closed_children"] = epic_closed_children
        if epic_closeable is not UNSET:
            field_dict["epic_closeable"] = epic_closeable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.bond_ref import BondRef  # noqa: PLC0415
        from ..models.comment import Comment  # noqa: PLC0415
        from ..models.issue_details_metadata import IssueDetailsMetadata  # noqa: PLC0415
        from ..models.issue_with_dependency_metadata import (
            IssueWithDependencyMetadata,  # noqa: PLC0415
        )

        d = dict(src_dict)
        id = d.pop("id")

        title = d.pop("title")

        priority = d.pop("priority")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))

        revision = d.pop("revision")

        description = d.pop("description", UNSET)

        design = d.pop("design", UNSET)

        acceptance_criteria = d.pop("acceptance_criteria", UNSET)

        notes = d.pop("notes", UNSET)

        spec_id = d.pop("spec_id", UNSET)

        status = d.pop("status", UNSET)

        issue_type = d.pop("issue_type", UNSET)

        is_blocked = d.pop("is_blocked", UNSET)

        assignee = d.pop("assignee", UNSET)

        owner = d.pop("owner", UNSET)

        estimated_minutes = d.pop("estimated_minutes", UNSET)

        created_by = d.pop("created_by", UNSET)

        _started_at = d.pop("started_at", UNSET)
        started_at: datetime.datetime | Unset
        if isinstance(_started_at, Unset):
            started_at = UNSET
        else:
            started_at = datetime.datetime.fromisoformat(_started_at)

        _closed_at = d.pop("closed_at", UNSET)
        closed_at: datetime.datetime | Unset
        if isinstance(_closed_at, Unset):
            closed_at = UNSET
        else:
            closed_at = datetime.datetime.fromisoformat(_closed_at)

        close_reason = d.pop("close_reason", UNSET)

        closed_by_session = d.pop("closed_by_session", UNSET)

        _lease_expires_at = d.pop("lease_expires_at", UNSET)
        lease_expires_at: datetime.datetime | Unset
        if isinstance(_lease_expires_at, Unset):
            lease_expires_at = UNSET
        else:
            lease_expires_at = datetime.datetime.fromisoformat(_lease_expires_at)

        _heartbeat_at = d.pop("heartbeat_at", UNSET)
        heartbeat_at: datetime.datetime | Unset
        if isinstance(_heartbeat_at, Unset):
            heartbeat_at = UNSET
        else:
            heartbeat_at = datetime.datetime.fromisoformat(_heartbeat_at)

        lease_granted_node = d.pop("lease_granted_node", UNSET)

        _due_at = d.pop("due_at", UNSET)
        due_at: datetime.datetime | Unset
        if isinstance(_due_at, Unset):
            due_at = UNSET
        else:
            due_at = datetime.datetime.fromisoformat(_due_at)

        _defer_until = d.pop("defer_until", UNSET)
        defer_until: datetime.datetime | Unset
        if isinstance(_defer_until, Unset):
            defer_until = UNSET
        else:
            defer_until = datetime.datetime.fromisoformat(_defer_until)

        external_ref = d.pop("external_ref", UNSET)

        source_system = d.pop("source_system", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: IssueDetailsMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = IssueDetailsMetadata.from_dict(_metadata)

        compaction_level = d.pop("compaction_level", UNSET)

        _compacted_at = d.pop("compacted_at", UNSET)
        compacted_at: datetime.datetime | Unset
        if isinstance(_compacted_at, Unset):
            compacted_at = UNSET
        else:
            compacted_at = datetime.datetime.fromisoformat(_compacted_at)

        compacted_at_commit = d.pop("compacted_at_commit", UNSET)

        original_size = d.pop("original_size", UNSET)

        sender = d.pop("sender", UNSET)

        ephemeral = d.pop("ephemeral", UNSET)

        no_history = d.pop("no_history", UNSET)

        wisp_type = d.pop("wisp_type", UNSET)

        storage_class = d.pop("storage_class", UNSET)

        pinned = d.pop("pinned", UNSET)

        is_template = d.pop("is_template", UNSET)

        _bonded_from = d.pop("bonded_from", UNSET)
        bonded_from: list[BondRef] | Unset = UNSET
        if _bonded_from is not UNSET:
            bonded_from = []
            for bonded_from_item_data in _bonded_from:
                bonded_from_item = BondRef.from_dict(bonded_from_item_data)

                bonded_from.append(bonded_from_item)

        await_type = d.pop("await_type", UNSET)

        await_id = d.pop("await_id", UNSET)

        timeout = d.pop("timeout", UNSET)

        waiters = cast(list[str], d.pop("waiters", UNSET))

        source_formula = d.pop("source_formula", UNSET)

        source_location = d.pop("source_location", UNSET)

        mol_type = d.pop("mol_type", UNSET)

        work_type = d.pop("work_type", UNSET)

        event_kind = d.pop("event_kind", UNSET)

        actor = d.pop("actor", UNSET)

        target = d.pop("target", UNSET)

        payload = d.pop("payload", UNSET)

        labels = cast(list[str], d.pop("labels", UNSET))

        _dependencies = d.pop("dependencies", UNSET)
        dependencies: list[IssueWithDependencyMetadata] | Unset = UNSET
        if _dependencies is not UNSET:
            dependencies = []
            for dependencies_item_data in _dependencies:
                dependencies_item = IssueWithDependencyMetadata.from_dict(dependencies_item_data)

                dependencies.append(dependencies_item)

        _comments = d.pop("comments", UNSET)
        comments: list[Comment] | Unset = UNSET
        if _comments is not UNSET:
            comments = []
            for comments_item_data in _comments:
                comments_item = Comment.from_dict(comments_item_data)

                comments.append(comments_item)

        _dependents = d.pop("dependents", UNSET)
        dependents: list[IssueWithDependencyMetadata] | Unset = UNSET
        if _dependents is not UNSET:
            dependents = []
            for dependents_item_data in _dependents:
                dependents_item = IssueWithDependencyMetadata.from_dict(dependents_item_data)

                dependents.append(dependents_item)

        parent = d.pop("parent", UNSET)

        dependency_count = d.pop("dependency_count", UNSET)

        dependent_count = d.pop("dependent_count", UNSET)

        comment_count = d.pop("comment_count", UNSET)

        comments_omitted = d.pop("comments_omitted", UNSET)

        epic_total_children = d.pop("epic_total_children", UNSET)

        epic_closed_children = d.pop("epic_closed_children", UNSET)

        epic_closeable = d.pop("epic_closeable", UNSET)

        issue_details = cls(
            id=id,
            title=title,
            priority=priority,
            created_at=created_at,
            updated_at=updated_at,
            revision=revision,
            description=description,
            design=design,
            acceptance_criteria=acceptance_criteria,
            notes=notes,
            spec_id=spec_id,
            status=status,
            issue_type=issue_type,
            is_blocked=is_blocked,
            assignee=assignee,
            owner=owner,
            estimated_minutes=estimated_minutes,
            created_by=created_by,
            started_at=started_at,
            closed_at=closed_at,
            close_reason=close_reason,
            closed_by_session=closed_by_session,
            lease_expires_at=lease_expires_at,
            heartbeat_at=heartbeat_at,
            lease_granted_node=lease_granted_node,
            due_at=due_at,
            defer_until=defer_until,
            external_ref=external_ref,
            source_system=source_system,
            metadata=metadata,
            compaction_level=compaction_level,
            compacted_at=compacted_at,
            compacted_at_commit=compacted_at_commit,
            original_size=original_size,
            sender=sender,
            ephemeral=ephemeral,
            no_history=no_history,
            wisp_type=wisp_type,
            storage_class=storage_class,
            pinned=pinned,
            is_template=is_template,
            bonded_from=bonded_from,
            await_type=await_type,
            await_id=await_id,
            timeout=timeout,
            waiters=waiters,
            source_formula=source_formula,
            source_location=source_location,
            mol_type=mol_type,
            work_type=work_type,
            event_kind=event_kind,
            actor=actor,
            target=target,
            payload=payload,
            labels=labels,
            dependencies=dependencies,
            comments=comments,
            dependents=dependents,
            parent=parent,
            dependency_count=dependency_count,
            dependent_count=dependent_count,
            comment_count=comment_count,
            comments_omitted=comments_omitted,
            epic_total_children=epic_total_children,
            epic_closed_children=epic_closed_children,
            epic_closeable=epic_closeable,
        )

        issue_details.additional_properties = d
        return issue_details

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
