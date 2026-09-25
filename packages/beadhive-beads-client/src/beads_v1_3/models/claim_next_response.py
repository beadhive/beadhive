from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.issue_with_counts import IssueWithCounts


T = TypeVar("T", bound="ClaimNextResponse")


@_attrs_define
class ClaimNextResponse:
    """The outcome of one atomic take of ready work.

    `claimed` IS ABSENT WHEN NOTHING WAS ELIGIBLE, and its absence is the whole signal — there is no boolean beside it,
    because a second member carrying the same fact is a second member that can disagree with the first. A polling client
    branches on presence.

    IT IS ALSO THE ONLY MEMBER, deliberately. A count of what was scanned, or how many rows a racing agent had already
    taken, would describe a moment inside a transaction that has committed and is not a fact about the claim.

    The row is `IssueWithCounts` — the element type `GET /v0/beads/ready` returns and `bd ready --json` emits — hydrated
    INSIDE the transaction that committed the claim, so its counts describe the state the claim produced rather than a
    later one. It is not an `Issue` because the listing this replaces answers with counts, and a client swapping the
    composed pair for this operation should not lose a field doing it.

        Attributes:
            claimed (IssueWithCounts | Unset): An `Issue` plus relationship cardinalities. This is the element type of both
                `/v0/beads/ready` and `/v0/beads/issues`, matching what `bd ready --json` and `bd list --json` emit. Property
                semantics are documented on `Issue`.
    """

    claimed: IssueWithCounts | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        claimed: dict[str, Any] | Unset = UNSET
        if not isinstance(self.claimed, Unset):
            claimed = self.claimed.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if claimed is not UNSET:
            field_dict["claimed"] = claimed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue_with_counts import IssueWithCounts  # noqa: PLC0415

        d = dict(src_dict)
        _claimed = d.pop("claimed", UNSET)
        claimed: IssueWithCounts | Unset
        if isinstance(_claimed, Unset):
            claimed = UNSET
        else:
            claimed = IssueWithCounts.from_dict(_claimed)

        claim_next_response = cls(
            claimed=claimed,
        )

        claim_next_response.additional_properties = d
        return claim_next_response

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
