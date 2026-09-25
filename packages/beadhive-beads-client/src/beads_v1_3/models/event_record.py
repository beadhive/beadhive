from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.event_record_comment import EventRecordComment
    from ..models.event_record_dep import EventRecordDep
    from ..models.event_record_issue_type_0 import EventRecordIssueType0


T = TypeVar("T", bound="EventRecord")


@_attrs_define
class EventRecord:
    """One record of the durable events journal: a single committed issue mutation, as a replaying consumer receives it.

    THIS IS THE CLI'S RECORD. It is pinned to the same Go struct `bd events tail` and `bd events export` marshal one per
    line, so the JSONL a consumer reads from stdout and the elements of an `EventsPage.records` array are the same bytes
    for the same row. A committed golden fixture pins that encoding field by field.

    `issue` is the full issue state AFTER the mutation and is ALWAYS PRESENT, carrying the literal `null` on a delete —
    where there is no surviving row to describe. That is the one place this document's general "treat null as absent"
    rule does not apply to a member's meaning: a consumer must be able to tell a delete from a payload the server failed
    to record, so the member is emitted rather than omitted. `dep` and `comment` are the opposite: they are ABSENT on
    the ops that have no such half, because their absence says the op has none, not that one was empty.

        Attributes:
            seq (int): Counter-assigned inside the mutation's own transaction: gapless, strictly increasing in commit order,
                never reused and never reset. This is the value to pass back as `since`. It is scoped to ONE replica — see the
                operation description.
            ts (str): UTC insert time, stamped inside the committing transaction and normalized to RFC 3339. It is NOT
                monotone in `seq`: two writers against one SQL server, or a clock stepped back by NTP, can commit an earlier
                `seq` with a later timestamp. Order by `seq`, never by this.
            op (str): What was done: `create`, `update`, `close`, `delete`, `dep_add`, `dep_remove` or `comment`. The set is
                closed in v0; a client MUST default-branch on an unknown value rather than fail, so that adding one stays
                additive.
            issue_id (str): The mutated issue's canonical id.
            issue (EventRecordIssueType0 | None): The full issue state after the mutation — the same object shape `Issue`
                describes — or `null` on a delete. Always present.
            actor (str | Unset): The acting identity that performed the mutation, as resolved for the audit-events table; on
                a `comment` record it is the comment's author (the same value as `comment.author`). Absent when the mutation
                path has no actor — derived maintenance, deletes (other than a rename's synthetic `delete` record), and records
                written before the journal recorded actors. An absent actor is never user attribution: read it as
                system/unknown, not as a conflicting writer.
            dep (EventRecordDep | Unset): On `dep_add` and `dep_remove` only: `{"kind","target","metadata"}` for the edge.
                Absent on every other op.

                `metadata` differs in PROVENANCE between the two: on `dep_add` it is the value being written as the caller
                supplied it, on `dep_remove` it is the stored column read back just before the delete. The two can differ byte
                for byte while meaning the same thing, so compare parsed values rather than strings.
            comment (EventRecordComment | Unset): On `comment` only: `{"id","author","text","created_at","source"}`. Absent
                on every other op.
    """

    seq: int
    ts: str
    op: str
    issue_id: str
    issue: EventRecordIssueType0 | None
    actor: str | Unset = UNSET
    dep: EventRecordDep | Unset = UNSET
    comment: EventRecordComment | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.event_record_issue_type_0 import EventRecordIssueType0  # noqa: PLC0415

        seq = self.seq

        ts = self.ts

        op = self.op

        issue_id = self.issue_id

        issue: dict[str, Any] | None
        if isinstance(self.issue, EventRecordIssueType0):
            issue = self.issue.to_dict()
        else:
            issue = self.issue

        actor = self.actor

        dep: dict[str, Any] | Unset = UNSET
        if not isinstance(self.dep, Unset):
            dep = self.dep.to_dict()

        comment: dict[str, Any] | Unset = UNSET
        if not isinstance(self.comment, Unset):
            comment = self.comment.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "seq": seq,
                "ts": ts,
                "op": op,
                "issue_id": issue_id,
                "issue": issue,
            }
        )
        if actor is not UNSET:
            field_dict["actor"] = actor
        if dep is not UNSET:
            field_dict["dep"] = dep
        if comment is not UNSET:
            field_dict["comment"] = comment

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.event_record_comment import EventRecordComment  # noqa: PLC0415
        from ..models.event_record_dep import EventRecordDep  # noqa: PLC0415
        from ..models.event_record_issue_type_0 import EventRecordIssueType0  # noqa: PLC0415

        d = dict(src_dict)
        seq = d.pop("seq")

        ts = d.pop("ts")

        op = d.pop("op")

        issue_id = d.pop("issue_id")

        def _parse_issue(data: object) -> EventRecordIssueType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                issue_type_0 = EventRecordIssueType0.from_dict(data)

                return issue_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EventRecordIssueType0 | None, data)

        issue = _parse_issue(d.pop("issue"))

        actor = d.pop("actor", UNSET)

        _dep = d.pop("dep", UNSET)
        dep: EventRecordDep | Unset
        if isinstance(_dep, Unset):
            dep = UNSET
        else:
            dep = EventRecordDep.from_dict(_dep)

        _comment = d.pop("comment", UNSET)
        comment: EventRecordComment | Unset
        if isinstance(_comment, Unset):
            comment = UNSET
        else:
            comment = EventRecordComment.from_dict(_comment)

        event_record = cls(
            seq=seq,
            ts=ts,
            op=op,
            issue_id=issue_id,
            issue=issue,
            actor=actor,
            dep=dep,
            comment=comment,
        )

        event_record.additional_properties = d
        return event_record

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
