from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue import Issue


T = TypeVar("T", bound="CloseIssueResponse")


@_attrs_define
class CloseIssueResponse:
    """
    Attributes:
        issue (Issue): A tracked work item. Property semantics documented here apply to every schema that repeats them
            below.
        already_closed (bool): True when the issue was already closed and this call changed nothing — the idempotent re-
            close, mirroring `ClaimResponse.already_claimed`. The response still carries the row, and `reason`/`session`
            were not rewritten.
        open_children (int): How many open children the close observed. Reported by a FORCED close — including an
            idempotent re-close — because a caller that bypassed the guard is exactly the caller that wants the number. An
            unforced close that got this far had none, so it reports 0.
        revision (str): The row's optimistic-concurrency token AFTER this close, spelled the way
            `UpdateIssueResponse.revision` spells it.

            It is here because `expected_version` is: a guard whose token no response carries is a guard a caller cannot
            fill, and a close-then-reopen or close-then-delete chain has to compose its next expectation from the value the
            close ANSWERED with. An idempotent re-close carries one too — the row still has a version, and a caller that
            guarded a replay needs the token whether or not the replay wrote.

            IT IS A STRING, the token's decimal spelling — `"-3819021935081927"`, or `"0"` for a legacy migration-0054 row.
            Send it back verbatim as an `expected_version`; do not parse it into a number. A JSON number would not survive
            the trip: the token spans the FULL 64-bit range, and an IEEE-754-double parser — JavaScript's `JSON.parse`, Go's
            `any`, Python's `float` — rounds anything past 2^53 to a value NEAR the token that is not it, so a guard
            composed from it is refused against a row nothing else touched. A string round-trips exactly in every consumer.
    """

    issue: Issue
    already_closed: bool
    open_children: int
    revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        issue = self.issue.to_dict()

        already_closed = self.already_closed

        open_children = self.open_children

        revision = self.revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "issue": issue,
                "already_closed": already_closed,
                "open_children": open_children,
                "revision": revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue import Issue  # noqa: PLC0415

        d = dict(src_dict)
        issue = Issue.from_dict(d.pop("issue"))

        already_closed = d.pop("already_closed")

        open_children = d.pop("open_children")

        revision = d.pop("revision")

        close_issue_response = cls(
            issue=issue,
            already_closed=already_closed,
            open_children=open_children,
            revision=revision,
        )

        close_issue_response.additional_properties = d
        return close_issue_response

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
