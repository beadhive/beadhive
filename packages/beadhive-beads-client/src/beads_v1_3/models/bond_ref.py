from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="BondRef")


@_attrs_define
class BondRef:
    """A constituent of a compound molecule.

    Attributes:
        source_id (str):
        bond_type (str):
        bond_point (str | Unset):
    """

    source_id: str
    bond_type: str
    bond_point: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source_id = self.source_id

        bond_type = self.bond_type

        bond_point = self.bond_point

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source_id": source_id,
                "bond_type": bond_type,
            }
        )
        if bond_point is not UNSET:
            field_dict["bond_point"] = bond_point

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source_id = d.pop("source_id")

        bond_type = d.pop("bond_type")

        bond_point = d.pop("bond_point", UNSET)

        bond_ref = cls(
            source_id=source_id,
            bond_type=bond_type,
            bond_point=bond_point,
        )

        bond_ref.additional_properties = d
        return bond_ref

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
