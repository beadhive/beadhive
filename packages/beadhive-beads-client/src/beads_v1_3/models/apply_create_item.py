from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.apply_create_item_metadata_refs import ApplyCreateItemMetadataRefs


T = TypeVar("T", bound="ApplyCreateItem")


@_attrs_define
class ApplyCreateItem:
    """Creates one issue and optionally NAMES it, so later items can reach the row without knowing an id the request has
    not minted yet.

    It publishes the whole create vocabulary rather than `POST /v0/beads/issues:batchCreate`'s narrow one, and the
    additions are the point: `status`, `sender`, `metadata`, `ephemeral` and `no_history` are the members whose absence
    there makes that operation unusable for a caller composing a real plan.

    THE EDGES ARE NOT HERE. An issue's dependencies and its parent are `dep_add` ITEMS, so the order of every edge in
    the request is total and there is exactly one spelling for an edge. An item carrying comments or dependencies on the
    issue is a `400`.

    `metadata` is the issue's own metadata document and must be a JSON OBJECT where it is present at all. It is stored
    as sent; the resolved ids `metadata_refs` splices are written over its top-level keys after every id in the request
    exists.

        Attributes:
            title (str):
            key (str | Unset): This item's name inside the request. OPTIONAL — an item nothing refers to needs no name — and
                unique across the request's create items; a repeat is a `400`. It is what a later `Ref.key` resolves to, and the
                response's `keys` member is where the id it was bound to is read.
            id (str | Unset): An explicit id for the new row, CREATE-ONLY: an id that already names a stored row is a `409`
                `already_exists` and the whole request is refused — never an adoption and never an overwrite. To act on a row
                that already exists, send an `update` item referencing it by `{"id": …}`. The id is checked against the
                workspace's configured issue prefix unless the request sets `force_id_prefix`.

                Absent is the ordinary case and the server mints one. This is the member `POST /v0/beads/issues:batchCreate`
                deliberately does not publish, which is why that operation can never adopt or overwrite a stored row and this
                one can be refused for trying.
            description (str | Unset):
            design (str | Unset):
            acceptance_criteria (str | Unset):
            notes (str | Unset):
            issue_type (str | Unset): Issue type. Spelled `issue_type` rather than `type`, matching the member `Issue`
                carries, and validated against the built-ins plus the workspace's configured custom types by the ROLE — this
                server cannot read that vocabulary without a transaction, so it checks only what this schema declares and an
                unknown one arrives as a `400`.
            status (str | Unset): The status the issue is created in, from this workspace's own configured vocabulary.
                Absent means the workspace default.
            priority (int | Unset): 0 is P0/critical. Absent means the workspace default.
            assignee (str | Unset):
            owner (str | Unset): The human owner, which is a different member from `assignee`: the assignee is who is
                working it now, the owner is who it is attributed to.
            labels (list[str] | Unset): The complete label set the issue is created with. Authoritative, not a patch — a
                create has nothing to add to.
            estimated_minutes (int | Unset): An estimate in minutes. Absent leaves it unset.
            external_ref (str | Unset):
            due_at (datetime.datetime | Unset): RFC 3339.
            defer_until (datetime.datetime | Unset): RFC 3339. The issue is hidden from ready work until then.
            sender (str | Unset): Who sent this, for the message-shaped rows a plan creates. Stored verbatim and interpreted
                by nothing on this surface.
            metadata (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
                because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
                string, and a client must not decode it as one.

                Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
                and holds null. Those are different states and this surface reports both.
            ephemeral (bool | Unset): Creates the issue on the EPHEMERAL plane rather than the durable one. Per item,
                exactly as it is for `POST /v0/beads/issues:batchCreate`, so one request may create durable issues and ephemeral
                ones together.

                The two planes hold their edges in different tables, so a `dep_add` between two rows this request creates on
                OPPOSITE planes is refused with everything else the request asked for. Mutually exclusive with `no_history`.
                Default: False.
            no_history (bool | Unset): Creates the issue on the ephemeral plane WITHOUT history, and without the garbage
                collection an ordinary ephemeral row is eligible for. Mutually exclusive with `ephemeral`. Default: False.
            metadata_refs (ApplyCreateItemMetadataRefs | Unset): Splices resolved ids into this issue's metadata: each entry
                writes the id its `Ref` resolves to as the WHOLE VALUE of one top-level metadata key.

                IT IS THE ONE PLACE A KEY MAY REACH FORWARD, or name this item's own `key` — see the operation's description. A
                ref here that names a key NO item declares is still a `400`.

                IT IS A TYPED MAP, NOT TEMPLATING. A `${key}` placeholder inside a JSON string would have no escape for a
                literal dollar-brace, would collide with every other templating language a caller's own values might carry, and
                could not be type-checked at all. This is one key, one whole value, one level deep.

                The splice is applied AFTER the row is created, so a consumer of the event stream sees a create and then an
                update on the spliced row.
    """

    title: str
    key: str | Unset = UNSET
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
    metadata_refs: ApplyCreateItemMetadataRefs | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        key = self.key

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

        metadata_refs: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata_refs, Unset):
            metadata_refs = self.metadata_refs.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "title": title,
            }
        )
        if key is not UNSET:
            field_dict["key"] = key
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
        if metadata_refs is not UNSET:
            field_dict["metadata_refs"] = metadata_refs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.apply_create_item_metadata_refs import (
            ApplyCreateItemMetadataRefs,  # noqa: PLC0415
        )

        d = dict(src_dict)
        title = d.pop("title")

        key = d.pop("key", UNSET)

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

        _metadata_refs = d.pop("metadata_refs", UNSET)
        metadata_refs: ApplyCreateItemMetadataRefs | Unset
        if isinstance(_metadata_refs, Unset):
            metadata_refs = UNSET
        else:
            metadata_refs = ApplyCreateItemMetadataRefs.from_dict(_metadata_refs)

        apply_create_item = cls(
            title=title,
            key=key,
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
            metadata_refs=metadata_refs,
        )

        return apply_create_item
