from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="Ref")


@_attrs_define
class Ref:
    """Names ONE issue, either by an id that already exists or by the `key` a create item earlier in the same request gave
    itself.

    EXACTLY ONE OF THE TWO IS SET, and both cases the schema cannot express are a `400`: both members set is a caller
    that cannot say which it meant, and neither set is a reference to nothing. (Spelling that as a schema alternation
    would need `oneOf`, which this document does not use — see `ApplyItem`.)

    A KEY REACHES BACKWARD ONLY where the ref ADDRESSES a row — an `update.target`, a `close.target`, either endpoint of
    a `dep_add`. The one exception is `create.metadata_refs`, whose values may reach forward or name their own item's
    key; the operation's description says why.

        Attributes:
            key (str | Unset): The `key` a create item in THIS REQUEST gave itself. It is not an id, it is not stored
                anywhere, and it is resolved to the id the request minted — which the response's `keys` member reports.
            id (str | Unset): An id that already exists, EXACTLY. There is no fuzzy, prefix or cross-repo resolution on this
                surface.
    """

    key: str | Unset = UNSET
    id: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        key = self.key

        id = self.id

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if key is not UNSET:
            field_dict["key"] = key
        if id is not UNSET:
            field_dict["id"] = id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        key = d.pop("key", UNSET)

        id = d.pop("id", UNSET)

        ref = cls(
            key=key,
            id=id,
        )

        return ref
