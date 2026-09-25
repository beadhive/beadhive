from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="RemoveDependencyRequest")


@_attrs_define
class RemoveDependencyRequest:
    """
    Attributes:
        actor (str): Who is removing the edge, under `ClaimRequest.actor`'s rules and for the same reasons: the server
            trims it, refuses an empty result, anything longer than 256 BYTES, and any control character including newline.
            It is attributed on the `dependency_removed` event a real removal records, and interpolated into the storage
            commit message.
        issue_id (str): The edge's SOURCE — the issue that depends on the other end. An EXACT canonical id: there is no
            fuzzy, prefix or substring resolution on this surface.
        depends_on_id (str): The edge's TARGET — the issue depended upon. An exact canonical id, under `issue_id`'s
            rule.
    """

    actor: str
    issue_id: str
    depends_on_id: str

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        issue_id = self.issue_id

        depends_on_id = self.depends_on_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
                "issue_id": issue_id,
                "depends_on_id": depends_on_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        issue_id = d.pop("issue_id")

        depends_on_id = d.pop("depends_on_id")

        remove_dependency_request = cls(
            actor=actor,
            issue_id=issue_id,
            depends_on_id=depends_on_id,
        )

        return remove_dependency_request
