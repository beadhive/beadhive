from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.cycle_member import CycleMember


T = TypeVar("T", bound="Cycle")


@_attrs_define
class Cycle:
    """One circular blocking dependency: its members in EDGE ORDER, so `members[i]` blocks on `members[i+1]` and the last
    member blocks on the first. The closing edge is implied and is not repeated as a final member.

    The rotation is canonical — the lowest id comes first — which is what makes two snapshots of an unchanged workspace
    comparable.

        Attributes:
            members (list[CycleMember]): The nodes, in edge order, starting at the lowest id. Never empty.
            partial (bool): True when at least one member has no `issue`. It is always present, including when false: a
                consumer must be able to read "this path is complete" from the answer rather than from the absence of a key.

                `members` is complete either way. This flag says the DESCRIPTIONS beside the ids are not.
    """

    members: list[CycleMember]
    partial: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        members = []
        for members_item_data in self.members:
            members_item = members_item_data.to_dict()
            members.append(members_item)

        partial = self.partial

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "members": members,
                "partial": partial,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cycle_member import CycleMember  # noqa: PLC0415

        d = dict(src_dict)
        members = []
        _members = d.pop("members")
        for members_item_data in _members:
            members_item = CycleMember.from_dict(members_item_data)

            members.append(members_item)

        partial = d.pop("partial")

        cycle = cls(
            members=members,
            partial=partial,
        )

        cycle.additional_properties = d
        return cycle

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
