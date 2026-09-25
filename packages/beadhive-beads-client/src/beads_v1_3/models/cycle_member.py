from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.issue import Issue


T = TypeVar("T", bound="CycleMember")


@_attrs_define
class CycleMember:
    """One node on a dependency cycle.

    `id` is always present, and its presence is what proves the node is on the cycle. `issue` is the row behind it, and
    is ABSENT — never null — when this workspace holds no record for that id: a target in another repository's
    namespace, an `external:` reference, or a row whose edges outlived it. That absence means the node cannot be
    DESCRIBED here, never that it is not really on the cycle.

    `issue` is spelled as a bare `$ref` with no sibling keywords, following the codegen note at the top of this
    document.

        Attributes:
            id (str):
            issue (Issue | Unset): A tracked work item. Property semantics documented here apply to every schema that
                repeats them below.
    """

    id: str
    issue: Issue | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        issue: dict[str, Any] | Unset = UNSET
        if not isinstance(self.issue, Unset):
            issue = self.issue.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
            }
        )
        if issue is not UNSET:
            field_dict["issue"] = issue

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue import Issue  # noqa: PLC0415

        d = dict(src_dict)
        id = d.pop("id")

        _issue = d.pop("issue", UNSET)
        issue: Issue | Unset
        if isinstance(_issue, Unset):
            issue = UNSET
        else:
            issue = Issue.from_dict(_issue)

        cycle_member = cls(
            id=id,
            issue=issue,
        )

        cycle_member.additional_properties = d
        return cycle_member

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
