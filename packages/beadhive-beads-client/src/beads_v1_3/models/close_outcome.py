from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.issue import Issue


T = TypeVar("T", bound="CloseOutcome")


@_attrs_define
class CloseOutcome:
    """What happened to ONE requested item.

    `code` IS THE DISCRIMINATOR. Present means the item REFUSED and nothing was written for it; absent means it
    succeeded, and `issue`, `already_closed` and `open_children` are all present. A client branches on `code` first and
    reads nothing else until it has.

        Attributes:
            issue_id (str): The id the caller asked for, echoed verbatim so an outcome can be read without indexing back
                into the request.
            issue (Issue | Unset): A tracked work item. Property semantics documented here apply to every schema that
                repeats them below.
            already_closed (bool | Unset): True when the issue was already closed and this item changed nothing — the
                idempotent re-close, spelled the way `CloseIssueResponse.already_closed` spells it. Present only on a successful
                item.

                A BATCH WHOSE ITEMS ARE ALL `true` LANDED NOTHING, and records no history entry: a per-item success that changed
                nothing is not work the caller did.
            open_children (int | Unset): How many open children the transaction observed for this item.

                ITS MEANING FOLLOWS `code`, and both readings are the ones the single close already publishes. On a SUCCESSFUL
                item it is always present and is `CloseIssueResponse.open_children` — the number a forced close bypassed,
                reported even for an idempotent re-close, and `0` for an unforced close that got that far. On a REFUSED item its
                PRESENCE is the discriminator between the two `not_closable` refusals, exactly as it is on a problem document:
                present means open children, absent means a live blocker.
            code (str | Unset): This item's refusal, from `Problem.code`'s vocabulary and restricted to `not_found` (the id
                names no row in either plane) and `not_closable` (close policy refused it: open children, or a live blocker —
                see `open_children`). ABSENT means the item succeeded.

                It is the problem vocabulary rather than a second one because an item refusal and a request refusal are the same
                question asked at two scopes, and a client that had to learn two vocabularies to classify one condition would be
                classifying the SCOPE rather than the condition.
            detail (str | Unset): Prose for a refusal, never load-bearing and present only with `code`. It reflects the
                request and this server's own words rather than the role's message, for the reason `Problem.detail` gives.
    """

    issue_id: str
    issue: Issue | Unset = UNSET
    already_closed: bool | Unset = UNSET
    open_children: int | Unset = UNSET
    code: str | Unset = UNSET
    detail: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        issue_id = self.issue_id

        issue: dict[str, Any] | Unset = UNSET
        if not isinstance(self.issue, Unset):
            issue = self.issue.to_dict()

        already_closed = self.already_closed

        open_children = self.open_children

        code = self.code

        detail = self.detail

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "issue_id": issue_id,
            }
        )
        if issue is not UNSET:
            field_dict["issue"] = issue
        if already_closed is not UNSET:
            field_dict["already_closed"] = already_closed
        if open_children is not UNSET:
            field_dict["open_children"] = open_children
        if code is not UNSET:
            field_dict["code"] = code
        if detail is not UNSET:
            field_dict["detail"] = detail

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue import Issue  # noqa: PLC0415

        d = dict(src_dict)
        issue_id = d.pop("issue_id")

        _issue = d.pop("issue", UNSET)
        issue: Issue | Unset
        if isinstance(_issue, Unset):
            issue = UNSET
        else:
            issue = Issue.from_dict(_issue)

        already_closed = d.pop("already_closed", UNSET)

        open_children = d.pop("open_children", UNSET)

        code = d.pop("code", UNSET)

        detail = d.pop("detail", UNSET)

        close_outcome = cls(
            issue_id=issue_id,
            issue=issue,
            already_closed=already_closed,
            open_children=open_children,
            code=code,
            detail=detail,
        )

        close_outcome.additional_properties = d
        return close_outcome

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
