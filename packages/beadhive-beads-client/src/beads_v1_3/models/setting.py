from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="Setting")


@_attrs_define
class Setting:
    """One entry of the workspace's stored settings plane.

    THIS SCHEMA IS DELIBERATELY NOT `x-go-type`-PINNED, and it is one of the two ROW schemas on this surface that is not
    — `Memory` is the other. (Envelopes and page wrappers are unpinned as a class, for a different reason: they are new
    wire surface with no prior implementation. `TestWireTagBijection`'s `pinnedSchemas` is the authority on what is
    pinned.) The thirteen pinned schemas above are pinned because a canonical Go struct already IS the contract —
    `types.Issue`'s JSON encoding is what `bd show --json` emits. A setting has no such struct: the CLI marshals an ad-
    hoc `map[string]string` per verb, so there is nothing to pin TO, and minting a type to pin to would mean changing
    what `bd config get --json` prints in order to satisfy a rule about not changing it.

    The two surfaces are still one shape where they overlap — `key` and `value` are spelled as the CLI spells them — and
    they diverge in exactly one deliberate place, `redacted`, which exists because a bearer on this surface is optional,
    shared and surface-wide — it cannot decide that one caller may read a credential and another may not — while the CLI
    requires access to the database anyway.

        Attributes:
            key (str): The setting's key, echoed verbatim.
            redacted (bool): True when `value` is withheld because the KEY marks the setting as credential-bearing — the
                name contains `token`, `secret`, `password`, or an API-key spelling. It is a decision about the key alone: no
                value is inspected, so a credential stored under an innocuous name is NOT protected by this and must not be
                stored in this plane at all.

                Always present, including when false, so a client never has to infer redaction from an absent member.
            value (str | Unset): The stored value, verbatim.

                ABSENT MEANS ONE OF TWO THINGS, and `redacted` says which. With `redacted: false` the workspace stores nothing
                for this key OR stores the empty string; those are indistinguishable through this surface and through the CLI.
                With `redacted: true` a value may well be stored and is withheld.

                It is never emitted as an empty string and never transformed: a value that is not the stored value is omitted
                rather than masked, so a client can never mistake a placeholder for configuration.
    """

    key: str
    redacted: bool
    value: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        key = self.key

        redacted = self.redacted

        value = self.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "key": key,
                "redacted": redacted,
            }
        )
        if value is not UNSET:
            field_dict["value"] = value

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        key = d.pop("key")

        redacted = d.pop("redacted")

        value = d.pop("value", UNSET)

        setting = cls(
            key=key,
            redacted=redacted,
            value=value,
        )

        setting.additional_properties = d
        return setting

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
