from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.dependency_edge import DependencyEdge


T = TypeVar("T", bound="AddDependenciesResponse")


@_attrs_define
class AddDependenciesResponse:
    """
    Attributes:
        added (list[DependencyEdge]): The request's edges, in REQUEST ORDER. It echoes the request because all-or-
            nothing means it is either every edge or the call failed, so a caller reporting what landed reads the result and
            never has to know which of the two it is safe to read. An idempotent same-type re-add is echoed like any other
            edge; the response does not say which edges were genuinely new, because nothing a client does depends on that.

            Never null and never shorter than the request: a partial outcome does not exist on this operation.
    """

    added: list[DependencyEdge]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        added = []
        for added_item_data in self.added:
            added_item = added_item_data.to_dict()
            added.append(added_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "added": added,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.dependency_edge import DependencyEdge  # noqa: PLC0415

        d = dict(src_dict)
        added = []
        _added = d.pop("added")
        for added_item_data in _added:
            added_item = DependencyEdge.from_dict(added_item_data)

            added.append(added_item)

        add_dependencies_response = cls(
            added=added,
        )

        add_dependencies_response.additional_properties = d
        return add_dependencies_response

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
