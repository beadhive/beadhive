from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.issue import Issue


T = TypeVar("T", bound="UpdateIssueResponse")


@_attrs_define
class UpdateIssueResponse:
    """
    Attributes:
        issue (Issue): A tracked work item. Property semantics documented here apply to every schema that repeats them
            below.
        changed (bool): Whether the request persisted a semantic mutation. A same-value patch is a 200 with `changed:
            false` rather than an error — idempotent, like every replay answer on this surface.
        revision (str): The row's optimistic-concurrency token AFTER this write, spelled the way
            `ApplyItemResult.revision` spells it.

            It is here because `expected_version` is: a guard whose token no response carries is a guard a caller cannot
            fill. A read-modify-write loop composes its next expectation from THIS value and never from a number it
            incremented itself, for the reason `compareAndSetMetadata` gives about a value the store renormalizes. `GET
            /v0/beads/issues/{id}`'s `revision` is the read that publishes the same token, and this member agrees with it.

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

        update_issue_response = cls(
            issue=issue,
            changed=changed,
            revision=revision,
        )

        update_issue_response.additional_properties = d
        return update_issue_response

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
