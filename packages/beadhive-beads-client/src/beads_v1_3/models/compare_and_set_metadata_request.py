from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CompareAndSetMetadataRequest")


@_attrs_define
class CompareAndSetMetadataRequest:
    """
    Attributes:
        actor (str): Who is performing the swap. `ClaimRequest.actor`'s rules exactly: the server trims it, then refuses
            an empty result, anything longer than 256 BYTES (the `maxLength` above counts characters — the byte limit is the
            binding one), and any control character including newline. It reaches the update event's attribution and the
            storage commit message, so an unvalidated newline would forge audit-trail lines.

            It is REQUIRED here rather than optional, because a swap is a coordination write between racing callers and the
            one question asked of its history entry afterwards is which of them won.
        key (str): The single metadata key to read and write. It must match the workspace's metadata-key syntax — a
            letter or underscore, then letters, digits, underscores, dots and slashes — so a key the query layer could not
            later spell is refused rather than written.

            ONE KEY, NOT A PATH: a dotted key like `gc.lease` names a top-level key spelled with a dot, not a nested field.
            The metadata object's nesting is VALUE structure, and this operation swaps whole values.
        expected (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
            because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
            string, and a client must not decode it as one.

            Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
            and holds null. Those are different states and this surface reports both.
        value (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
            because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
            string, and a client must not decode it as one.

            Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
            and holds null. Those are different states and this surface reports both.
    """

    actor: str
    key: str
    expected: Any | Unset = UNSET
    value: Any | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        key = self.key

        expected = self.expected

        value = self.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
                "key": key,
            }
        )
        if expected is not UNSET:
            field_dict["expected"] = expected
        if value is not UNSET:
            field_dict["value"] = value

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        key = d.pop("key")

        expected = d.pop("expected", UNSET)

        value = d.pop("value", UNSET)

        compare_and_set_metadata_request = cls(
            actor=actor,
            key=key,
            expected=expected,
            value=value,
        )

        return compare_and_set_metadata_request
