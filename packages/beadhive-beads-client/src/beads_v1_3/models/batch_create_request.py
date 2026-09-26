from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

if TYPE_CHECKING:
    from ..models.batch_create_item import BatchCreateItem


T = TypeVar("T", bound="BatchCreateRequest")


@_attrs_define
class BatchCreateRequest:
    """
    Attributes:
        actor (str): Who is creating the issues, under `ClaimRequest.actor`'s rules and for the same reasons: the server
            trims it, refuses an empty result, anything longer than 256 BYTES, and any control character including newline.
            It is attributed to every item and interpolated into the storage commit message.
        items (list[BatchCreateItem]): The issues to create, in order. An empty array is a `400` rather than a
            successful no-op: a write request that writes nothing is a client bug, and answering it with a cheerful empty
            success is how a client whose own list filtered to nothing silently stops creating anything.

            The 100-item cap is a bound on how long one request may hold a write transaction, not a statement about batch
            semantics. Split a larger plan; each request is atomic on its own.
    """

    actor: str
    items: list[BatchCreateItem]

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
                "items": items,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.batch_create_item import BatchCreateItem  # noqa: PLC0415

        d = dict(src_dict)
        actor = d.pop("actor")

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = BatchCreateItem.from_dict(items_item_data)

            items.append(items_item)

        batch_create_request = cls(
            actor=actor,
            items=items,
        )

        return batch_create_request
