from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue import Issue


T = TypeVar("T", bound="ReopenIssueResponse")


@_attrs_define
class ReopenIssueResponse:
    """
    Attributes:
        issue (Issue): A tracked work item. Property semantics documented here apply to every schema that repeats them
            below.
        already_open (bool): True when the issue was not in a done status and this call changed nothing — idempotent,
            mirroring `CloseIssueResponse.already_closed` and `ClaimResponse.already_claimed`. The response still carries
            the row.
        revision (str): The row's optimistic-concurrency token AFTER this reopen, spelled the way
            `CloseIssueResponse.revision` spells it and here for the same reason: a recovery flow that reopens and then re-
            closes composes its next `expected_version` from this value. DECODE IT AS A 64-BIT INTEGER.

            IT IS A STRING, the token's decimal spelling — `"-3819021935081927"`, or `"0"` for a legacy migration-0054 row.
            Send it back verbatim as an `expected_version`; do not parse it into a number. A JSON number would not survive
            the trip: the token spans the FULL 64-bit range, and an IEEE-754-double parser — JavaScript's `JSON.parse`, Go's
            `any`, Python's `float` — rounds anything past 2^53 to a value NEAR the token that is not it, so a guard
            composed from it is refused against a row nothing else touched. A string round-trips exactly in every consumer.
    """

    issue: Issue
    already_open: bool
    revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        issue = self.issue.to_dict()

        already_open = self.already_open

        revision = self.revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "issue": issue,
                "already_open": already_open,
                "revision": revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue import Issue  # noqa: PLC0415

        d = dict(src_dict)
        issue = Issue.from_dict(d.pop("issue"))

        already_open = d.pop("already_open")

        revision = d.pop("revision")

        reopen_issue_response = cls(
            issue=issue,
            already_open=already_open,
            revision=revision,
        )

        reopen_issue_response.additional_properties = d
        return reopen_issue_response

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
