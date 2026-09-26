from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CompareAndSetMetadataResponse")


@_attrs_define
class CompareAndSetMetadataResponse:
    """
    Attributes:
        swapped (bool): Whether the precondition held and the transition applied. THIS IS THE VERDICT and the only
            member to dispatch on. False is a lost race — an answer, not a failure — and the response is still a 200.
        current (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
            because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
            string, and a client must not decode it as one.

            Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
            and holds null. Those are different states and this surface reports both.
    """

    swapped: bool
    current: Any | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        swapped = self.swapped

        current = self.current

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "swapped": swapped,
            }
        )
        if current is not UNSET:
            field_dict["current"] = current

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        swapped = d.pop("swapped")

        current = d.pop("current", UNSET)

        compare_and_set_metadata_response = cls(
            swapped=swapped,
            current=current,
        )

        compare_and_set_metadata_response.additional_properties = d
        return compare_and_set_metadata_response

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
