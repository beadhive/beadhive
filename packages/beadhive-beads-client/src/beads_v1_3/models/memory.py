from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="Memory")


@_attrs_define
class Memory:
    """One entry of the workspace's persistent memory plane.

    NOT `x-go-type`-PINNED, for the reason `Setting` is not: the CLI marshals an ad-hoc map per verb, so there is no
    canonical Go struct whose JSON encoding is this contract, and minting one to pin to would mean changing what `bd
    recall --json` prints in order to satisfy a rule about not changing it.

    IT HAS NO `redacted` MEMBER, and that is the deliberate difference from `Setting`. Redaction there is a decision
    about the KEY NAME, which works because settings keys are configured names; memory keys are derived from the
    content, so the same rule would withhold a memory about credentials and serve one containing a credential under an
    innocuous slug. A configured bearer would not close that either — it admits a client to the whole surface rather
    than to particular keys — so this schema states the exposure rather than implying a protection it does not have.

        Attributes:
            key (str): The memory's key, echoed verbatim.
            value (str): The stored content, verbatim: newlines, surrounding space and unicode as stored, never truncated
                and never withheld.

                Always present. It is the empty string only where a row was written out of band with an empty value, which `GET
                /v0/beads/memories/{key}` answers as a `404` and `GET /v0/beads/memories` enumerates.
    """

    key: str
    value: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        key = self.key

        value = self.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "key": key,
                "value": value,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        key = d.pop("key")

        value = d.pop("value")

        memory = cls(
            key=key,
            value=value,
        )

        memory.additional_properties = d
        return memory

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
