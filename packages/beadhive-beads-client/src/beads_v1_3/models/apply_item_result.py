from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.apply_item_result_kind import ApplyItemResultKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="ApplyItemResult")


@_attrs_define
class ApplyItemResult:
    """What ONE item did, at the index the item occupied.

    IT IS LEAN, AND CARRIES NO ISSUE. Every other write on this surface answers with the stored row; this one answers
    with ids and a revision, and a client that wants the rows reads them back. A hundred hydrated issues with their
    labels and edges is a response an order of magnitude larger than the request that produced it, and no client needs
    all of them: the ids are what a plan's next step is composed from. The library contract behind this operation DOES
    carry a post-item snapshot, because its completion hooks hand a script the row it is being told about — and hooks
    never fire on this surface at all, which is exactly why the snapshot stops here.

        Attributes:
            kind (ApplyItemResultKind): Echoes the item's kind, so a caller walking the results does not have to walk the
                request alongside them.
            issue_id (str): The row the item acted on: the minted or explicit id for a `create`, the resolved target for an
                `update` or a `close`, and the edge's SOURCE for a `dep_add`.
            changed (bool): Whether this item persisted a semantic mutation. A `create` is always true. An `update` and a
                `close` follow their own operations' `changed`/`already_closed` answers, and a `dep_add` is false for an
                idempotent re-add of an edge that already existed with the same type.
            revision (str): The row's optimistic-concurrency token AFTER the item, and the value an `expected_version` guard
                is composed from. The same member the CLI's detail view publishes under this name; it is not a new word.

                IT IS EQUALITY-ONLY: compare it, never order or interpret it. A change signals the row was mutated since you
                read it, and nothing more — it is a random value the engine rewrites, not a counter.

                ITS COVERAGE IS PARTIAL, and the partiality is inherited rather than introduced: the token is rewritten by
                claim, close, unclaim and the generic update path, and NOT by the direct-update paths that rewrite text without
                touching it. A client needing complete change detection combines it with `updated_at`, `status` and the label
                set.

                It is ALWAYS PRESENT, including as 0. Zero is a real value — a legacy row backfilled and not mutated since — and
                a `dep_add` is 0 too, because an edge acts on no single row's version. An absent member would be ambiguous
                between the two.

                IT IS A STRING, the token's decimal spelling — `"-3819021935081927"`, or `"0"` for a legacy migration-0054 row.
                Send it back verbatim as an `expected_version`; do not parse it into a number. A JSON number would not survive
                the trip: the token spans the FULL 64-bit range, and an IEEE-754-double parser — JavaScript's `JSON.parse`, Go's
                `any`, Python's `float` — rounds anything past 2^53 to a value NEAR the token that is not it, so a guard
                composed from it is refused against a row nothing else touched. A string round-trips exactly in every consumer.
            depends_on_id (str | Unset): The edge's target. Present for `dep_add` and ABSENT for every other kind, which act
                on a row rather than on a pair.
    """

    kind: ApplyItemResultKind
    issue_id: str
    changed: bool
    revision: str
    depends_on_id: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        issue_id = self.issue_id

        changed = self.changed

        revision = self.revision

        depends_on_id = self.depends_on_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "issue_id": issue_id,
                "changed": changed,
                "revision": revision,
            }
        )
        if depends_on_id is not UNSET:
            field_dict["depends_on_id"] = depends_on_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = ApplyItemResultKind(d.pop("kind"))

        issue_id = d.pop("issue_id")

        changed = d.pop("changed")

        revision = d.pop("revision")

        depends_on_id = d.pop("depends_on_id", UNSET)

        apply_item_result = cls(
            kind=kind,
            issue_id=issue_id,
            changed=changed,
            revision=revision,
            depends_on_id=depends_on_id,
        )

        apply_item_result.additional_properties = d
        return apply_item_result

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
