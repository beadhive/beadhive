from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue import Issue


T = TypeVar("T", bound="ReleaseIssueResponse")


@_attrs_define
class ReleaseIssueResponse:
    """
    Attributes:
        issue (Issue): A tracked work item. Property semantics documented here apply to every schema that repeats them
            below.
        changed (bool): Whether the release WROTE the row. It is TRUE on every 200 this operation returns, because every
            shape that would not write is refused above it — an unheld row is a 409, not an idempotent no-op. DO NOT WRITE A
            `changed: false` BRANCH: no request reaches one.

            It is published rather than omitted because `claimIssue` and `updateIssue` publish the same fact, and a caller
            holding all three should not have to read them two ways. It is also the negative space that answers "where is
            the `already_released` member" — there is none, and the operation description says why.
        revision (str): The row's optimistic-concurrency token AFTER the release, spelled the way
            `UpdateIssueResponse.revision` spells it and carrying the same promise: a read-modify-write loop composes its
            next `expected_version` from THIS value, never from one it composed itself.

            A release REMINTS the token by design, so a caller that guarded a following write on a version it read BEFORE
            the release will miss. That is the point — a concurrent reclaim or close conflicts rather than silently merging
            — and this member is how the caller stays in step.

            IT IS A STRING, the token's decimal spelling — `"-3819021935081927"`, or `"0"` for a legacy migration-0054 row.
            Send it back verbatim as an `expected_version`; do not parse it into a number. A JSON number would not survive
            the trip: the token spans the FULL 64-bit range, and an IEEE-754-double parser — JavaScript's `JSON.parse`, Go's
            `any`, Python's `float` — rounds anything past 2^53 to a value NEAR the token that is not it, so a guard
            composed from it is refused against a row nothing else touched. A string round-trips exactly in every consumer.
    """

    issue: Issue
    changed: bool
    revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        issue = self.issue.to_dict()

        changed = self.changed

        revision = self.revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "issue": issue,
                "changed": changed,
                "revision": revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue import Issue  # noqa: PLC0415

        d = dict(src_dict)
        issue = Issue.from_dict(d.pop("issue"))

        changed = d.pop("changed")

        revision = d.pop("revision")

        release_issue_response = cls(
            issue=issue,
            changed=changed,
            revision=revision,
        )

        release_issue_response.additional_properties = d
        return release_issue_response

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
