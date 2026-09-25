from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="IssueBlocking")


@_attrs_define
class IssueBlocking:
    """One issue's derived blocking decoration.

    `blocked_by` and `blocks` are ASCENDING BY ID with repeats collapsed, and both are always present — an empty array,
    never null and never absent, so a client reads "nothing blocks this" from the answer rather than from a missing key.
    `parent` is absent when the issue has none and when the parent it has is closed.

        Attributes:
            id (str): The annotated id, spelled exactly as the request spelled it.
            blocked_by (list[str]): The OPEN issues this one is blocked by: the targets of its `blocks` edges whose own
                status is not closed.
            blocks (list[str]): The issues this one blocks. Empty when this issue is itself closed, which is the same rule
                `blocked_by` applies from the other end.
            parent (str | Unset): This issue's parent id. AT MOST ONE is reported; where an issue carries several `parent-
                child` edges, which one appears here is not specified. A client that needs every structural edge reads them from
                `GET /v0/beads/dependencies`.
    """

    id: str
    blocked_by: list[str]
    blocks: list[str]
    parent: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        blocked_by = self.blocked_by

        blocks = self.blocks

        parent = self.parent

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "blocked_by": blocked_by,
                "blocks": blocks,
            }
        )
        if parent is not UNSET:
            field_dict["parent"] = parent

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        blocked_by = cast(list[str], d.pop("blocked_by"))

        blocks = cast(list[str], d.pop("blocks"))

        parent = d.pop("parent", UNSET)

        issue_blocking = cls(
            id=id,
            blocked_by=blocked_by,
            blocks=blocks,
            parent=parent,
        )

        issue_blocking.additional_properties = d
        return issue_blocking

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
