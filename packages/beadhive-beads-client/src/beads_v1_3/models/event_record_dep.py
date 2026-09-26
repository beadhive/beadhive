from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="EventRecordDep")


@_attrs_define
class EventRecordDep:
    """On `dep_add` and `dep_remove` only: `{"kind","target","metadata"}` for the edge. Absent on every other op.

    `metadata` differs in PROVENANCE between the two: on `dep_add` it is the value being written as the caller supplied
    it, on `dep_remove` it is the stored column read back just before the delete. The two can differ byte for byte while
    meaning the same thing, so compare parsed values rather than strings.

    """

    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        event_record_dep = cls()

        event_record_dep.additional_properties = d
        return event_record_dep

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
