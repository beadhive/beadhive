from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..models.sweep_request_tier import SweepRequestTier
from ..types import UNSET, Unset

T = TypeVar("T", bound="SweepRequest")


@_attrs_define
class SweepRequest:
    """Which closed beads to clear. The predicate is FIXED at "closed beads of one tier" and the two narrowing members only
    narrow it: there is no status, no assignee, no label and no free-text query here, because every one of those would
    be another way to spell a destructive selection that a caller could get subtly wrong.

    `additionalProperties: false`, so an unknown member is a `400` naming the member — the same posture the query-
    parameter rule takes, and for the same reason: on this operation a silently ignored narrowing term widens what is
    erased.

        Attributes:
            tier (SweepRequestTier): Which plane to clear. `ephemeral` is the wisp tier (`bd purge`) and `durable` is the
                issue tier (`bd prune`). The two are DISJOINT: a sweep of one can never touch a bead of the other. Required,
                with no default — a caller handed the wrong tier has nothing to notice until the beads are gone.
            actor (str | Unset): Caller-asserted attribution for wherever the backend records it, under the same rules and
                for the same reasons as `ClaimRequest`'s `actor`: trimmed, refused when empty after trimming, over 256 BYTES, or
                carrying any control character. Optional — a deleted bead leaves no row to attribute the deletion on.
            closed_before (datetime.datetime | Unset): Keep only beads closed STRICTLY BEFORE this instant (RFC 3339). A
                bead closed exactly at it is kept, which is the half-open interval every other time bound on this surface uses.
                `bd prune --older-than 30d` resolves the duration itself and sends the resulting instant.
            pattern (str | Unset): Keep only beads whose id matches this shell glob (`*`, `?`, `[...]`, `\\` escapes; `*`
                also crosses `-` and `.`, since an id is not a path). Absent matches every bead in the tier. A MALFORMED glob is
                a `400`, never a pattern that matches nothing.
            protect_referenced (bool | Unset): Skip candidates whose id is CITED — as a literal, at word boundaries — in the
                description, notes or comments of any bead that is not done, so a decision trail a live bead still points at is
                not deleted out from under it.

                It DEFAULTS ON here, unlike the library default, and that is deliberate. This is the only destructive operation
                on the surface, and the bearer a deployment may configure is not a per-caller right: one shared token admits a
                client to everything published here, so being authenticated says nothing about whether this particular deletion
                was meant. A caller that omits the member must therefore not get weaker protection than the operator typing `bd
                prune`, which protects unless `--ignore-references`. The inverse — opt OUT locally, opt IN remotely — is the
                shape that lets a stray request delete a decision trail nothing brings back.

                It costs a full scan of the not-done set and its comments. Sending `protect_referenced: false` buys the cheaper
                sweep, and asking for it explicitly is the point: that is the request that should be the deliberate one.
                Default: True.
            dry_run (bool | Unset): Report what the sweep WOULD do and delete nothing. The counts, the skips and the
                refusals are the same ones the real sweep would produce, computed against the same snapshot — and nothing is
                recorded in history either. Default: False.
    """

    tier: SweepRequestTier
    actor: str | Unset = UNSET
    closed_before: datetime.datetime | Unset = UNSET
    pattern: str | Unset = UNSET
    protect_referenced: bool | Unset = True
    dry_run: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        tier = self.tier.value

        actor = self.actor

        closed_before: str | Unset = UNSET
        if not isinstance(self.closed_before, Unset):
            closed_before = self.closed_before.isoformat()

        pattern = self.pattern

        protect_referenced = self.protect_referenced

        dry_run = self.dry_run

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "tier": tier,
            }
        )
        if actor is not UNSET:
            field_dict["actor"] = actor
        if closed_before is not UNSET:
            field_dict["closed_before"] = closed_before
        if pattern is not UNSET:
            field_dict["pattern"] = pattern
        if protect_referenced is not UNSET:
            field_dict["protect_referenced"] = protect_referenced
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        tier = SweepRequestTier(d.pop("tier"))

        actor = d.pop("actor", UNSET)

        _closed_before = d.pop("closed_before", UNSET)
        closed_before: datetime.datetime | Unset
        if isinstance(_closed_before, Unset):
            closed_before = UNSET
        else:
            closed_before = datetime.datetime.fromisoformat(_closed_before)

        pattern = d.pop("pattern", UNSET)

        protect_referenced = d.pop("protect_referenced", UNSET)

        dry_run = d.pop("dry_run", UNSET)

        sweep_request = cls(
            tier=tier,
            actor=actor,
            closed_before=closed_before,
            pattern=pattern,
            protect_referenced=protect_referenced,
            dry_run=dry_run,
        )

        return sweep_request
