from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="IssueMetadata")


@_attrs_define
class IssueMetadata:
    """Arbitrary caller-supplied JSON object. VALUES MAY BE OF ANY JSON TYPE — string, number, boolean, array or nested
    object — because typed values enter through the explicit JSON metadata path and persist in older rows. Clients MUST
    NOT decode this into a string-to-string map; a strict decode fails on the first typed value and takes the whole
    response with it. That prohibition is on assuming a narrower type when reading THIS document: a profile MAY declare
    a narrower value schema under the rules in **Profiles**, and a client of that profile may rely on the profile's
    declaration.

    The object-at-top-level shape is the contract every producer and every metadata filter (`metadata_field`,
    `has_metadata_key`) assumes, but be aware the store validates only that the value is WELL-FORMED JSON: a row created
    with a non-object (e.g. `bd create --metadata '[1,2]'`) can carry an array or a scalar here. That is a storage-side
    gap, not licence for a producer to emit one — but a tolerant client should skip such a row rather than fail the
    whole response.

    """

    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        issue_metadata = cls()

        issue_metadata.additional_properties = d
        return issue_metadata

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
