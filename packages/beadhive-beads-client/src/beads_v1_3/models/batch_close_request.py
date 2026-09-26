from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.batch_close_item import BatchCloseItem


T = TypeVar("T", bound="BatchCloseRequest")


@_attrs_define
class BatchCloseRequest:
    """
    Attributes:
        actor (str): Who is closing. `ClaimRequest.actor`'s rules exactly, and the value is recorded against every item.
        items (list[BatchCloseItem]): The issues to close, in the order the caller asked for them. Every item appears in
            `outcomes` at the same index.

            An EMPTY array is a `400` rather than an empty answer, and the cap is `batchCreateIssues`' cap for its reason:
            it bounds how long one request may hold a write transaction.
        session (str | Unset): The working session, recorded against every item that closes, under
            `CloseIssueRequest.session`'s first-close-wins rule and bounds.
        force (bool | Unset): Bypass close policy — the open-children refusal and the live-blocker refusal — for EVERY
            item, and nothing else. It never bypasses validation and it never bypasses existence: an id that names nothing
            refuses whether or not this is set. It is request-wide because the flag that spells it is. Default: False.
    """

    actor: str
    items: list[BatchCloseItem]
    session: str | Unset = UNSET
    force: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        session = self.session

        force = self.force

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
                "items": items,
            }
        )
        if session is not UNSET:
            field_dict["session"] = session
        if force is not UNSET:
            field_dict["force"] = force

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.batch_close_item import BatchCloseItem  # noqa: PLC0415

        d = dict(src_dict)
        actor = d.pop("actor")

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = BatchCloseItem.from_dict(items_item_data)

            items.append(items_item)

        session = d.pop("session", UNSET)

        force = d.pop("force", UNSET)

        batch_close_request = cls(
            actor=actor,
            items=items,
            session=session,
            force=force,
        )

        return batch_close_request
