from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.dependency import Dependency


T = TypeVar("T", bound="DependencyEdges")


@_attrs_define
class DependencyEdges:
    """The stored edges of the named issues, plus the ids that named nothing. It is NOT a page: this operation has no limit
    and no cursor, because the number of issues asked about is what bounds it.

        Attributes:
            items (list[Dependency]): Every matching edge, flattened across the named issues rather than keyed by source —
                the same flat array `bd dep list a b c --json` emits, so the two surfaces are one compatibility domain. Group by
                `issue_id` to recover the per-source view. Empty array (never null) when nothing matches.

                The order is by the named issues in the order the request named them, and within each issue by target id.
            missing (list[str]): The requested `issue_id` values that name neither an issue nor a wisp, in the order they
                were named. Empty array (never null) when every named issue exists.

                An id here contributes no `items`, and the absence of an id here is NOT a claim that it has edges — an issue
                that exists and depends on nothing is in neither list.
    """

    items: list[Dependency]
    missing: list[str]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        missing = self.missing

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
                "missing": missing,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.dependency import Dependency  # noqa: PLC0415

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = Dependency.from_dict(items_item_data)

            items.append(items_item)

        missing = cast(list[str], d.pop("missing"))

        dependency_edges = cls(
            items=items,
            missing=missing,
        )

        dependency_edges.additional_properties = d
        return dependency_edges

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
