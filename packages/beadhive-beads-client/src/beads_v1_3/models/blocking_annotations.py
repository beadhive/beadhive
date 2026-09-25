from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue_blocking import IssueBlocking


T = TypeVar("T", bound="BlockingAnnotations")


@_attrs_define
class BlockingAnnotations:
    """The blocking decoration of the named issues. It is NOT a page: this operation has no limit and no cursor, because
    the number of issues asked about is what bounds it.

        Attributes:
            items (list[IssueBlocking]): One entry per DISTINCT requested id, in the order the request first named it — so a
                client can zip this against the ids it sent. Empty array (never null) when the request named none.

                There is no `missing` beside it, unlike `DependencyEdges`: this operation probes no id's existence, so every
                requested id has an entry and an id that names nothing is simply bare.
    """

    items: list[IssueBlocking]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue_blocking import IssueBlocking  # noqa: PLC0415

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = IssueBlocking.from_dict(items_item_data)

            items.append(items_item)

        blocking_annotations = cls(
            items=items,
        )

        blocking_annotations.additional_properties = d
        return blocking_annotations

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
