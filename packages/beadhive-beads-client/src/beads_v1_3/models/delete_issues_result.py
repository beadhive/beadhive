from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeleteIssuesResult")


@_attrs_define
class DeleteIssuesResult:
    """What one delete did. Every number describes the SAME snapshot, because the guard, the deletion and the reference
    rewrite ran in one transaction.

    It is NOT `x-go-type`-pinned, for the reason `SweepResult` is not: there is no canonical Go struct whose JSON
    encoding is this contract. `bd delete --json` publishes these numbers under its own per-shape keys (`deleted_count`,
    `dependencies_removed`, and a scalar `deleted` on the single-id form), which are a stdout presentation rather than a
    wire type, so pinning would weld this body to one of them.

        Attributes:
            dry_run (bool): Echoes the request, so a result carries whether its numbers describe beads that are gone or
                beads that would go.
            deleted (int): How many beads were deleted, or under `dry_run` would be. Under `cascade` this counts the whole
                closure, so it is normally larger than `ids` and it — not the request length — is the number to show.
            dependencies (int): Dependency edge rows removed with them, in either direction. Reported because a delete's
                visible effect is much larger than its bead count.
            labels (int): Label rows removed with the deleted beads.
            events (int): Event rows removed with the deleted beads.
            references_updated (int): How many SURVIVING beads had their text rewritten — beads, not occurrences. Always 0
                under `dry_run`, because a preview rewrites nothing.
            orphaned (list[str] | Unset): The surviving beads that depended on something this request deleted, in ascending
                id order. Present exactly when the request carried `force` without `cascade`, which is the only mode in which
                orphaning is possible; absent otherwise.

                DIRECT dependents only. A bead two edges away lost no edge.
    """

    dry_run: bool
    deleted: int
    dependencies: int
    labels: int
    events: int
    references_updated: int
    orphaned: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        dry_run = self.dry_run

        deleted = self.deleted

        dependencies = self.dependencies

        labels = self.labels

        events = self.events

        references_updated = self.references_updated

        orphaned: list[str] | Unset = UNSET
        if not isinstance(self.orphaned, Unset):
            orphaned = self.orphaned

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "dry_run": dry_run,
                "deleted": deleted,
                "dependencies": dependencies,
                "labels": labels,
                "events": events,
                "references_updated": references_updated,
            }
        )
        if orphaned is not UNSET:
            field_dict["orphaned"] = orphaned

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        dry_run = d.pop("dry_run")

        deleted = d.pop("deleted")

        dependencies = d.pop("dependencies")

        labels = d.pop("labels")

        events = d.pop("events")

        references_updated = d.pop("references_updated")

        orphaned = cast(list[str], d.pop("orphaned", UNSET))

        delete_issues_result = cls(
            dry_run=dry_run,
            deleted=deleted,
            dependencies=dependencies,
            labels=labels,
            events=events,
            references_updated=references_updated,
            orphaned=orphaned,
        )

        delete_issues_result.additional_properties = d
        return delete_issues_result

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
