from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.sweep_skips import SweepSkips


T = TypeVar("T", bound="SweepResult")


@_attrs_define
class SweepResult:
    """What one sweep did. Every number describes the SAME snapshot, because the selection and the deletion ran in one
    transaction.

    It is NOT `x-go-type`-pinned, for the reason `ReadyCount` is not: there is no canonical Go struct whose JSON
    encoding is this contract. The CLI publishes these numbers under its own per-command keys (`purged_count`,
    `pruned_count`), which are a stdout presentation rather than a wire type, so pinning would weld this body to one of
    them.

        Attributes:
            dry_run (bool): Echoes the request, so a result carries whether its numbers describe beads that are gone or
                beads that would go.
            swept (int): How many beads were deleted, or under `dry_run` would be.
            dependencies (int): Dependency edge rows removed with them. Reported because a sweep's visible effect is much
                larger than its bead count.
            labels (int): Label rows removed with the swept beads.
            events (int): Event rows removed with the swept beads.
            skipped (SweepSkips): The candidates a sweep declined to delete, bucketed by WHY. They are separate counters
                rather than one number because they mean different things: the first two are PROTECTIONS, and the last four are
                the sweep declining to trust its own input.
            referenced_ids (list[str] | Unset): A BOUNDED SAMPLE of the ids `skipped.referenced` counts — at most 100, in
                the order the candidate query returned them. It is a sample, not the set: compare its length against 100 to tell
                a truncated one from a complete one. Absent when nothing was protected.
    """

    dry_run: bool
    swept: int
    dependencies: int
    labels: int
    events: int
    skipped: SweepSkips
    referenced_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        dry_run = self.dry_run

        swept = self.swept

        dependencies = self.dependencies

        labels = self.labels

        events = self.events

        skipped = self.skipped.to_dict()

        referenced_ids: list[str] | Unset = UNSET
        if not isinstance(self.referenced_ids, Unset):
            referenced_ids = self.referenced_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "dry_run": dry_run,
                "swept": swept,
                "dependencies": dependencies,
                "labels": labels,
                "events": events,
                "skipped": skipped,
            }
        )
        if referenced_ids is not UNSET:
            field_dict["referenced_ids"] = referenced_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.sweep_skips import SweepSkips  # noqa: PLC0415

        d = dict(src_dict)
        dry_run = d.pop("dry_run")

        swept = d.pop("swept")

        dependencies = d.pop("dependencies")

        labels = d.pop("labels")

        events = d.pop("events")

        skipped = SweepSkips.from_dict(d.pop("skipped"))

        referenced_ids = cast(list[str], d.pop("referenced_ids", UNSET))

        sweep_result = cls(
            dry_run=dry_run,
            swept=swept,
            dependencies=dependencies,
            labels=labels,
            events=events,
            skipped=skipped,
            referenced_ids=referenced_ids,
        )

        sweep_result.additional_properties = d
        return sweep_result

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
