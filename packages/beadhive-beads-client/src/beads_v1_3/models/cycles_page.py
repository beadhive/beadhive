from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.cycle import Cycle


T = TypeVar("T", bound="CyclesPage")


@_attrs_define
class CyclesPage:
    """
    Attributes:
        items (list[Cycle]): Empty array (never null) when the workspace has no cycles. Its LENGTH is the total: a cycle
            whose members this workspace cannot describe is still counted here, so the number cannot shrink because a row
            went missing.
        has_more (bool): Always false in v0: this operation takes no limit, so the report is never truncated. Present so
            that adding a bound later is additive.
    """

    items: list[Cycle]
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
        from ..models.cycle import Cycle  # noqa: PLC0415

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = Cycle.from_dict(items_item_data)

            items.append(items_item)

        has_more = d.pop("has_more")

        cycles_page = cls(
            items=items,
            has_more=has_more,
        )

        cycles_page.additional_properties = d
        return cycles_page

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
