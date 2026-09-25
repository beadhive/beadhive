from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="ClaimNextRequest")


@_attrs_define
class ClaimNextRequest:
    """
    Attributes:
        actor (str): Who is claiming. `ClaimRequest.actor`'s rules exactly: the server trims it, then refuses an empty
            result, anything longer than 256 BYTES (the `maxLength` above counts characters — the byte limit is the binding
            one), and any control character including newline. The value is persisted as the assignee and interpolated into
            the storage commit message, so an unvalidated newline would forge audit-trail lines.

            IT IS THE ONLY BODY MEMBER, and the FILTER travels in the query string instead. That split is deliberate: the
            filter vocabulary is `GET /v0/beads/ready`'s and is decoded by the same function, so re-spelling it as a body
            object would create a second expression of one predicate — and two spellings of one predicate eventually
            disagree. The actor cannot go the same way: it is provenance that lands in a column, and this surface has always
            carried that in a body.
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

        claim_next_request = cls(
            actor=actor,
        )

        return claim_next_request
