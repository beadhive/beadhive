"""Contract evidence for the concrete work capability callback adapters."""

from __future__ import annotations

from beadhive import work_services
from beadhive.modules.work import (
    CheckRequest,
    MergeRequest,
    ReviewRequest,
    ScheduleRequest,
    SubmissionRequest,
)


def test_callback_adapters_forward_every_typed_lifecycle_request() -> None:
    calls = []

    def capture(request):
        calls.append(request)
        if isinstance(request, ScheduleRequest):
            return {"groups": [], "singletons": [], "coordinators": [], "max_depth": 1}
        return "legacy-result"

    service = work_services.work_lifecycle_service(
        schedule=capture,
        check=capture,
        submit=capture,
        review=capture,
        merge=capture,
    )

    results = (
        service.schedule(ScheduleRequest("bh-e")),
        service.check(CheckRequest("bh-1")),
        service.submit(SubmissionRequest(bead="bh-1")),
        service.review(ReviewRequest("bh-1")),
        service.merge(MergeRequest(bead="bh-1")),
    )

    assert len(calls) == 5
    assert results[0].plan["max_depth"] == 1
    assert all(getattr(result, "value", "legacy-result") == "legacy-result" for result in results)


def test_adapter_identity_and_notification_ports_remain_live_per_composition() -> None:
    events = []
    service = work_services.work_lifecycle_service(
        submit=lambda request: request.actor,
        resolve_identity=lambda actor, bead, hive, action: actor or f"dev/{bead}/{action}",
        notify=lambda action, subject, result: events.append((action, subject, result.value)),
    )

    result = service.submit(SubmissionRequest(bead="bh-2"))

    assert result.value == "dev/bh-2/submit"
    assert events == [("submit", "bh-2", "dev/bh-2/submit")]
