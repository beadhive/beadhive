from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.event_record import EventRecord


T = TypeVar("T", bound="EventsPage")


@_attrs_define
class EventsPage:
    """One page of the journal plus the position of its end.

    THERE IS NO `has_more`, and that is deliberate rather than an omission. Every other page on this surface reports
    truncation with a boolean because its ordering is a query's; here the answer is a number the client already needs
    for its next request. Compare the last record's `seq` with `head`: equal means caught up, lower means keep reading.
    A full page proves nothing either way, and a `has_more` computed from the limit would be a second, weaker way to ask
    the same question.

        Attributes:
            records (list[EventRecord]): Records with `seq` strictly greater than the requested `since`, in ASCENDING `seq`
                order and contiguous — a gap in the retained window is a 410, never a quietly shortened list. Empty array (never
                null) when the caller is caught up.
            head (int): The highest `seq` this journal has ever assigned, read in the same transaction as the records above.

                It is the journal's HISTORY, not its contents: pruning deletes rows and never touches the counter, so a fully
                pruned journal still reports the head it reached. `0` means no mutation has ever been journaled here — which,
                given that a disabled journal is refused with 409 rather than answered, means an enabled journal on a workspace
                that has not been written to yet.

                Because it is read after the rows within one transaction, it is always greater than or equal to the last
                record's `seq`; it may be greater simply because a mutation committed while the page was being read, which is
                the ordinary signal to poll again.
    """

    records: list[EventRecord]
    head: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        records = []
        for records_item_data in self.records:
            records_item = records_item_data.to_dict()
            records.append(records_item)

        head = self.head

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "records": records,
                "head": head,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.event_record import EventRecord  # noqa: PLC0415

        d = dict(src_dict)
        records = []
        _records = d.pop("records")
        for records_item_data in _records:
            records_item = EventRecord.from_dict(records_item_data)

            records.append(records_item)

        head = d.pop("head")

        events_page = cls(
            records=records,
            head=head,
        )

        events_page.additional_properties = d
        return events_page

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
