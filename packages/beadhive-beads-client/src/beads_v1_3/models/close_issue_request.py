from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CloseIssueRequest")


@_attrs_define
class CloseIssueRequest:
    """
    Attributes:
        actor (str): Who is closing the issue. `ClaimRequest.actor`'s rules exactly: the server trims it, then refuses
            an empty result, anything longer than 256 BYTES (the `maxLength` above counts characters — the byte limit is the
            binding one), and any control character including newline. The value reaches stored columns, event-stream
            attribution and the storage commit message, so an unvalidated newline would forge audit-trail lines.
        reason (str | Unset): Why the issue is closed. Stored on the issue and read back as `close_reason`. THE FIRST
            CLOSE WINS: an idempotent re-close writes neither this nor `session`, so a replayed close cannot rewrite the
            record of why the work ended. Refused for control characters, and bounded by what the column holds rather than
            by the number above.
        session (str | Unset): The working session that closed the issue, stored and read back as `closed_by_session`,
            under the same first-close-wins rule and the same bounds as `reason`.
        force (bool | Unset): Bypass close policy — the open-children refusal and the live-blocker refusal — and nothing
            else. The refusals are the ROLE's, so this endpoint cannot skip a guard by forgetting one exists. A forced close
            still reports `open_children`.

            IT BYPASSES POLICY, NEVER A PRECONDITION. `expected_version` is still checked with it set, for the reason
            `issueops.CloseRequest.Force` gives: a caller saying "close it anyway" has said nothing about whether the row is
            still the one it read. Default: False.
        expected_version (str | Unset): Requires the row's revision to equal this value BEFORE the close. A miss refuses
            the whole request with `409 precondition_failed` and writes nothing — `UpdateIssueRequest.expected_version`'s
            contract, on the operation that closes one row.

            IT IS CHECKED BEFORE THE IDEMPOTENT RE-CLOSE, which is the one place this guard differs from the update's. A re-
            close of a row somebody else has moved since the caller read it is a `409` and not the 200-with-`already_closed`
            the same body earns without a guard: a replay whose premise has expired is a refusal the caller wants to see,
            and it is the only way `already_closed` can be trusted as "nothing has happened here since".

            The token is the `revision` this operation's own response carries. Compose the next expectation from the value a
            write ANSWERED with, never from one the client composed itself: the token is OPAQUE and compared for equality
            alone, so it has no predecessor a client can compute. A first guarded close seeds itself from `GET
            /v0/beads/issues/{id}`'s `revision` — the read that sources a guard — or, for a chain already mid-flight, from
            an unguarded lifecycle write or `POST /v0/beads/issues:batchApply`'s `ApplyItemResult.revision`.

            IT IS A STRING, and it must be the `revision` string a response carried, verbatim. A JSON number — or any other
            type — is a `400` naming this member. The token spans the FULL 64-bit range, so a number would be rounded past
            2^53 by an IEEE-754-double parser and the guard would miss a row nothing else touched; a string round-trips
            exactly in every consumer.
    """

    actor: str
    reason: str | Unset = UNSET
    session: str | Unset = UNSET
    force: bool | Unset = False
    expected_version: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        reason = self.reason

        session = self.session

        force = self.force

        expected_version = self.expected_version

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason
        if session is not UNSET:
            field_dict["session"] = session
        if force is not UNSET:
            field_dict["force"] = force
        if expected_version is not UNSET:
            field_dict["expected_version"] = expected_version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        reason = d.pop("reason", UNSET)

        session = d.pop("session", UNSET)

        force = d.pop("force", UNSET)

        expected_version = d.pop("expected_version", UNSET)

        close_issue_request = cls(
            actor=actor,
            reason=reason,
            session=session,
            force=force,
            expected_version=expected_version,
        )

        return close_issue_request
