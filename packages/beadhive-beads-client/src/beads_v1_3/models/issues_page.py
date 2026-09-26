from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.issue_with_counts import IssueWithCounts


T = TypeVar("T", bound="IssuesPage")


@_attrs_define
class IssuesPage:
    """
    Attributes:
        items (list[IssueWithCounts]): Empty array (never null) when nothing matches.
        has_more (bool):
        next_cursor (str | Unset): Present if and only if `has_more` is true. Pass it back verbatim as `cursor` to fetch
            the next page. Opaque and server-private.
    """

    items: list[IssueWithCounts]
    has_more: bool
    next_cursor: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        has_more = self.has_more

        next_cursor = self.next_cursor

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
                "has_more": has_more,
            }
        )
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue_with_counts import IssueWithCounts  # noqa: PLC0415

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = IssueWithCounts.from_dict(items_item_data)

            items.append(items_item)

        has_more = d.pop("has_more")

        next_cursor = d.pop("next_cursor", UNSET)

        issues_page = cls(
            items=items,
            has_more=has_more,
            next_cursor=next_cursor,
        )

        issues_page.additional_properties = d
        return issues_page

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
