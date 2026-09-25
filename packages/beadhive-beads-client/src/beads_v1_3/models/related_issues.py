from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue_with_dependency_metadata import IssueWithDependencyMetadata


T = TypeVar("T", bound="RelatedIssues")


@_attrs_define
class RelatedIssues:
    """One issue's neighbors — the body of `GET /v0/beads/issues/{id}/related`. It is NOT a page: this operation has no
    limit and no cursor, and it names ONE anchor, so there is nothing for a `has_more` to be about.

    There is no `missing` beside `items`, unlike `DependencyEdges`. That member exists because a batch cannot report an
    absent anchor any other way without discarding the anchors it did find; here the single absent anchor is a 404.

    It is NOT `x-go-type`-pinned, for `EdgeCounts`' reason: there is no canonical Go struct whose JSON encoding is this
    envelope. The role answers with a bare slice of `issueops.RelatedIssue`, and THAT element is pinned — see
    `IssueWithDependencyMetadata`.

        Attributes:
            items (list[IssueWithDependencyMetadata]): The neighbors, ascending by id with the edge type breaking a tie.
                Empty array (never null) when this issue has none in the requested direction, or when the `type` filter rejected
                every edge.

                Each element is a full issue plus `dependency_type`, the type of the edge that led to it — the same element `GET
                /v0/beads/issues/{id}` carries under `dependencies` and `dependents`, so the two surfaces are one compatibility
                domain.

                AN EDGE WITH NO FAR END IN THIS DATABASE CONTRIBUTES NOTHING here: an `external:` reference, an id in another
                repository's namespace and an id whose issue was deleted out from under its edges are all silently absent, with
                no placeholder row and no error. The length of this array is a NEIGHBOR count, never an edge count.
    """

    items: list[IssueWithDependencyMetadata]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue_with_dependency_metadata import (
            IssueWithDependencyMetadata,  # noqa: PLC0415
        )

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = IssueWithDependencyMetadata.from_dict(items_item_data)

            items.append(items_item)

        related_issues = cls(
            items=items,
        )

        related_issues.additional_properties = d
        return related_issues

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
