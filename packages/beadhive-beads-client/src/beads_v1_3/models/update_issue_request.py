from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.issue_patch_body import IssuePatchBody


T = TypeVar("T", bound="UpdateIssueRequest")


@_attrs_define
class UpdateIssueRequest:
    """
    Attributes:
        actor (str): Who is editing the issue. `ClaimRequest.actor`'s rules exactly: the server trims it, then refuses
            an empty result, anything longer than 256 BYTES (the `maxLength` above counts characters — the byte limit is the
            binding one), and any control character including newline. The value reaches the history entry's attribution and
            the storage commit message, so an unvalidated newline would forge audit-trail lines.
        patch (IssuePatchBody): The fields to write. Every member is optional and PRESENCE is the signal: a member
            present is written, a member absent is untouched. An empty object is a `400` — a write that writes nothing is a
            client bug.

            This is a deliberate SUBSET of the fields an issue carries; the members it does not spell are future surface
            rather than oversights, and `updateIssue`'s own description says which and why.

            It now agrees with `ApplyPatchBody` on every member it publishes, and the two differ only in the SHAPE of two of
            them: `labels` is complete replacement here and an ordered add/remove/replace patch there, because that
            operation edits a set it did not compose. Everything else — down to the `metadata` algebra and the four nullable
            members — is one definition, so a caller cannot get a different answer for the same edit depending on which
            operation it sent.
        expected_version (str | Unset): Requires the row's revision to equal this value before the patch. A miss refuses
            the WHOLE request with `409 precondition_failed` and writes nothing — `ApplyUpdateItem.expected_version`'s
            contract, on the operation that patches one row.

            The token is the `revision` this operation's own response carries, and the same one `GET /v0/beads/issues/{id}`
            publishes — which is where a first guarded write seeds itself, rather than from an unguarded one or from `POST
            /v0/beads/issues:batchApply`'s `ApplyItemResult.revision`. Compose the next expectation from the value the write
            ANSWERED with, never from one the client composed itself: the token is OPAQUE and compared for equality alone,
            so it has no predecessor a client can compute.

            IT IS A STRING, and it must be the `revision` string a response carried, verbatim. A JSON number — or any other
            type — is a `400` naming this member. The token spans the FULL 64-bit range, so a number would be rounded past
            2^53 by an IEEE-754-double parser and the guard would miss a row nothing else touched; a string round-trips
            exactly in every consumer.
        expected_status (str | Unset): Requires the issue's status to equal this value before the patch. A miss refuses
            the whole request with `409 precondition_failed`.

            Unlike `expected_version` this one is readable: `Issue.status` is on every read of this surface, so a caller can
            guard a status transition without any token at all.
        expected_assignee (str | Unset): Requires the issue's assignee to equal this value before the patch. A match
            AUTHORIZES the requested `patch.assignee` transfer: this compare-and-set replaces the ordinary anti-steal fence,
            so it must not be combined with `force_assignee_transfer`. A miss refuses the whole request with `409
            precondition_failed`.
        force_close_policy (bool | Unset): Bypasses ONLY close policy — the open-children refusal and the live blocker
            refusal — for a `patch.status` that crosses into the workspace's done category. It has no effect without such a
            status change, and it never bypasses validation, the preconditions above, or the assignee fence. Default: False.
        force_assignee_transfer (bool | Unset): Bypasses ONLY a genuine transfer away from a live foreign in-progress
            owner. Reasserting the exact current assignee is idempotent and needs no force. It requires `patch.assignee` — a
            request setting it without one is a `400` — and it must be false when `expected_assignee` is sent. Default:
            False.
    """

    actor: str
    patch: IssuePatchBody
    expected_version: str | Unset = UNSET
    expected_status: str | Unset = UNSET
    expected_assignee: str | Unset = UNSET
    force_close_policy: bool | Unset = False
    force_assignee_transfer: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        patch = self.patch.to_dict()

        expected_version = self.expected_version

        expected_status = self.expected_status

        expected_assignee = self.expected_assignee

        force_close_policy = self.force_close_policy

        force_assignee_transfer = self.force_assignee_transfer

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
                "patch": patch,
            }
        )
        if expected_version is not UNSET:
            field_dict["expected_version"] = expected_version
        if expected_status is not UNSET:
            field_dict["expected_status"] = expected_status
        if expected_assignee is not UNSET:
            field_dict["expected_assignee"] = expected_assignee
        if force_close_policy is not UNSET:
            field_dict["force_close_policy"] = force_close_policy
        if force_assignee_transfer is not UNSET:
            field_dict["force_assignee_transfer"] = force_assignee_transfer

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.issue_patch_body import IssuePatchBody  # noqa: PLC0415

        d = dict(src_dict)
        actor = d.pop("actor")

        patch = IssuePatchBody.from_dict(d.pop("patch"))

        expected_version = d.pop("expected_version", UNSET)

        expected_status = d.pop("expected_status", UNSET)

        expected_assignee = d.pop("expected_assignee", UNSET)

        force_close_policy = d.pop("force_close_policy", UNSET)

        force_assignee_transfer = d.pop("force_assignee_transfer", UNSET)

        update_issue_request = cls(
            actor=actor,
            patch=patch,
            expected_version=expected_version,
            expected_status=expected_status,
            expected_assignee=expected_assignee,
            force_close_policy=force_close_policy,
            force_assignee_transfer=force_assignee_transfer,
        )

        return update_issue_request
