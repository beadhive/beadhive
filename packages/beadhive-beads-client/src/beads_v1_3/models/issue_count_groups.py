from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="IssueCountGroups")


@_attrs_define
class IssueCountGroups:
    """Bucket key to cardinality, PRESENT exactly when the request carried `group_by` and ABSENT otherwise. That absence is
    the answer to "you did not ask for buckets"; an empty OBJECT is the answer to "nothing matched", and the two are
    deliberately different — a client must be able to tell a scalar count from a grouped count of an empty set without
    re-reading its own request.

    Buckets with no rows are absent rather than present at zero. The dimensions are open-ended — any assignee, any
    label, any custom status — so there is no closed set of keys to enumerate and a client reads an absent key as zero.
    The KEY normalization is part of the contract and is documented on `group_by`.

    """

    additional_properties: dict[str, int] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        issue_count_groups = cls()

        issue_count_groups.additional_properties = d
        return issue_count_groups

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> int:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: int) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
