from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue_with_counts import IssueWithCounts


T = TypeVar("T", bound="QueryPage")


@_attrs_define
class QueryPage:
    """A page of query results. It is `ReadyPage`'s shape rather than `IssuesPage`'s, and the missing member is the point:
    a page of this operation carries no `next_cursor`, because a cursor is a keyset position in a database order and a
    predicate query's matching set is assembled outside the database.

        Attributes:
            items (list[IssueWithCounts]): Empty array (never null) when the expression matched nothing.
            has_more (bool): True when `limit` truncated the result. It is exact for every expression, including the ones
                evaluated outside the database: those are matched against every candidate row, so the count of matches is known
                before the page is cut. There is no cursor — raise `limit` or narrow the expression.
    """

    items: list[IssueWithCounts]
    has_more: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        has_more = self.has_more

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
                "has_more": has_more,
            }
        )

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

        query_page = cls(
            items=items,
            has_more=has_more,
        )

        query_page.additional_properties = d
        return query_page

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
