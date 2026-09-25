from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.apply_label_patch import ApplyLabelPatch
    from ..models.apply_metadata_patch import ApplyMetadataPatch


T = TypeVar("T", bound="ApplyPatchBody")


@_attrs_define
class ApplyPatchBody:
    """The fields an `update` item writes. Every member is optional and PRESENCE is the signal: a member present is
    written, a member absent is untouched. An empty object is a `400` — a write that writes nothing is a client bug.

    It mirrors `IssuePatchBody` member for member and diverges in exactly two places now that `PATCH
    /v0/beads/issues/{id}` publishes `status`, `assignee` and the same `metadata` algebra.

    `owner` is published here and not there, which is an accident of order rather than a decision: nothing has asked for
    it on the single patch.

    `labels` is a full patch rather than a complete replacement, and that one is a real difference: a plan has to be
    able to REMOVE one label without knowing the rest of the set, because it edits a set it did not compose. A caller
    patching one row it just read already knows the set.

    `parent_id` is deliberately absent, and its absence is this operation's one-edge-one-spelling rule: a parent is a
    `dep_add` item of type `parent-child`, so the order of every edge in the request stays total. The single patch has
    no ordering to express and publishes it directly. `persistence` is absent from both — moving a row between planes
    mid-plan is a different act from writing its fields, and nothing has asked for it here.

        Attributes:
            title (str | Unset): Must not be blank after trimming; the length bound is what the column holds.
            description (str | Unset):
            design (str | Unset):
            acceptance_criteria (str | Unset):
            notes (str | Unset): Replaces the notes. Mutually exclusive with `append_notes`; sending both is a `400`.
            append_notes (str | Unset): Appends to the notes rather than replacing them. Mutually exclusive with `notes`.
            priority (int | Unset):
            issue_type (str | Unset): The issue type, from this workspace's own configured vocabulary. A type outside it is
                refused by the ROLE and reaches the client as a `400`.
            status (str | Unset): The issue's status, from this workspace's own configured vocabulary.

                A STATUS THAT CROSSES INTO THE DONE CATEGORY ANSWERS TO CLOSE POLICY: the item is refused with `409
                not_closable` for open children or a live blocker unless `force_close_policy` is set. A done-to-done change and
                a move OUT of the done category are unaffected — which is how a plan reopens a row, since there is no reopen
                item.
            assignee (str | Unset): The assignee. A transfer away from a live foreign in-progress owner is refused with `409
                already_claimed` unless `force_assignee_transfer` is set or `expected_assignee` matched.
            owner (str | Unset):
            labels (ApplyLabelPatch | Unset): An ordered label edit: `replace` first, then `add`, then `remove`, so REMOVAL
                WINS when the same label appears in more than one member.

                It is the full patch rather than `IssuePatchBody.labels`' complete replacement because a plan edits a set it did
                not compose: replacing would mean reading the labels back first, and the read this operation exists to avoid is
                exactly that one.

                Repetition is free in both directions — a label named twice in one member is applied once, and removing a label
                the issue does not carry is a no-op. An EMPTY-STRING entry is dropped rather than refused: a label row holding
                "" renders as nothing and matches nothing, so refusing the whole request for one stray entry would fail an
                otherwise-good edit.
            estimated_minutes (int | None | Unset): Explicit `null` CLEARS the estimate.
            external_ref (None | str | Unset): Explicit `null` CLEARS the reference.
            due_at (datetime.datetime | None | Unset): RFC 3339. Explicit `null` CLEARS the due date.
            defer_until (datetime.datetime | None | Unset): RFC 3339. Explicit `null` CLEARS the deferral.
            metadata (ApplyMetadataPatch | Unset): A metadata edit. `replace` is mutually exclusive with the other three;
                without it the edits apply as `merge`, then `set` in key order, then `unset`, so UNSETTING A KEY WINS over
                setting or merging it. Sending `replace` beside any of the others is a `400`.

                `replace` replaces the whole document. Present holding `null`, `{}` or an empty value CLEARS metadata — and
                clearing STORES THE EMPTY JSON DOCUMENT rather than SQL null, so "created with no metadata" and "given metadata
                and then cleared" are the same stored value; a reader must treat absent, empty and `{}` as one value on the way
                out. `merge` must be a nonempty JSON OBJECT and is merged into the current document.
    """

    title: str | Unset = UNSET
    description: str | Unset = UNSET
    design: str | Unset = UNSET
    acceptance_criteria: str | Unset = UNSET
    notes: str | Unset = UNSET
    append_notes: str | Unset = UNSET
    priority: int | Unset = UNSET
    issue_type: str | Unset = UNSET
    status: str | Unset = UNSET
    assignee: str | Unset = UNSET
    owner: str | Unset = UNSET
    labels: ApplyLabelPatch | Unset = UNSET
    estimated_minutes: int | None | Unset = UNSET
    external_ref: None | str | Unset = UNSET
    due_at: datetime.datetime | None | Unset = UNSET
    defer_until: datetime.datetime | None | Unset = UNSET
    metadata: ApplyMetadataPatch | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        description = self.description

        design = self.design

        acceptance_criteria = self.acceptance_criteria

        notes = self.notes

        append_notes = self.append_notes

        priority = self.priority

        issue_type = self.issue_type

        status = self.status

        assignee = self.assignee

        owner = self.owner

        labels: dict[str, Any] | Unset = UNSET
        if not isinstance(self.labels, Unset):
            labels = self.labels.to_dict()

        estimated_minutes: int | None | Unset
        if isinstance(self.estimated_minutes, Unset):
            estimated_minutes = UNSET
        else:
            estimated_minutes = self.estimated_minutes

        external_ref: None | str | Unset
        if isinstance(self.external_ref, Unset):
            external_ref = UNSET
        else:
            external_ref = self.external_ref

        due_at: None | str | Unset
        if isinstance(self.due_at, Unset):
            due_at = UNSET
        elif isinstance(self.due_at, datetime.datetime):
            due_at = self.due_at.isoformat()
        else:
            due_at = self.due_at

        defer_until: None | str | Unset
        if isinstance(self.defer_until, Unset):
            defer_until = UNSET
        elif isinstance(self.defer_until, datetime.datetime):
            defer_until = self.defer_until.isoformat()
        else:
            defer_until = self.defer_until

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if title is not UNSET:
            field_dict["title"] = title
        if description is not UNSET:
            field_dict["description"] = description
        if design is not UNSET:
            field_dict["design"] = design
        if acceptance_criteria is not UNSET:
            field_dict["acceptance_criteria"] = acceptance_criteria
        if notes is not UNSET:
            field_dict["notes"] = notes
        if append_notes is not UNSET:
            field_dict["append_notes"] = append_notes
        if priority is not UNSET:
            field_dict["priority"] = priority
        if issue_type is not UNSET:
            field_dict["issue_type"] = issue_type
        if status is not UNSET:
            field_dict["status"] = status
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
        if metadata is not UNSET:
            field_dict["metadata"] = metadata

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.apply_label_patch import ApplyLabelPatch  # noqa: PLC0415
        from ..models.apply_metadata_patch import ApplyMetadataPatch  # noqa: PLC0415

        d = dict(src_dict)
        title = d.pop("title", UNSET)

        description = d.pop("description", UNSET)

        design = d.pop("design", UNSET)

        acceptance_criteria = d.pop("acceptance_criteria", UNSET)

        notes = d.pop("notes", UNSET)

        append_notes = d.pop("append_notes", UNSET)

        priority = d.pop("priority", UNSET)

        issue_type = d.pop("issue_type", UNSET)

        status = d.pop("status", UNSET)

        assignee = d.pop("assignee", UNSET)

        owner = d.pop("owner", UNSET)

        _labels = d.pop("labels", UNSET)
        labels: ApplyLabelPatch | Unset
        if isinstance(_labels, Unset):
            labels = UNSET
        else:
            labels = ApplyLabelPatch.from_dict(_labels)

        def _parse_estimated_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        estimated_minutes = _parse_estimated_minutes(d.pop("estimated_minutes", UNSET))

        def _parse_external_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        external_ref = _parse_external_ref(d.pop("external_ref", UNSET))

        def _parse_due_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                due_at_type_0 = datetime.datetime.fromisoformat(data)

                return due_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        due_at = _parse_due_at(d.pop("due_at", UNSET))

        def _parse_defer_until(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                defer_until_type_0 = datetime.datetime.fromisoformat(data)

                return defer_until_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        defer_until = _parse_defer_until(d.pop("defer_until", UNSET))

        _metadata = d.pop("metadata", UNSET)
        metadata: ApplyMetadataPatch | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = ApplyMetadataPatch.from_dict(_metadata)

        apply_patch_body = cls(
            title=title,
            description=description,
            design=design,
            acceptance_criteria=acceptance_criteria,
            notes=notes,
            append_notes=append_notes,
            priority=priority,
            issue_type=issue_type,
            status=status,
            assignee=assignee,
            owner=owner,
            labels=labels,
            estimated_minutes=estimated_minutes,
            external_ref=external_ref,
            due_at=due_at,
            defer_until=defer_until,
            metadata=metadata,
        )

        return apply_patch_body
