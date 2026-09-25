from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ref import Ref


T = TypeVar("T", bound="ApplyCloseItem")


@_attrs_define
class ApplyCloseItem:
    """Closes one existing issue, under `POST /v0/beads/issues/{id}:close`'s rules including first-close-wins.

    Attributes:
        target (Ref): Names ONE issue, either by an id that already exists or by the `key` a create item earlier in the
            same request gave itself.

            EXACTLY ONE OF THE TWO IS SET, and both cases the schema cannot express are a `400`: both members set is a
            caller that cannot say which it meant, and neither set is a reference to nothing. (Spelling that as a schema
            alternation would need `oneOf`, which this document does not use — see `ApplyItem`.)

            A KEY REACHES BACKWARD ONLY where the ref ADDRESSES a row — an `update.target`, a `close.target`, either
            endpoint of a `dep_add`. The one exception is `create.metadata_refs`, whose values may reach forward or name
            their own item's key; the operation's description says why.
        reason (str | Unset): Why the issue is closed, stored and read back as `close_reason`. THE FIRST CLOSE WINS: an
            idempotent re-close writes neither this nor `session`.
        session (str | Unset): The working session that closed the issue, stored and read back as `closed_by_session`,
            under the same first-close-wins rule.
        force (bool | Unset): Bypasses close policy — the open-children refusal and the live-blocker refusal — and
            nothing else.

            CLOSE POLICY EVALUATES AT THIS ITEM, against the row as this request has already changed it. A LATER item that
            gives a closed parent an open child is NOT refused: the policy is a gate on the closing act, not an invariant
            the store maintains. Default: False.
        expected_version (str | Unset): Requires the row's `revision` to equal this value, evaluated as-modified and
            checked before the idempotent close. A miss refuses the whole request with `409 precondition_failed`, and
            `ApplyUpdateItem.expected_version`'s already-written rule applies here identically.

            THERE IS DELIBERATELY NO `expected_status` HERE. A close is idempotent — re-closing a closed issue is `changed:
            false` — so a guard spelled to refuse an already-closed row is asking for a REFUSAL where this verb answers with
            a no-op. That belongs on an `update` item whose `patch.status` crosses into the done category.

            IT IS A STRING, and it must be the `revision` string a response carried, verbatim. A JSON number — or any other
            type — is a `400` naming this member. The token spans the FULL 64-bit range, so a number would be rounded past
            2^53 by an IEEE-754-double parser and the guard would miss a row nothing else touched; a string round-trips
            exactly in every consumer.
    """

    target: Ref
    reason: str | Unset = UNSET
    session: str | Unset = UNSET
    force: bool | Unset = False
    expected_version: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        target = self.target.to_dict()

        reason = self.reason

        session = self.session

        force = self.force

        expected_version = self.expected_version

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "target": target,
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
        from ..models.ref import Ref  # noqa: PLC0415

        d = dict(src_dict)
        target = Ref.from_dict(d.pop("target"))

        reason = d.pop("reason", UNSET)

        session = d.pop("session", UNSET)

        force = d.pop("force", UNSET)

        expected_version = d.pop("expected_version", UNSET)

        apply_close_item = cls(
            target=target,
            reason=reason,
            session=session,
            force=force,
            expected_version=expected_version,
        )

        return apply_close_item
