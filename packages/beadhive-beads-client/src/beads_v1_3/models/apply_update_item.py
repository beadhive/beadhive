from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.apply_patch_body import ApplyPatchBody
    from ..models.ref import Ref


T = TypeVar("T", bound="ApplyUpdateItem")


@_attrs_define
class ApplyUpdateItem:
    """Patches one existing issue, under `PATCH /v0/beads/issues/{id}`'s rules.

    The two carry the same preconditions and the same force flags; what is this operation's alone is that its guards
    evaluate AS-MODIFIED — against the row as earlier items of this same request have already changed it — and that a
    miss takes the whole plan down rather than one write.

        Attributes:
            target (Ref): Names ONE issue, either by an id that already exists or by the `key` a create item earlier in the
                same request gave itself.

                EXACTLY ONE OF THE TWO IS SET, and both cases the schema cannot express are a `400`: both members set is a
                caller that cannot say which it meant, and neither set is a reference to nothing. (Spelling that as a schema
                alternation would need `oneOf`, which this document does not use — see `ApplyItem`.)

                A KEY REACHES BACKWARD ONLY where the ref ADDRESSES a row — an `update.target`, a `close.target`, either
                endpoint of a `dep_add`. The one exception is `create.metadata_refs`, whose values may reach forward or name
                their own item's key; the operation's description says why.
            patch (ApplyPatchBody): The fields an `update` item writes. Every member is optional and PRESENCE is the signal:
                a member present is written, a member absent is untouched. An empty object is a `400` — a write that writes
                nothing is a client bug.

                It mirrors `IssuePatchBody` member for member and diverges in exactly two places now that `PATCH
                /v0/beads/issues/{id}` publishes `status`, `assignee` and the same `metadata` algebra.

                `owner` is published here and not there, which is an accident of order rather than a decision: nothing has asked
                for it on the single patch.

                `labels` is a full patch rather than a complete replacement, and that one is a real difference: a plan has to be
                able to REMOVE one label without knowing the rest of the set, because it edits a set it did not compose. A
                caller patching one row it just read already knows the set.

                `parent_id` is deliberately absent, and its absence is this operation's one-edge-one-spelling rule: a parent is
                a `dep_add` item of type `parent-child`, so the order of every edge in the request stays total. The single patch
                has no ordering to express and publishes it directly. `persistence` is absent from both — moving a row between
                planes mid-plan is a different act from writing its fields, and nothing has asked for it here.
            expected_version (str | Unset): Requires the row's `revision` to equal this value before the patch. A miss
                refuses the WHOLE request with `409 precondition_failed`.

                IT IS A `400`, NOT A `409`, ON A ROW THIS REQUEST HAS ALREADY WRITTEN — including one an earlier item created.
                The token is minted by the write, so mid-request there is no value a caller could send: the pre-request token is
                stale by construction and a row this request just created never had one the caller could read. Refusing
                statically says so; answering with a mismatch would send the caller looking for a concurrent writer that does
                not exist.

                `expected_status` and `expected_assignee` carry no such rule, because a caller CAN know what its own earlier
                item set them to.

                IT IS A STRING, and it must be the `revision` string a response carried, verbatim. A JSON number — or any other
                type — is a `400` naming this member. The token spans the FULL 64-bit range, so a number would be rounded past
                2^53 by an IEEE-754-double parser and the guard would miss a row nothing else touched; a string round-trips
                exactly in every consumer.
            expected_status (str | Unset): Requires the issue's status to equal this value, evaluated AS-MODIFIED — against
                the row as this request has already changed it at this item's position. A miss refuses the whole request with
                `409 precondition_failed`.
            expected_assignee (str | Unset): Requires the issue's assignee to equal this value, evaluated as-modified. A
                match AUTHORIZES the requested `patch.assignee` transfer: this compare-and-set replaces the ordinary anti-steal
                fence, so it must not be combined with `force_assignee_transfer`. A miss refuses the whole request with `409
                precondition_failed`.
            force_close_policy (bool | Unset): Bypasses ONLY close policy — the open-children refusal and the live blocker
                refusal — for a `patch.status` that crosses into the workspace's done category. It has no effect without such a
                status change, and it never bypasses validation, the preconditions above, or the assignee fence. Default: False.
            force_assignee_transfer (bool | Unset): Bypasses ONLY a genuine transfer away from a live foreign in-progress
                owner. Reasserting the exact current assignee is idempotent and needs no force. It requires `patch.assignee` — a
                request setting it without one is a `400` — and it must be false when `expected_assignee` is sent. Default:
                False.
    """

    target: Ref
    patch: ApplyPatchBody
    expected_version: str | Unset = UNSET
    expected_status: str | Unset = UNSET
    expected_assignee: str | Unset = UNSET
    force_close_policy: bool | Unset = False
    force_assignee_transfer: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        target = self.target.to_dict()

        patch = self.patch.to_dict()

        expected_version = self.expected_version

        expected_status = self.expected_status

        expected_assignee = self.expected_assignee

        force_close_policy = self.force_close_policy

        force_assignee_transfer = self.force_assignee_transfer

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "target": target,
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
        from ..models.apply_patch_body import ApplyPatchBody  # noqa: PLC0415
        from ..models.ref import Ref  # noqa: PLC0415

        d = dict(src_dict)
        target = Ref.from_dict(d.pop("target"))

        patch = ApplyPatchBody.from_dict(d.pop("patch"))

        expected_version = d.pop("expected_version", UNSET)

        expected_status = d.pop("expected_status", UNSET)

        expected_assignee = d.pop("expected_assignee", UNSET)

        force_close_policy = d.pop("force_close_policy", UNSET)

        force_assignee_transfer = d.pop("force_assignee_transfer", UNSET)

        apply_update_item = cls(
            target=target,
            patch=patch,
            expected_version=expected_version,
            expected_status=expected_status,
            expected_assignee=expected_assignee,
            force_close_policy=force_close_policy,
            force_assignee_transfer=force_assignee_transfer,
        )

        return apply_update_item
