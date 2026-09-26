from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.statistics import Statistics


T = TypeVar("T", bound="StatsResponse")


@_attrs_define
class StatsResponse:
    """The same envelope `bd status --json` prints, minus one member: the CLI also carries `recent_activity`, which every
    shipped code path leaves absent, so it is not published here.

        Attributes:
            summary (Statistics): Workspace summary counts. Two of them are DEPENDENCY-AWARE and two are structurally always
                zero; both facts are stated on the properties themselves, because every number here is the same JSON type and
                nothing else on the wire distinguishes them.

                This is the struct `bd status --json` marshals under `summary`, pinned so the two surfaces are one compatibility
                domain.
            blocked_count_skipped (bool): True when the summary came back without the blocked-set scan, which is exactly
                `summary.blocked_issues == null`. It is DERIVED from the answer rather than echoed from the request:
                `skip_blocked` is a hint, and a backend with no cheaper path answers with the full numbers and this flag false.
    """

    summary: Statistics
    blocked_count_skipped: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        summary = self.summary.to_dict()

        blocked_count_skipped = self.blocked_count_skipped

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "summary": summary,
                "blocked_count_skipped": blocked_count_skipped,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.statistics import Statistics  # noqa: PLC0415

        d = dict(src_dict)
        summary = Statistics.from_dict(d.pop("summary"))

        blocked_count_skipped = d.pop("blocked_count_skipped")

        stats_response = cls(
            summary=summary,
            blocked_count_skipped=blocked_count_skipped,
        )

        stats_response.additional_properties = d
        return stats_response

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
