from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="SetSettingRequest")


@_attrs_define
class SetSettingRequest:
    """What to store under the key the path names. The key is not a member here: it has one spelling, and a body carrying
    it too would give one request two anchors and a question about what to do when they disagree.

    THERE IS NO `actor`, unlike every issue mutation on this surface, and no guard member either. This plane records no
    history entry to attribute a write on and holds no row version to compare, so both would be members with nothing
    behind them.

        Attributes:
            value (str): The value to store, VERBATIM. It is not trimmed and not character-filtered: two of the keys this
                plane holds carry structured configuration a filter would corrupt.

                IT IS BOUNDED AT 65535 BYTES, which is the storage column, and the refusal is a `400` naming this member rather
                than the `500` the column would otherwise produce for a request the caller could have fixed. BYTES rather than
                characters, because that is how the column counts: 40000 multi-byte characters overflow it and 65000 ASCII ones
                do not. The 1 MiB body cap every operation shares still applies above this and is never the binding limit here.

                The bound is NOT the one `addComment`'s `text` carries, and the difference is what the two members are for. A
                comment is a document — a stack trace, a diff, a captured transcript — so its column is `LONGTEXT`. A setting is
                a value: nothing this plane holds is a megabyte of configuration, so the narrow bound is the honest description
                rather than a limitation to widen later.

                The empty string is a legal value and is stored. Read back it is INDISTINGUISHABLE from a key nothing ever set —
                `Setting.value` is absent for both — which is this plane's shipped conflation rather than something this
                operation introduces. A caller that means "remove it" sends `DELETE`.

                What comes back is this value, for every key this plane accepts: the one stored key with a normalization step is
                `issue_prefix`, which is also the one key this plane refuses, so no write through this door is transformed on
                its way in. The one thing the response may not repeat is a value the KEY marks credential-bearing; see the
                operation.
    """

    value: str

    def to_dict(self) -> dict[str, Any]:
        value = self.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "value": value,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        value = d.pop("value")

        set_setting_request = cls(
            value=value,
        )

        return set_setting_request
