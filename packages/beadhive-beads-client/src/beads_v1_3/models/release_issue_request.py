from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReleaseIssueRequest")


@_attrs_define
class ReleaseIssueRequest:
    """
    Attributes:
        actor (str): Who is releasing the claim. `ClaimRequest.actor`'s rules exactly: the server trims it, then refuses
            an empty result, anything longer than 256 BYTES (the `maxLength` above counts characters — the byte limit is the
            binding one), and any control character including newline. The value reaches the event the release records and
            the storage commit message, so an unvalidated newline would forge audit-trail lines.

            It is REQUIRED, and for one reason beyond the audit trail: a release is the moment work stops being owned, and
            the one question asked of its history entry afterwards is who let it go. On the unconditional path it is ALSO
            the ownership fence's subject — see the operation description.
        expected_assignee (str | Unset): Compare-and-set on the holder: the release proceeds only while the issue is
            still assigned to this actor, and otherwise refuses with `409` / `precondition_failed` naming this value, having
            written nothing.

            A MATCH REPLACES THE OWNERSHIP FENCE, so `actor` need not be the holder. Sending it beside `force` is a 400: the
            two are answers to the same question and they disagree.

            THE COMPARISON IS SEPARATOR-INSENSITIVE AND NOTHING ELSE. A run of `.`, `_` or `-` matches any other such run,
            so `agent-a`, `agent_a` and `agent.a` are one holder — that is deliberate, so a caller naming the holder under a
            different layer's spelling is a match rather than a mismatch. THE ONE EXCEPTION IS AN EXACT `--` RUN: that is
            gascity's session-name encoding of a rig-qualified agent's `/`, so it decodes to `/` instead of collapsing. `a--
            b` matches `a/b`, and no longer matches `a__b` or `a-b`. Those name different identities — `a--b` is the agent
            `b` on rig `a`, `a__b` is the dotted alias `a.b` — so treating them as one holder was a widening, and removing
            it is the point of the exception. Longer or mixed runs, `__` included, still collapse. NOTHING ELSE IS FORGIVEN:
            the value is not trimmed and not case-folded, so `" agent-a"` and `Agent-a` are both refusals. The server trims
            only far enough to tell a blank expectation from a real one and never sends the trimmed form on, so a caller
            that pads its expectation loses EVERY time rather than intermittently. Compose it from a holder a read gave you.

            THE EMPTY STRING IS A 400, and this is the one place this member disagrees with
            `UpdateIssueRequest.expected_assignee`, where an empty string is a real guard meaning "expected unassigned".
            Here "release a row nobody holds" describes no release at all; a caller that wants to assert a row is unheld is
            asking a READER a question, not asking this operation to do nothing. Absent, and only absent, selects the
            unconditional path.

            IT IS NOT LENGTH- OR PATTERN-BOUNDED the way `actor` is, and the asymmetry is deliberate: this value is COMPARED
            and never stored, so a value no assignee column could hold simply cannot match, and refusing it at the edge
            would be a refusal the role does not have.
        force (bool | Unset): Bypass the ownership fence, so an actor that is not the holder may release the claim. It
            is the escape hatch `bd unclaim --force` spells, for an abandoned claim whose holder crashed.

            IT BYPASSES THE FENCE AND NOTHING ELSE. It does not make an unheld row releasable, it does not make a closed one
            releasable, and it never bypasses a precondition — sending it beside `expected_assignee` is a 400 rather than a
            silent win for either. Default: False.
    """

    actor: str
    expected_assignee: str | Unset = UNSET
    force: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        expected_assignee = self.expected_assignee

        force = self.force

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
            }
        )
        if expected_assignee is not UNSET:
            field_dict["expected_assignee"] = expected_assignee
        if force is not UNSET:
            field_dict["force"] = force

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        expected_assignee = d.pop("expected_assignee", UNSET)

        force = d.pop("force", UNSET)

        release_issue_request = cls(
            actor=actor,
            expected_assignee=expected_assignee,
            force=force,
        )

        return release_issue_request
