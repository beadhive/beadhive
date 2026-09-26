from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ReadyCount")


@_attrs_define
class ReadyCount:
    """The size of a ready set. It carries no items, no `has_more` and no cursor: this is a number about a set, and the
    operation that returns rows is `GET /v0/beads/ready`.

    It is NOT `x-go-type`-pinned, unlike the seven schemas above, and that is a decision rather than an omission: those
    seven are pinned because a canonical Go struct's JSON encoding IS the contract and a second wire struct would let
    the CLI's `--json` drift from these bodies. There is no canonical struct here — the CLI publishes this number inside
    its own stdout envelope's `pagination` member, which is not a wire type — so pinning would weld this body to a CLI
    presentation type instead of preventing a drift.

        Attributes:
            total (int): How many items `GET /v0/beads/ready` would return for these filters with `limit=0`. Never negative;
                0 when nothing is ready, which is a 200 rather than a 404 — a question about a set has an answer even when the
                set is empty.
    """

    total: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "total": total,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        total = d.pop("total")

        ready_count = cls(
            total=total,
        )

        ready_count.additional_properties = d
        return ready_count

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
