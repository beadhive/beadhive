from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeleteIssuesRequest")


@_attrs_define
class DeleteIssuesRequest:
    """Which beads to erase, and what to do about the beads that point at them. There is no predicate here — no status, no
    cutoff, no glob — and that absence is the reason this operation needs no require-a-filter gate: a caller cannot
    spell "everything" without typing every id.

    `additionalProperties: false`, so an unknown member is a `400` naming the member. On this operation a silently
    ignored member is the difference between orphaning a dependent and deleting it.

        Attributes:
            ids (list[str]): The beads to delete, exact ids, in either plane. DUPLICATES COLLAPSE. An empty array is a `400`
                rather than a no-op — a caller whose id list came out empty because its own construction broke would read
                "deleted 0" and conclude the workspace was already clean.

                The cap is on the REQUEST rather than on what a cascade expands to: the whole delete is one transaction, so the
                practical bound is the backend's write timeout and no number here can promise it.
            actor (str | Unset): Caller-asserted attribution, under the same rules as `SweepRequest`'s `actor`: trimmed,
                refused when empty after trimming, over 256 BYTES, or carrying any control character. Optional — a deleted bead
                leaves no row to attribute the deletion on — but it does reach the SURVIVING beads whose text this operation
                rewrites, so a workspace that cares who rewrote a description sends one.
            cascade (bool | Unset): Also delete the transitive closure of everything that depends on the named beads. With
                `cascade` there is nothing left outside the set to orphan, so it makes `force` moot rather than conflicting with
                it: a request carrying both behaves as `cascade` and `orphaned` comes back empty. Default: False.
            force (bool | Unset): Delete the named beads and leave their dependents ORPHANED, reported in `orphaned`.
                Without it and without `cascade`, a named bead with a dependent the request did not name is refused.

                It defaults FALSE, which is the guarded mode, and the default is the protection. Authentication here is a
                deployment posture, and where it is configured it is a single shared bearer that admits a client to the WHOLE
                surface — it names no principal this operation could weigh and grants no narrower right — so an omitted member
                must not silently choose the answer that changes another bead's graph. Default: False.
            dry_run (bool | Unset): Report what the deletion WOULD do and change nothing. The counts and BOTH refusals are
                the ones the real request would produce, computed against the same snapshot, and nothing is recorded in history
                either. Default: False.
            expected_version (str | Unset): Requires the named bead's revision to equal this value before anything is
                erased. A miss refuses the whole request with `409 precondition_failed` and deletes NOTHING —
                `UpdateIssueRequest.expected_version`'s contract, on the operation where being wrong about which row you are
                looking at cannot be undone.

                IT REQUIRES A SINGLE-ID REQUEST. Sending it beside more than one DISTINCT id is a `400` naming this member,
                refused before anything is read. One token cannot describe two rows: the version space is per-row, so checking
                one number against a list would pass by coincidence for a list of never-written rows — every one of them holds 0
                — and fail forever for a list whose rows have since diverged. A guard that passes by coincidence and a guard
                nobody can satisfy are one defect seen from two sides. Delete one bead per guarded request; the per-id shape a
                batch would need is a token PER id, which is a different request type.

                DUPLICATES COLLAPSE FIRST, so `{"ids":["be-1","be-1"], "expected_version":N}` names one bead and is legal. The
                refusal counts DISTINCT ids, not mentions, exactly as the library surface does.

                NEITHER `cascade` NOR `force` BYPASSES IT. Both bypass POLICY — the dependents guard — and never a precondition.
                Under `cascade` the guard still covers only the NAMED bead: the closure is resolved inside the deleting
                transaction, so a matching token promises the row is the one you read and promises nothing about how far the
                closure has grown since. A caller that needs the closure itself pinned wants `dry_run` first.

                IT GUARDS LIFECYCLE STATE, NOT THE GRAPH. The token is reminted by status, assignee and started-at writes and
                deliberately not by label, dependency or rename writes, so a match does not promise the bead's edges are the
                ones you saw.

                The token is the `revision` a lifecycle write answers with, and the one `GET /v0/beads/issues/{id}` publishes —
                which is where a delete guard should seed itself, since reading the bead before erasing it is the only way to be
                sure it is the bead you meant.

                IT IS A STRING, and it must be the `revision` string a response carried, verbatim. A JSON number — or any other
                type — is a `400` naming this member. The token spans the FULL 64-bit range, so a number would be rounded past
                2^53 by an IEEE-754-double parser and the guard would miss a row nothing else touched; a string round-trips
                exactly in every consumer.
    """

    ids: list[str]
    actor: str | Unset = UNSET
    cascade: bool | Unset = False
    force: bool | Unset = False
    dry_run: bool | Unset = False
    expected_version: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        ids = self.ids

        actor = self.actor

        cascade = self.cascade

        force = self.force

        dry_run = self.dry_run

        expected_version = self.expected_version

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "ids": ids,
            }
        )
        if actor is not UNSET:
            field_dict["actor"] = actor
        if cascade is not UNSET:
            field_dict["cascade"] = cascade
        if force is not UNSET:
            field_dict["force"] = force
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if expected_version is not UNSET:
            field_dict["expected_version"] = expected_version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ids = cast(list[str], d.pop("ids"))

        actor = d.pop("actor", UNSET)

        cascade = d.pop("cascade", UNSET)

        force = d.pop("force", UNSET)

        dry_run = d.pop("dry_run", UNSET)

        expected_version = d.pop("expected_version", UNSET)

        delete_issues_request = cls(
            ids=ids,
            actor=actor,
            cascade=cascade,
            force=force,
            dry_run=dry_run,
            expected_version=expected_version,
        )

        return delete_issues_request
