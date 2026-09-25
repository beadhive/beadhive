from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="RememberRequest")


@_attrs_define
class RememberRequest:
    """What to remember, and optionally under what key.

    Attributes:
        content (str): The memory itself, stored VERBATIM: newlines, surrounding space and unicode all survive.
            Flattening it to one line is what a front door does when it prints, not what this plane does when it stores.

            Empty after trimming is a `400`. So is content from which no key can be derived when `key` is omitted — `"!!!"`
            derives to nothing — and the recovery for that one is to send a `key`.
        key (str | Unset): The key to store under. OMIT IT to have the server derive one from `content`; the response's
            `key` is then how the caller learns where the memory landed.

            Supplied, it is used verbatim — no trimming, no slugging, no charset restriction. A key carrying a control
            character is storable this way and by `bd remember --key`, and is then unreachable through `GET`/`DELETE
            /v0/beads/memories/{key}`, which refuse one: see those operations.
    """

    content: str
    key: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        content = self.content

        key = self.key

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "content": content,
            }
        )
        if key is not UNSET:
            field_dict["key"] = key

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content = d.pop("content")

        key = d.pop("key", UNSET)

        remember_request = cls(
            content=content,
            key=key,
        )

        return remember_request
