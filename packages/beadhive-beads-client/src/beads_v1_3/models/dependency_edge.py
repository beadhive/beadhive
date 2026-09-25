from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="DependencyEdge")


@_attrs_define
class DependencyEdge:
    """One directed edge, as a REQUEST names it. It is not `Dependency`, which is the stored row `GET
    /v0/beads/dependencies` returns and carries the columns storage assigned; this is the three members a caller
    supplies.

        Attributes:
            issue_id (str): The edge's SOURCE — the issue that depends on the other end. An EXACT canonical id, and one this
                database holds: an edge follows its source, so a source that names nothing is a `400`.
            depends_on_id (str): The edge's TARGET — the issue depended upon. An exact canonical id, an `external:`
                reference, or an id belonging to another repository. Only an absence this database can SEE is refused. It must
                differ from `issue_id`.
            type_ (str): The edge type, from the same OPEN vocabulary `Dependency.type` carries: checked for being a
                storable value, never for membership of a known-types list, so a workspace's own type passes.
    """

    issue_id: str
    depends_on_id: str
    type_: str

    def to_dict(self) -> dict[str, Any]:
        issue_id = self.issue_id

        depends_on_id = self.depends_on_id

        type_ = self.type_

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "issue_id": issue_id,
                "depends_on_id": depends_on_id,
                "type": type_,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        issue_id = d.pop("issue_id")

        depends_on_id = d.pop("depends_on_id")

        type_ = d.pop("type")

        dependency_edge = cls(
            issue_id=issue_id,
            depends_on_id=depends_on_id,
            type_=type_,
        )

        return dependency_edge
