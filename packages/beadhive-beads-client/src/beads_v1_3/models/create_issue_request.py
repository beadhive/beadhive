from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.create_issue_dependency import CreateIssueDependency
    from ..models.create_issue_waits_for import CreateIssueWaitsFor


T = TypeVar("T", bound="CreateIssueRequest")


@_attrs_define
class CreateIssueRequest:
    """One issue, its parent, its explicit edges and its waits-for gate, created as one act.

    It is FLAT rather than nesting the issue's fields under an `issue` member, unlike `UpdateIssueRequest`'s `patch`: a
    patch has to distinguish a member that is absent from one set to its zero value, and a create has no such
    distinction to make — an absent member is the workspace default, which is the same answer a nested object would have
    given.

    The issue members mirror `ApplyCreateItem` exactly, minus that schema's two plan-only members (`key` and
    `metadata_refs`, which name items of a request this operation has only one of). What this adds is the edge
    vocabulary that operation moves into `dep_add` items: `parent_id`, `inherit_labels_from_parent`, `dependencies` and
    `waits_for`.

        Attributes:
            actor (str): Who is creating the issue. `ClaimRequest.actor`'s rules exactly: the server trims it, then refuses
                an empty result, anything longer than 256 BYTES (the `maxLength` above counts characters — the byte limit is the
                binding one), and any control character including newline. The value reaches the created edges' author column,
                the history entry's attribution and the storage commit message, so an unvalidated newline would forge audit-
                trail lines.

                It is NOT the issue's `created_by`, which this operation does not publish: this is the caller-asserted
                provenance of the ACT, and the row's own author column is left to the implementation.
            title (str): The issue's title. Must not be blank after trimming.
            id (str | Unset): An explicit id for the new row, CREATE-ONLY: an id that already names a stored row is a `409`
                `already_exists` and nothing is written — never an adoption and never an overwrite. It is checked against the
                workspace's configured issue prefix unless `force_id_prefix` is set. Absent is the ordinary case and the server
                mints one.
            description (str | Unset):
            design (str | Unset):
            acceptance_criteria (str | Unset):
            notes (str | Unset):
            issue_type (str | Unset): Issue type. Spelled `issue_type` rather than `type`, matching the member `Issue`
                carries, and validated against the built-ins plus the workspace's configured custom types by the ROLE — this
                server cannot read that vocabulary without a transaction, so it checks only what this schema declares and an
                unknown one arrives as a `400`.

                SEND ONE. The member is optional in this schema and the role validates the EMPTY type against the same
                vocabulary as any other, where it is neither a built-in nor a configured type — so an omitted `issue_type` is
                refused with everything else the request asked for. It stays optional because the vocabulary belongs to the
                workspace and a deployment may configure a default this server cannot read, but it is not optional in practice
                on any workspace shipped today. `POST /v0/beads/issues:batchCreate` has the same property and does not say so,
                which is why this member does.
            status (str | Unset): The status the issue is created in, from this workspace's own configured vocabulary.
                Absent means the workspace's own default, which is `open` today — unlike `issue_type`, the role fills this one
                in before it validates.
            priority (int | Unset): 0 is P0/critical. Absent means the workspace default.
            assignee (str | Unset):
            owner (str | Unset): The human owner, which is a different member from `assignee`: the assignee is who is
                working it now, the owner is who it is attributed to.
            labels (list[str] | Unset): The complete label set the issue is created with. Authoritative, not a patch — a
                create has nothing to add to. `inherit_labels_from_parent` adds the parent's labels on top of it.
            estimated_minutes (int | Unset): An estimate in minutes. Absent leaves it unset. NOT nullable, unlike
                `IssuePatchBody.estimated_minutes`: a create has nothing to clear, so `null` here would be a second spelling of
                omission and is a `400`.
            external_ref (str | Unset): e.g. `gh-9`. Not nullable, for `estimated_minutes`' reason.
            due_at (datetime.datetime | Unset): RFC 3339. Not nullable, for `estimated_minutes`' reason.
            defer_until (datetime.datetime | Unset): RFC 3339. The issue is hidden from ready work until then. Not nullable,
                for `estimated_minutes`' reason.
            sender (str | Unset): Who sent this, for the message-shaped rows an orchestrator creates. Stored verbatim and
                interpreted by nothing on this surface.
            metadata (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
                because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
                string, and a client must not decode it as one.

                Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
                and holds null. Those are different states and this surface reports both.
            ephemeral (bool | Unset): Creates the issue on the EPHEMERAL plane rather than the durable one, exactly as it
                does for `POST /v0/beads/issues:batchApply`. Mutually exclusive with `no_history`. Default: False.
            no_history (bool | Unset): Creates the issue on the ephemeral plane WITHOUT history, and without the garbage
                collection an ordinary ephemeral row is eligible for. Mutually exclusive with `ephemeral`. Default: False.
            parent_id (str | Unset): Creates a typed `parent-child` edge from the new issue to this target. It must not
                duplicate an edge `dependencies` already spells; naming the same pair twice with two types is a `400`.
            inherit_labels_from_parent (bool | Unset): Copies the parent's labels onto the new issue at creation, on top of
                `labels`. It has no effect without `parent_id`.

                The DEFAULT IS FALSE and diverges from `bd create --parent`, whose default is to inherit. A wire caller sends
                what it means: this operation has no `--no-inherit-labels` to turn off, and a create that silently acquired
                labels the request never named would be a set the caller has to read back to learn. Default: False.
            dependencies (list[CreateIssueDependency] | Unset): The complete set of explicit edges created with the issue.
                Authoritative, not a patch. Every edge is written in the same transaction as the row, so an edge this request
                cannot write means no issue either.

                A TARGET NEED NOT BE A ROW THIS DATABASE HOLDS: an `external:` reference and an id belonging to another
                repository are legitimate targets, so only an absence this database can SEE is refused — `ApplyDepAddItem`'s
                rule, unchanged.
            waits_for (CreateIssueWaitsFor | Unset): A typed `waits-for` edge from the new issue to a spawner whose children
                gate it. It records a readiness primitive; it does not define scheduling or execution policy.

                IT IS A TYPED MEMBER HERE AND A METADATA BLOB ON `POST /v0/beads/issues:batchApply`, and the difference follows
                the ROLE rather than taste: `CreateRequest.WaitsFor` is a typed field that gets the gate defaulted and the "must
                not duplicate an explicit edge" check, while that operation's `dep_add` item is one generic edge with no typed
                field to reach. One spelling per operation, and each is its role's.
            force_id_prefix (bool | Unset): Permits an explicit `id` outside the workspace's configured issue prefix. It
                bypasses ONLY that check: it is not a force on the create-only guard, so an occupied id is still a `409`.
                Default: False.
    """

    actor: str
    title: str
    id: str | Unset = UNSET
    description: str | Unset = UNSET
    design: str | Unset = UNSET
    acceptance_criteria: str | Unset = UNSET
    notes: str | Unset = UNSET
    issue_type: str | Unset = UNSET
    status: str | Unset = UNSET
    priority: int | Unset = UNSET
    assignee: str | Unset = UNSET
    owner: str | Unset = UNSET
    labels: list[str] | Unset = UNSET
    estimated_minutes: int | Unset = UNSET
    external_ref: str | Unset = UNSET
    due_at: datetime.datetime | Unset = UNSET
    defer_until: datetime.datetime | Unset = UNSET
    sender: str | Unset = UNSET
    metadata: Any | Unset = UNSET
    ephemeral: bool | Unset = False
    no_history: bool | Unset = False
    parent_id: str | Unset = UNSET
    inherit_labels_from_parent: bool | Unset = False
    dependencies: list[CreateIssueDependency] | Unset = UNSET
    waits_for: CreateIssueWaitsFor | Unset = UNSET
    force_id_prefix: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        title = self.title

        id = self.id

        description = self.description

        design = self.design

        acceptance_criteria = self.acceptance_criteria

        notes = self.notes

        issue_type = self.issue_type

        status = self.status

        priority = self.priority

        assignee = self.assignee

        owner = self.owner

        labels: list[str] | Unset = UNSET
        if not isinstance(self.labels, Unset):
            labels = self.labels

        estimated_minutes = self.estimated_minutes

        external_ref = self.external_ref

        due_at: str | Unset = UNSET
        if not isinstance(self.due_at, Unset):
            due_at = self.due_at.isoformat()

        defer_until: str | Unset = UNSET
        if not isinstance(self.defer_until, Unset):
            defer_until = self.defer_until.isoformat()

        sender = self.sender

        metadata = self.metadata

        ephemeral = self.ephemeral

        no_history = self.no_history

        parent_id = self.parent_id

        inherit_labels_from_parent = self.inherit_labels_from_parent

        dependencies: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.dependencies, Unset):
            dependencies = []
            for dependencies_item_data in self.dependencies:
                dependencies_item = dependencies_item_data.to_dict()
                dependencies.append(dependencies_item)

        waits_for: dict[str, Any] | Unset = UNSET
        if not isinstance(self.waits_for, Unset):
            waits_for = self.waits_for.to_dict()

        force_id_prefix = self.force_id_prefix

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
                "title": title,
            }
        )
        if id is not UNSET:
            field_dict["id"] = id
        if description is not UNSET:
            field_dict["description"] = description
        if design is not UNSET:
            field_dict["design"] = design
        if acceptance_criteria is not UNSET:
            field_dict["acceptance_criteria"] = acceptance_criteria
        if notes is not UNSET:
            field_dict["notes"] = notes
        if issue_type is not UNSET:
            field_dict["issue_type"] = issue_type
        if status is not UNSET:
            field_dict["status"] = status
        if priority is not UNSET:
            field_dict["priority"] = priority
        if assignee is not UNSET:
            field_dict["assignee"] = assignee
        if owner is not UNSET:
            field_dict["owner"] = owner
        if labels is not UNSET:
            field_dict["labels"] = labels
        if estimated_minutes is not UNSET:
            field_dict["estimated_minutes"] = estimated_minutes
        if external_ref is not UNSET:
            field_dict["external_ref"] = external_ref
        if due_at is not UNSET:
            field_dict["due_at"] = due_at
        if defer_until is not UNSET:
            field_dict["defer_until"] = defer_until
        if sender is not UNSET:
            field_dict["sender"] = sender
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if ephemeral is not UNSET:
            field_dict["ephemeral"] = ephemeral
        if no_history is not UNSET:
            field_dict["no_history"] = no_history
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if inherit_labels_from_parent is not UNSET:
            field_dict["inherit_labels_from_parent"] = inherit_labels_from_parent
        if dependencies is not UNSET:
            field_dict["dependencies"] = dependencies
        if waits_for is not UNSET:
            field_dict["waits_for"] = waits_for
        if force_id_prefix is not UNSET:
            field_dict["force_id_prefix"] = force_id_prefix

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.create_issue_dependency import CreateIssueDependency  # noqa: PLC0415
        from ..models.create_issue_waits_for import CreateIssueWaitsFor  # noqa: PLC0415

        d = dict(src_dict)
        actor = d.pop("actor")

        title = d.pop("title")

        id = d.pop("id", UNSET)

        description = d.pop("description", UNSET)

        design = d.pop("design", UNSET)

        acceptance_criteria = d.pop("acceptance_criteria", UNSET)

        notes = d.pop("notes", UNSET)

        issue_type = d.pop("issue_type", UNSET)

        status = d.pop("status", UNSET)

        priority = d.pop("priority", UNSET)

        assignee = d.pop("assignee", UNSET)

        owner = d.pop("owner", UNSET)

        labels = cast(list[str], d.pop("labels", UNSET))

        estimated_minutes = d.pop("estimated_minutes", UNSET)

        external_ref = d.pop("external_ref", UNSET)

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

        sender = d.pop("sender", UNSET)

        metadata = d.pop("metadata", UNSET)

        ephemeral = d.pop("ephemeral", UNSET)

        no_history = d.pop("no_history", UNSET)

        parent_id = d.pop("parent_id", UNSET)

        inherit_labels_from_parent = d.pop("inherit_labels_from_parent", UNSET)

        _dependencies = d.pop("dependencies", UNSET)
        dependencies: list[CreateIssueDependency] | Unset = UNSET
        if _dependencies is not UNSET:
            dependencies = []
            for dependencies_item_data in _dependencies:
                dependencies_item = CreateIssueDependency.from_dict(dependencies_item_data)

                dependencies.append(dependencies_item)

        _waits_for = d.pop("waits_for", UNSET)
        waits_for: CreateIssueWaitsFor | Unset
        if isinstance(_waits_for, Unset):
            waits_for = UNSET
        else:
            waits_for = CreateIssueWaitsFor.from_dict(_waits_for)

        force_id_prefix = d.pop("force_id_prefix", UNSET)

        create_issue_request = cls(
            actor=actor,
            title=title,
            id=id,
            description=description,
            design=design,
            acceptance_criteria=acceptance_criteria,
            notes=notes,
            issue_type=issue_type,
            status=status,
            priority=priority,
            assignee=assignee,
            owner=owner,
            labels=labels,
            estimated_minutes=estimated_minutes,
            external_ref=external_ref,
            due_at=due_at,
            defer_until=defer_until,
            sender=sender,
            metadata=metadata,
            ephemeral=ephemeral,
            no_history=no_history,
            parent_id=parent_id,
            inherit_labels_from_parent=inherit_labels_from_parent,
            dependencies=dependencies,
            waits_for=waits_for,
            force_id_prefix=force_id_prefix,
        )

        return create_issue_request
