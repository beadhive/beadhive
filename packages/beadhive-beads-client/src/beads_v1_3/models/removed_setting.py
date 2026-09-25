from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="RemovedSetting")


@_attrs_define
class RemovedSetting:
    """The outcome of removing one setting.

    IT CARRIES THE KEY AND NOTHING ELSE, and the absence is the contract rather than an unfinished shape. There is no
    `removed` flag because the storage seam discards the affected-row count on every implementation, so the member would
    be a value one of them had to invent — and no `value`, because reporting what was there would publish, on the one
    operation that withholds nothing, exactly the credential `GET /v0/beads/config/{key}` redacts.

        Attributes:
            key (str): The key that now holds nothing, echoed verbatim.
    """

    key: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        key = self.key

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "key": key,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        key = d.pop("key")

        removed_setting = cls(
            key=key,
        )

        removed_setting.additional_properties = d
        return removed_setting

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
