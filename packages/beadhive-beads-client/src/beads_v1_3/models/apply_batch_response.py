from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.apply_batch_response_keys import ApplyBatchResponseKeys
    from ..models.apply_item_result import ApplyItemResult


T = TypeVar("T", bound="ApplyBatchResponse")


@_attrs_define
class ApplyBatchResponse:
    """
    Attributes:
        keys (ApplyBatchResponseKeys): Each create item's `key` mapped to the id it was bound to. It is the one fact the
            request cannot carry and every caller needs.

            It carries only the keys the request NAMED: an unnamed create item is in `items` and not here. A request whose
            create items named nothing answers with an empty object, never `null`.
        items (list[ApplyItemResult]): One entry per requested item, in REQUEST ORDER. Never null and never shorter than
            the request: a batch that could not apply every item applied none, so there is no index with nothing to put at
            it.

            There is no `has_more` and no `next_cursor`. This is not a page — the client already knows how many items it
            sent.
    """

    keys: ApplyBatchResponseKeys
    items: list[ApplyItemResult]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        keys = self.keys.to_dict()

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "keys": keys,
                "items": items,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.apply_batch_response_keys import ApplyBatchResponseKeys  # noqa: PLC0415
        from ..models.apply_item_result import ApplyItemResult  # noqa: PLC0415

        d = dict(src_dict)
        keys = ApplyBatchResponseKeys.from_dict(d.pop("keys"))

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = ApplyItemResult.from_dict(items_item_data)

            items.append(items_item)

        apply_batch_response = cls(
            keys=keys,
            items=items,
        )

        apply_batch_response.additional_properties = d
        return apply_batch_response

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
