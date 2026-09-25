from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.tree_node import TreeNode


T = TypeVar("T", bound="DependencyTreePage")


@_attrs_define
class DependencyTreePage:
    """
    Attributes:
        items (list[TreeNode]): The walked nodes in DEPTH-FIRST PRE-ORDER: a node appears before every node it led to,
            and a subtree is contiguous. Never null.

            It is empty only when a `status` filter matched nothing — the root is kept in a filtered answer solely as an
            ancestor of a match, never for its own sake. Without `status` the root is always the first element, which is
            what lets a client tell "this issue depends on nothing" from "this issue is not there" (a 404).
        has_more (bool): Always false in v0: this operation takes no limit, so the walk is bounded by `max_depth` rather
            than truncated after the fact. Present so that adding a bound later is additive.
    """

    items: list[TreeNode]
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
        from ..models.tree_node import TreeNode  # noqa: PLC0415

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = TreeNode.from_dict(items_item_data)

            items.append(items_item)

        has_more = d.pop("has_more")

        dependency_tree_page = cls(
            items=items,
            has_more=has_more,
        )

        dependency_tree_page.additional_properties = d
        return dependency_tree_page

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
