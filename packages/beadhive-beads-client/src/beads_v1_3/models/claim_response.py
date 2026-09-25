from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue import Issue


T = TypeVar("T", bound="ClaimResponse")


@_attrs_define
class ClaimResponse:
    """
    Attributes:
        issue (Issue): A tracked work item. Property semantics documented here apply to every schema that repeats them
            below.
        already_claimed (bool): True when the caller already held the issue and this call changed nothing — the
            idempotent re-claim. A claim held by a DIFFERENT actor is a 409, not a 200 with this flag.
    """

    issue: Issue
    already_claimed: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        issue = self.issue.to_dict()

        already_claimed = self.already_claimed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "issue": issue,
                "already_claimed": already_claimed,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue import Issue  # noqa: PLC0415

        d = dict(src_dict)
        issue = Issue.from_dict(d.pop("issue"))

        already_claimed = d.pop("already_claimed")

        claim_response = cls(
            issue=issue,
            already_claimed=already_claimed,
        )

        claim_response.additional_properties = d
        return claim_response

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
