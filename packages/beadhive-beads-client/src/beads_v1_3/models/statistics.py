from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="Statistics")


@_attrs_define
class Statistics:
    """Workspace summary counts. Two of them are DEPENDENCY-AWARE and two are structurally always zero; both facts are
    stated on the properties themselves, because every number here is the same JSON type and nothing else on the wire
    distinguishes them.

    This is the struct `bd status --json` marshals under `summary`, pinned so the two surfaces are one compatibility
    domain.

        Attributes:
            total_issues (int): Every row in the workspace-wide answer's scan, INCLUDING closed and pinned ones. The status
                counts below do not sum to it: a row whose status is none of the four falls into no bucket, and `pinned_issues`
                counts a flag that overlaps all of them.
            open_issues (int):
            in_progress_issues (int):
            closed_issues (int):
            deferred_issues (int):
            blocked_issues (int | None): Rows the dependency graph blocks — the transitive is_blocked flag, NOT the count of
                rows whose status is `blocked`. An open row with an unfinished blocker is counted here and its status is still
                `open`.

                NULL when `skip_blocked` was honored, always together with `ready_issues`. The two are nil together or populated
                together; there is no state in which one is knowable and the other is not.

                When `assignee` is set this is a different number: the count of that actor's rows whose STATUS is `blocked`, and
                never null.
            ready_issues (int | None): ARITHMETIC, not a query: `open_issues` minus `blocked_issues`, clamped at zero. It is
                NOT the cardinality of `GET /v0/beads/ready`, which applies type exclusions, the deferral window and a limit
                that none of this touches.

                NULL under the same conditions as `blocked_issues`.

                When `assignee` is set this is the real ready-work count for that actor, and never null.
            pinned_issues (int): Rows carrying the pinned flag, overlapping every status bucket. Always 0 when `assignee` is
                set: that answer tallies the five statuses and nothing else.
            epics_eligible_for_closure (int): ALWAYS 0. No implementation computes it, on any backend or either surface. It
                is documented rather than dropped because this schema is pinned to the struct both surfaces marshal, and a
                caller reading a 0 here is reading an absent computation rather than an answer.
            average_lead_time_hours (float): ALWAYS 0, for the reason above.
    """

    total_issues: int
    open_issues: int
    in_progress_issues: int
    closed_issues: int
    deferred_issues: int
    blocked_issues: int | None
    ready_issues: int | None
    pinned_issues: int
    epics_eligible_for_closure: int
    average_lead_time_hours: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total_issues = self.total_issues

        open_issues = self.open_issues

        in_progress_issues = self.in_progress_issues

        closed_issues = self.closed_issues

        deferred_issues = self.deferred_issues

        blocked_issues: int | None
        blocked_issues = self.blocked_issues

        ready_issues: int | None
        ready_issues = self.ready_issues

        pinned_issues = self.pinned_issues

        epics_eligible_for_closure = self.epics_eligible_for_closure

        average_lead_time_hours = self.average_lead_time_hours

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "total_issues": total_issues,
                "open_issues": open_issues,
                "in_progress_issues": in_progress_issues,
                "closed_issues": closed_issues,
                "deferred_issues": deferred_issues,
                "blocked_issues": blocked_issues,
                "ready_issues": ready_issues,
                "pinned_issues": pinned_issues,
                "epics_eligible_for_closure": epics_eligible_for_closure,
                "average_lead_time_hours": average_lead_time_hours,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        total_issues = d.pop("total_issues")

        open_issues = d.pop("open_issues")

        in_progress_issues = d.pop("in_progress_issues")

        closed_issues = d.pop("closed_issues")

        deferred_issues = d.pop("deferred_issues")

        def _parse_blocked_issues(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        blocked_issues = _parse_blocked_issues(d.pop("blocked_issues"))

        def _parse_ready_issues(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        ready_issues = _parse_ready_issues(d.pop("ready_issues"))

        pinned_issues = d.pop("pinned_issues")

        epics_eligible_for_closure = d.pop("epics_eligible_for_closure")

        average_lead_time_hours = d.pop("average_lead_time_hours")

        statistics = cls(
            total_issues=total_issues,
            open_issues=open_issues,
            in_progress_issues=in_progress_issues,
            closed_issues=closed_issues,
            deferred_issues=deferred_issues,
            blocked_issues=blocked_issues,
            ready_issues=ready_issues,
            pinned_issues=pinned_issues,
            epics_eligible_for_closure=epics_eligible_for_closure,
            average_lead_time_hours=average_lead_time_hours,
        )

        statistics.additional_properties = d
        return statistics

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
