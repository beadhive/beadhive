from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="ClaimRequest")


@_attrs_define
class ClaimRequest:
    """
    Attributes:
        actor (str): Who is claiming the issue. The server trims it, then refuses an empty result, anything longer than
            256 BYTES (the `maxLength` above counts characters — the byte limit is the binding one), and any control
            character including newline: Unicode category Cc — C0, DEL and the C1 block — plus the U+2028/U+2029 line
            separators, which is the set the `pattern` above spells.

            The value is persisted as the assignee and interpolated into the storage commit message, so an unvalidated
            newline would forge audit-trail lines. C1 is refused for that same reason and not for tidiness: U+0085 is a line
            break on a VT-conformant terminal, and U+009B is the one-byte CSI introducer, which would make an actor an
            escape-sequence payload in anything that prints an assignee.
    """

    actor: str

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        claim_request = cls(
            actor=actor,
        )

        return claim_request
