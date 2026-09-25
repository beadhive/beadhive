from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReopenIssueRequest")


@_attrs_define
class ReopenIssueRequest:
    """
    Attributes:
        actor (str): Who is reopening the issue. `ClaimRequest.actor`'s rules exactly: the server trims it, then refuses
            an empty result, anything longer than 256 BYTES (the `maxLength` above counts characters — the byte limit is the
            binding one), and any control character including newline. The value reaches the `reopened` event's attribution
            and the storage commit message, so an unvalidated newline would forge audit-trail lines.
        reason (str | Unset): Why the issue is being reopened. Recorded on the `reopened` EVENT this move records — not
            on a field of the issue, and not carried in the response, so a caller that wants it back reads the issue's
            events. Refused for control characters, and bounded by what the column holds rather than by the number above.
        expected_version (str | Unset): Requires the row's revision to equal this value BEFORE the reopen. A miss
            refuses the whole request with `409 precondition_failed` and writes nothing —
            `CloseIssueRequest.expected_version`'s contract, on the close's mirror.

            IT IS CHECKED BEFORE THE NON-DONE NO-OP, the mirror of the close's check-before-the-idempotent-re-close, and for
            the same reason: a reopen of a row somebody else has moved is a `409` rather than the 200-with-`already_open`
            the same body earns unguarded, which is what lets `already_open` be read as "nothing has happened here since".

            The token is the `revision` this operation's own response carries; compose the next expectation from a value a
            write ANSWERED with and never from one the client computed.

            IT IS A STRING, and it must be the `revision` string a response carried, verbatim. A JSON number — or any other
            type — is a `400` naming this member. The token spans the FULL 64-bit range, so a number would be rounded past
            2^53 by an IEEE-754-double parser and the guard would miss a row nothing else touched; a string round-trips
            exactly in every consumer.
    """

    actor: str
    reason: str | Unset = UNSET
    expected_version: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        reason = self.reason

        expected_version = self.expected_version

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason
        if expected_version is not UNSET:
            field_dict["expected_version"] = expected_version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        reason = d.pop("reason", UNSET)

        expected_version = d.pop("expected_version", UNSET)

        reopen_issue_request = cls(
            actor=actor,
            reason=reason,
            expected_version=expected_version,
        )

        return reopen_issue_request
