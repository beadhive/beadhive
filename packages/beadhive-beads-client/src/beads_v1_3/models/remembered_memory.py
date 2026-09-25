from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="RememberedMemory")


@_attrs_define
class RememberedMemory:
    """One stored memory, plus whether storing it overwrote a previous value.

    It is `Memory`'s shape with `replaced` added rather than a composition of it, because this document repeats property
    lists instead of using `allOf` (see the note at the top of the file).

        Attributes:
            key (str): The key the memory now lives under: the one the request supplied, or the one derived from `content`.
                Recall it under exactly these bytes.
            value (str): The stored content, echoed verbatim. Always present, and never withheld — this plane has no
                redaction; see the operation description.
            replaced (bool): True when a previous value existed under `key` and this request overwrote it; false when the
                key was new. It is observed in the same transaction as the write, so it describes the row this request wrote.

                A previous value that was the EMPTY STRING reports true: the ROW existed, even though `GET
                /v0/beads/memories/{key}` would have answered `404` for it. That divergence is the storage seam's conflation
                showing through, and it is stated rather than smoothed over, because smoothing it would mean this member
                reporting "nothing was there" about a write that overwrote something.
    """

    key: str
    value: str
    replaced: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        key = self.key

        value = self.value

        replaced = self.replaced

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "key": key,
                "value": value,
                "replaced": replaced,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        key = d.pop("key")

        value = d.pop("value")

        replaced = d.pop("replaced")

        remembered_memory = cls(
            key=key,
            value=value,
            replaced=replaced,
        )

        remembered_memory.additional_properties = d
        return remembered_memory

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
