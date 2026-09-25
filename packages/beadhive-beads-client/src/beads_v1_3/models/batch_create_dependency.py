from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="BatchCreateDependency")


@_attrs_define
class BatchCreateDependency:
    """
    Attributes:
        target_id (str): The far end of the edge: an issue this workspace holds, an `external:` reference, or an id
            whose prefix belongs to another repository. Anything else is a `400` and nothing is created.

            NOT AN ITEM OF THIS REQUEST. The server assigns every id and an item carries no name, so there is nothing here a
            caller could write to address one; see the operation's description for the operation that can.
        type_ (str): The edge type, from the same OPEN vocabulary `Dependency.type` carries. It is spelled `type`
            because that is the member an edge carries everywhere else on this surface.
    """

    target_id: str
    type_: str

    def to_dict(self) -> dict[str, Any]:
        target_id = self.target_id

        type_ = self.type_

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "target_id": target_id,
                "type": type_,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        target_id = d.pop("target_id")

        type_ = d.pop("type")

        batch_create_dependency = cls(
            target_id=target_id,
            type_=type_,
        )

        return batch_create_dependency
