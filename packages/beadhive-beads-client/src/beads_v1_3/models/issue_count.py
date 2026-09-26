from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.issue_count_groups import IssueCountGroups


T = TypeVar("T", bound="IssueCount")


@_attrs_define
class IssueCount:
    """The size of a matching set, and its buckets when `group_by` asked for them. It carries no items and no cursor: this
    is a number about a set, and the operations that return rows are `GET /v0/beads/issues` and `GET
    /v0/beads/issues:query`.

    ONE SCHEMA FOR BOTH SHAPES, because the grouped answer is the scalar answer plus one member rather than a different
    answer. See the operation's own description for why that is one operation and not two.

    It is NOT `x-go-type`-pinned, for `ReadyCount`'s reason: there is no canonical Go struct whose JSON encoding is this
    contract.

        Attributes:
            total (int): How many issues match. Never negative; `0` when nothing matches, which is a 200 rather than a 404 —
                a question about a set has an answer even when the set is empty, and a client polling for work would otherwise
                have to classify an error to read a zero.

                Under `group_by` this is still the cardinality of the WHOLE matching set and NOT the sum of `groups`. The two
                differ for `label`, whose buckets overlap; see the operation description.
            groups (IssueCountGroups | Unset): Bucket key to cardinality, PRESENT exactly when the request carried
                `group_by` and ABSENT otherwise. That absence is the answer to "you did not ask for buckets"; an empty OBJECT is
                the answer to "nothing matched", and the two are deliberately different — a client must be able to tell a scalar
                count from a grouped count of an empty set without re-reading its own request.

                Buckets with no rows are absent rather than present at zero. The dimensions are open-ended — any assignee, any
                label, any custom status — so there is no closed set of keys to enumerate and a client reads an absent key as
                zero. The KEY normalization is part of the contract and is documented on `group_by`.
    """

    total: int
    groups: IssueCountGroups | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        groups: dict[str, Any] | Unset = UNSET
        if not isinstance(self.groups, Unset):
            groups = self.groups.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "total": total,
            }
        )
        if groups is not UNSET:
            field_dict["groups"] = groups

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue_count_groups import IssueCountGroups  # noqa: PLC0415

        d = dict(src_dict)
        total = d.pop("total")

        _groups = d.pop("groups", UNSET)
        groups: IssueCountGroups | Unset
        if isinstance(_groups, Unset):
            groups = UNSET
        else:
            groups = IssueCountGroups.from_dict(_groups)

        issue_count = cls(
            total=total,
            groups=groups,
        )

        issue_count.additional_properties = d
        return issue_count

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
