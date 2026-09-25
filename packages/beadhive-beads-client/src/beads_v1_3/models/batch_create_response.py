from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue import Issue


T = TypeVar("T", bound="BatchCreateResponse")


@_attrs_define
class BatchCreateResponse:
    """
    Attributes:
        items (list[Issue]): One entry per requested item, in REQUEST ORDER, each the stored issue with its generated id
            and its labels. Never null and never shorter than the request: a partial outcome does not exist on this
            operation.

            There is no `has_more` and no `next_cursor`. This is not a page — the client already knows how many items it
            sent — and publishing a paging envelope over a fixed-length answer would invite a client to look for a second
            page that can never exist.
    """

    items: list[Issue]
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
        from ..models.issue import Issue  # noqa: PLC0415

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = Issue.from_dict(items_item_data)

            items.append(items_item)

        batch_create_response = cls(
            items=items,
        )

        batch_create_response.additional_properties = d
        return batch_create_response

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
