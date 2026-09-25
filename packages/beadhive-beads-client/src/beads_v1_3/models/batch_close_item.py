from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="BatchCloseItem")


@_attrs_define
class BatchCloseItem:
    """
    Attributes:
        id (str): Exact canonical issue id, resolved across BOTH planes. No fuzzy, prefix or substring resolution —
            `IssueID`'s rule.

            A DUPLICATE is admissible; see the operation description.
        reason (str | Unset): Why THIS issue is closed. It is per item rather than per request because `bd close a b c
            --reason x --reason y --reason z` has always mapped them positionally, and one request-wide reason could not
            express it. `CloseIssueRequest.reason`'s rules and first-close-wins.
    """

    id: str
    reason: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        reason = self.reason

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        reason = d.pop("reason", UNSET)

        batch_close_item = cls(
            id=id,
            reason=reason,
        )

        return batch_close_item
