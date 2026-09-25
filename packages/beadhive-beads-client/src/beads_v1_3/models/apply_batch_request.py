from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.apply_item import ApplyItem


T = TypeVar("T", bound="ApplyBatchRequest")


@_attrs_define
class ApplyBatchRequest:
    """
    Attributes:
        actor (str): Who is applying the plan, under `ClaimRequest.actor`'s rules and for the same reasons: the server
            trims it, refuses an empty result, anything longer than 256 BYTES (the `maxLength` above counts characters — the
            byte limit is the binding one), and any control character including newline.

            It is attributed to every item and to the ONE history entry the request records, because a batch is one act by
            one caller.
        items (list[ApplyItem]): The items to apply, IN THE ORDER THEY ARE TO BE APPLIED. An empty array is a `400`
            rather than a successful no-op: a write request that writes nothing is a client bug, and answering it cheerfully
            is how a client whose own plan filtered to nothing silently stops writing.

            The 100-item cap bounds how long one request may hold a write transaction, not batch semantics. Split a larger
            plan; each request is atomic on its own — but splitting it changes what the end gate can see, since the gate
            runs over one request at a time.

            A per-item refusal names its offender as `items[i].kind.member`.
        provenance (str | Unset): Labels the version-control history entry this request records, under `updateIssue`'s
            rule: it changes how the entry READS, never whether one is recorded. Empty composes a default naming how many
            items of each kind landed and no ids.
        force_id_prefix (bool | Unset): Permits an explicit `create.id` outside the workspace's configured issue prefix,
            for EVERY create item in the request. Without it such an id is refused by the role and arrives as a `400`.
            Default: False.
        skip_per_edge_cycle_check (bool | Unset): Drops the PER-EDGE cycle probe for a caller wiring a large graph,
            exactly as it does on `POST /v0/beads/dependencies:add`.

            IT NEVER DROPS THE END GATE, which runs once after every item and re-validates the whole graph this request
            built, and it never drops the self-dependency refusal. It trades per-edge attribution for speed, not validation
            for speed. Default: False.
    """

    actor: str
    items: list[ApplyItem]
    provenance: str | Unset = UNSET
    force_id_prefix: bool | Unset = False
    skip_per_edge_cycle_check: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        provenance = self.provenance

        force_id_prefix = self.force_id_prefix

        skip_per_edge_cycle_check = self.skip_per_edge_cycle_check

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
                "items": items,
            }
        )
        if provenance is not UNSET:
            field_dict["provenance"] = provenance
        if force_id_prefix is not UNSET:
            field_dict["force_id_prefix"] = force_id_prefix
        if skip_per_edge_cycle_check is not UNSET:
            field_dict["skip_per_edge_cycle_check"] = skip_per_edge_cycle_check

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.apply_item import ApplyItem  # noqa: PLC0415

        d = dict(src_dict)
        actor = d.pop("actor")

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = ApplyItem.from_dict(items_item_data)

            items.append(items_item)

        provenance = d.pop("provenance", UNSET)

        force_id_prefix = d.pop("force_id_prefix", UNSET)

        skip_per_edge_cycle_check = d.pop("skip_per_edge_cycle_check", UNSET)

        apply_batch_request = cls(
            actor=actor,
            items=items,
            provenance=provenance,
            force_id_prefix=force_id_prefix,
            skip_per_edge_cycle_check=skip_per_edge_cycle_check,
        )

        return apply_batch_request
