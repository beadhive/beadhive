from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="SweepSkips")


@_attrs_define
class SweepSkips:
    """The candidates a sweep declined to delete, bucketed by WHY. They are separate counters rather than one number
    because they mean different things: the first two are PROTECTIONS, and the last four are the sweep declining to
    trust its own input.

        Attributes:
            pinned (int): Candidates protected by the pinned flag. No request member overrides it — a caller who wants a
                pinned bead gone unpins it first.
            referenced (int): Candidates protected by `protect_referenced`. Always 0 when that member is false or absent, so
                a 0 read without having asked says nothing about whether beads are cited.
            not_closed (int): Candidates the tier query returned that the recheck found were not closed. A NON-ZERO VALUE
                HERE IS A DEFENSE FIRING, not a normal outcome: the query asked for exactly the beads this excludes.
            unknown_closed_at (int): Closed candidates carrying no close timestamp. See `not_closed`.
            closed_at_or_after_cutoff (int): Candidates whose close timestamp did not satisfy `closed_before`. See
                `not_closed`.
            unreadable (int): Rows the tier query returned as nothing at all. A defense of the same kind, on a shape rather
                than a value.
    """

    pinned: int
    referenced: int
    not_closed: int
    unknown_closed_at: int
    closed_at_or_after_cutoff: int
    unreadable: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        pinned = self.pinned

        referenced = self.referenced

        not_closed = self.not_closed

        unknown_closed_at = self.unknown_closed_at

        closed_at_or_after_cutoff = self.closed_at_or_after_cutoff

        unreadable = self.unreadable

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "pinned": pinned,
                "referenced": referenced,
                "not_closed": not_closed,
                "unknown_closed_at": unknown_closed_at,
                "closed_at_or_after_cutoff": closed_at_or_after_cutoff,
                "unreadable": unreadable,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        pinned = d.pop("pinned")

        referenced = d.pop("referenced")

        not_closed = d.pop("not_closed")

        unknown_closed_at = d.pop("unknown_closed_at")

        closed_at_or_after_cutoff = d.pop("closed_at_or_after_cutoff")

        unreadable = d.pop("unreadable")

        sweep_skips = cls(
            pinned=pinned,
            referenced=referenced,
            not_closed=not_closed,
            unknown_closed_at=unknown_closed_at,
            closed_at_or_after_cutoff=closed_at_or_after_cutoff,
            unreadable=unreadable,
        )

        sweep_skips.additional_properties = d
        return sweep_skips

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
