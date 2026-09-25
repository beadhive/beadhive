from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="Dependency")


@_attrs_define
class Dependency:
    """A dependency edge between two issues.

    Attributes:
        issue_id (str):
        depends_on_id (str):
        type_ (str): Edge type. Not a closed vocabulary.
        created_at (datetime.datetime):
        id (str | Unset):
        created_by (str | Unset):
        metadata (str | Unset): Free-form edge annotation. A STRING, unlike `Issue.metadata`.
        thread_id (str | Unset):
    """

    issue_id: str
    depends_on_id: str
    type_: str
    created_at: datetime.datetime
    id: str | Unset = UNSET
    created_by: str | Unset = UNSET
    metadata: str | Unset = UNSET
    thread_id: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        issue_id = self.issue_id

        depends_on_id = self.depends_on_id

        type_ = self.type_

        created_at = self.created_at.isoformat()

        id = self.id

        created_by = self.created_by

        metadata = self.metadata

        thread_id = self.thread_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "issue_id": issue_id,
                "depends_on_id": depends_on_id,
                "type": type_,
                "created_at": created_at,
            }
        )
        if id is not UNSET:
            field_dict["id"] = id
        if created_by is not UNSET:
            field_dict["created_by"] = created_by
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if thread_id is not UNSET:
            field_dict["thread_id"] = thread_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        issue_id = d.pop("issue_id")

        depends_on_id = d.pop("depends_on_id")

        type_ = d.pop("type")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        id = d.pop("id", UNSET)

        created_by = d.pop("created_by", UNSET)

        metadata = d.pop("metadata", UNSET)

        thread_id = d.pop("thread_id", UNSET)

        dependency = cls(
            issue_id=issue_id,
            depends_on_id=depends_on_id,
            type_=type_,
            created_at=created_at,
            id=id,
            created_by=created_by,
            metadata=metadata,
            thread_id=thread_id,
        )

        dependency.additional_properties = d
        return dependency

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
