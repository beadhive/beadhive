from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.apply_metadata_patch import ApplyMetadataPatch


T = TypeVar("T", bound="IssuePatchBody")


@_attrs_define
class IssuePatchBody:
    """The fields to write. Every member is optional and PRESENCE is the signal: a member present is written, a member
    absent is untouched. An empty object is a `400` — a write that writes nothing is a client bug.

    This is a deliberate SUBSET of the fields an issue carries; the members it does not spell are future surface rather
    than oversights, and `updateIssue`'s own description says which and why.

    It now agrees with `ApplyPatchBody` on every member it publishes, and the two differ only in the SHAPE of two of
    them: `labels` is complete replacement here and an ordered add/remove/replace patch there, because that operation
    edits a set it did not compose. Everything else — down to the `metadata` algebra and the four nullable members — is
    one definition, so a caller cannot get a different answer for the same edit depending on which operation it sent.

        Attributes:
            title (str | Unset): The issue's title. Must not be blank after trimming; the length bound is what the column
                holds.
            description (str | Unset):
            design (str | Unset):
            acceptance_criteria (str | Unset):
            notes (str | Unset): Replaces the notes. Mutually exclusive with `append_notes`; sending both is a `400`.
            append_notes (str | Unset): Appends to the notes rather than replacing them. Mutually exclusive with `notes`.
            priority (int | Unset):
            issue_type (str | Unset): The issue type, from this workspace's own configured vocabulary. A type outside it is
                refused by the ROLE and reaches the client as a `400` — this server cannot read the vocabulary without a
                transaction, so it checks only what this schema declares.
            status (str | Unset): The issue's status, from this workspace's own configured vocabulary.

                A STATUS THAT CROSSES INTO THE DONE CATEGORY ANSWERS TO CLOSE POLICY: the update is refused with `409
                not_closable` for open children or a live blocker unless `force_close_policy` is set. A done-to-done change and
                a move OUT of the done category are unaffected.

                IT IS NOT A SECOND SPELLING OF `{id}:close` AND `{id}:reopen`. Those two carry semantics a status write has
                nowhere to put — the reason and session under first-close-wins, the done-status normalization, the
                `already_closed`/`already_open` idempotence flags — and they remain the operations to reach for when what you
                mean is "close this". This member is for the edit that moves a status ALONGSIDE other fields in one transaction,
                which is the thing two calls cannot do. `ApplyPatchBody.status` has meant exactly this since `issues:batchApply`
                landed.
            assignee (str | Unset): The assignee. A transfer away from a live foreign in-progress owner is refused with `409
                already_claimed` unless `force_assignee_transfer` is set or `expected_assignee` matched. Setting it to the empty
                string unassigns.

                `{id}:claim` remains the operation that ACQUIRES work: it carries its own eligibility rules and sets the status
                with the assignee in one act. This member is the raw write, fenced.
            parent_id (str | Unset): Replaces the issue's parents atomically: a nonempty value makes THAT issue the only
                parent, and an EMPTY STRING removes every parent-child edge the issue has. Labels are not inherited — that is a
                create-time choice (`CreateIssueRequest.inherit_labels_from_parent`) and a reparent does not re-run it.

                IT IS A GRAPH EDIT, and it earns the graph's refusals: a new parent this workspace holds no row for is a `400`,
                a pair that already carries an edge of another type is `409 dependency_exists`, and a move under the issue's own
                descendant is `409 dependency_cycle` — the PLAIN one, carrying no `issue_id`/`blocker_id`/
                `blocker_is_ancestor`, because the hierarchy refusal answers only to blocking edges and this member writes a
                `parent-child` edge. Naming the issue itself is a `400`. One call rather than a remove-then-add pair, which is
                the whole reason it is here: the two-call spelling leaves the issue parentless if the second call fails.
            labels (list[str] | Unset): COMPLETE REPLACEMENT of the label set. An empty array clears every label.

                It is the REPLACE half of the same ordered edit `ApplyPatchBody` spells as `labels.replace`, and
                `add_labels`/`remove_labels` are the other two. All three may travel together and are applied in that order —
                replace, then add, then remove — so REMOVAL WINS when one label appears in more than one of them. That is the
                role's own algebra, not this operation's arrangement of it.

                THE SHAPE DIFFERS FROM `ApplyPatchBody`'s, which nests the three under one `labels` object, and the difference
                is historical rather than meaningful. This member shipped as a bare array; nesting it now would RE-TYPE a
                published member, which is the one kind of change this document has no additive route for. Two flat siblings is
                the shape that could be added — and it is the shape `notes` and `append_notes` already use for the same
                replace/increment pair.
            add_labels (list[str] | Unset): Labels to add, applied AFTER any `labels` replacement.

                IT IS NOT MUTUALLY EXCLUSIVE WITH `labels`, and that is the difference from `append_notes`, which is. The role
                defines an order over all three label edits, so sending a replacement and an addition together has a defined
                result; notes have no such algebra, so there the two are a contradiction and are refused.

                IT IS WHY THIS PAIR EXISTS. A caller that reads a row, adds one label and writes the whole set back silently
                drops any label another writer added in between — and `bd label add` and every agent that tags work concurrently
                are exactly that caller. A replacement can only be composed safely by a writer that knows it is alone.

                Repetition is free: a label named twice is applied once, and adding one the issue already carries changes no
                labels. (Whether the RESPONSE reports `changed: false` is a fact about the whole patch — see `remove_labels`.)
                An EMPTY-STRING entry is DROPPED rather than refused — a label row carrying `""` renders as nothing and matches
                nothing, so writing one would only store junk, and refusing the whole update would let one stray entry fail an
                otherwise-good edit.
            remove_labels (list[str] | Unset): Labels to remove, applied AFTER `labels` and `add_labels`, so REMOVAL WINS
                over both.

                Removing a label the issue does not carry CHANGES NO LABELS; it is not a `404` and not a conflict. Whether the
                RESPONSE reports `changed: false` is a fact about the whole patch, not about this member — a request that also
                moved a title changed the row. The same repetition and empty-string rules as `add_labels` apply, and a value
                longer than the column is refused here as it is there — the length rule is about what a label may BE, not about
                whether this particular row happens to carry one.
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
    parent_id: str | Unset = UNSET
    labels: list[str] | Unset = UNSET
    add_labels: list[str] | Unset = UNSET
    remove_labels: list[str] | Unset = UNSET
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

        parent_id = self.parent_id

        labels: list[str] | Unset = UNSET
        if not isinstance(self.labels, Unset):
            labels = self.labels

        add_labels: list[str] | Unset = UNSET
        if not isinstance(self.add_labels, Unset):
            add_labels = self.add_labels

        remove_labels: list[str] | Unset = UNSET
        if not isinstance(self.remove_labels, Unset):
            remove_labels = self.remove_labels

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
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if labels is not UNSET:
            field_dict["labels"] = labels
        if add_labels is not UNSET:
            field_dict["add_labels"] = add_labels
        if remove_labels is not UNSET:
            field_dict["remove_labels"] = remove_labels
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

        parent_id = d.pop("parent_id", UNSET)

        labels = cast(list[str], d.pop("labels", UNSET))

        add_labels = cast(list[str], d.pop("add_labels", UNSET))

        remove_labels = cast(list[str], d.pop("remove_labels", UNSET))

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

        issue_patch_body = cls(
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
            parent_id=parent_id,
            labels=labels,
            add_labels=add_labels,
            remove_labels=remove_labels,
            estimated_minutes=estimated_minutes,
            external_ref=external_ref,
            due_at=due_at,
            defer_until=defer_until,
            metadata=metadata,
        )

        return issue_patch_body
