from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateIssueDependency")


@_attrs_define
class CreateIssueDependency:
    """One edge created with the issue. It carries `reverse` where `BatchCreateDependency` does not, because that
    operation's items have no id a target could point back at and this one's issue does.

        Attributes:
            target_id (str): The other endpoint of the edge.
            type_ (str): The edge type, from the same OPEN vocabulary `Dependency.type` carries: checked for BEING a
                storable value, never for membership of a known-types list, so a workspace's own type passes.
            reverse (bool | Unset): Writes the edge from `target_id` TO the new issue rather than from it. It is what lets a
                create declare an edge that points INTO the row being minted — the id no caller could have spelled beforehand —
                and it is the member that makes `dependency_cycle` reachable on this operation at all. Default: False.
            metadata (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
                because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
                string, and a client must not decode it as one.

                Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
                and holds null. Those are different states and this surface reports both.
    """

    target_id: str
    type_: str
    reverse: bool | Unset = False
    metadata: Any | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        target_id = self.target_id

        type_ = self.type_

        reverse = self.reverse

        metadata = self.metadata

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "target_id": target_id,
                "type": type_,
            }
        )
        if reverse is not UNSET:
            field_dict["reverse"] = reverse
        if metadata is not UNSET:
            field_dict["metadata"] = metadata

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        target_id = d.pop("target_id")

        type_ = d.pop("type")

        reverse = d.pop("reverse", UNSET)

        metadata = d.pop("metadata", UNSET)

        create_issue_dependency = cls(
            target_id=target_id,
            type_=type_,
            reverse=reverse,
            metadata=metadata,
        )

        return create_issue_dependency
