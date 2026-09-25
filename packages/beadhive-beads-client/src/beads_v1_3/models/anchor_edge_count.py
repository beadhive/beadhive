from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AnchorEdgeCount")


@_attrs_define
class AnchorEdgeCount:
    """One anchor's edge cardinality, or the report that it is not there.

    Attributes:
        id (str): The anchor, spelled exactly as the request spelled it. It is not re-canonicalized: there is no fuzzy,
            prefix or substring resolution on this surface, so what comes back is what went out.
        count (int): How many stored edges match, in the requested direction, after the type and status filters. Never
            negative.

            It SPANS BOTH DEPENDENCY PLANES and is a SUM over them rather than a distinct count of edge rows — a durable
            issue's dependent count includes the wisps that depend on it, and a wisp's dependency count includes the durable
            issues it depends on. `issueops.AnchorEdgeCount.Count` states the rule, the one state that can make the sum
            differ from a distinct count, and why this role answers with the sum. Nothing is restated here.

            ALWAYS PRESENT, including as 0, and 0 is the COMMON answer: most issues have no edges in at least one direction.
            It is 0 for a missing anchor too, which is exactly why `missing` is beside it.

            DECODE IT AS A 64-BIT INTEGER. The member is `format: int64` and a workspace's graph is not bounded by 2^53; a
            lossy parser would answer a number NEAR the count, which on a cardinality is worse than an error because nothing
            downstream can tell.
        missing (bool): True when no issue and no wisp carries this id.

            ALWAYS PRESENT, including as `false`. It is the member that keeps this from being a question a caller cannot
            tell it got wrong: a count of 0 is otherwise indistinguishable from a typo, and an absent boolean would be
            ambiguous between "present" and "this producer does not report misses".

            A missing anchor counts 0 whatever rows are still keyed to it — a dependency row whose source has been deleted
            is orphaned data, and counting it would contradict this flag.

            DANGLING EDGES ARE NOT MISSING ANCHORS. This is about the ANCHOR. An edge whose OTHER end names nothing is
            counted like any other edge, and nothing here reports on it.
    """

    id: str
    count: int
    missing: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        count = self.count

        missing = self.missing

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "count": count,
                "missing": missing,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        count = d.pop("count")

        missing = d.pop("missing")

        anchor_edge_count = cls(
            id=id,
            count=count,
            missing=missing,
        )

        anchor_edge_count.additional_properties = d
        return anchor_edge_count

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
