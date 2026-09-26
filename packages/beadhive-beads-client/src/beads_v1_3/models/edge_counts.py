from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.anchor_edge_count import AnchorEdgeCount


T = TypeVar("T", bound="EdgeCounts")


@_attrs_define
class EdgeCounts:
    """Each anchor's edge cardinality — the body of `GET /v0/beads/dependencies:count`. It is NOT a page and carries no
    total: the answer is per anchor, and a sum across anchors would double-count every edge whose two ends were both
    named.

    It is NOT `x-go-type`-pinned, for `IssueCount`'s reason: there is no canonical Go struct whose JSON encoding is this
    contract. The role answers with `issueops.EdgeCountResult`, whose members carry no JSON tags at all because nothing
    marshals it.

        Attributes:
            anchors (list[AnchorEdgeCount]): One entry per DISTINCT requested `issue_id`, in the order the request first
                named it. Empty array (never null) when the request named no anchors this server accepted — which it cannot,
                since `issue_id` is required and bounded below at one.

                A repeated id appears ONCE. The collapse happens before anything is counted, so a caller that summed the entries
                would not count the same edges twice.
    """

    anchors: list[AnchorEdgeCount]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        anchors = []
        for anchors_item_data in self.anchors:
            anchors_item = anchors_item_data.to_dict()
            anchors.append(anchors_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "anchors": anchors,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.anchor_edge_count import AnchorEdgeCount  # noqa: PLC0415

        d = dict(src_dict)
        anchors = []
        _anchors = d.pop("anchors")
        for anchors_item_data in _anchors:
            anchors_item = AnchorEdgeCount.from_dict(anchors_item_data)

            anchors.append(anchors_item)

        edge_counts = cls(
            anchors=anchors,
        )

        edge_counts.additional_properties = d
        return edge_counts

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
