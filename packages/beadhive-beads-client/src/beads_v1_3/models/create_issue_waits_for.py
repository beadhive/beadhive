from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateIssueWaitsFor")


@_attrs_define
class CreateIssueWaitsFor:
    """A typed `waits-for` edge from the new issue to a spawner whose children gate it. It records a readiness primitive;
    it does not define scheduling or execution policy.

    IT IS A TYPED MEMBER HERE AND A METADATA BLOB ON `POST /v0/beads/issues:batchApply`, and the difference follows the
    ROLE rather than taste: `CreateRequest.WaitsFor` is a typed field that gets the gate defaulted and the "must not
    duplicate an explicit edge" check, while that operation's `dep_add` item is one generic edge with no typed field to
    reach. One spelling per operation, and each is its role's.

        Attributes:
            spawner_id (str): The dependency target whose children are observed. It must not duplicate an edge
                `dependencies` or `parent_id` already spells.
            gate (str | Unset): The readiness condition: `all-children` or `any-children`. Absent or empty defaults to `all-
                children`. A value that is neither is refused by the ROLE and reaches the client as a `400`.
    """

    spawner_id: str
    gate: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        spawner_id = self.spawner_id

        gate = self.gate

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "spawner_id": spawner_id,
            }
        )
        if gate is not UNSET:
            field_dict["gate"] = gate

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        spawner_id = d.pop("spawner_id")

        gate = d.pop("gate", UNSET)

        create_issue_waits_for = cls(
            spawner_id=spawner_id,
            gate=gate,
        )

        return create_issue_waits_for
