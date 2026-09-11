"""Contract evidence for the concrete work capability callback adapters."""

from __future__ import annotations

from beadhive import work_services
from beadhive.modules.work import (
    AbandonRequest,
    ApprovalRequest,
    AssignmentRequest,
    BounceRequest,
    CheckRequest,
    ClaimRequest,
    MergeRequest,
    ResumeRequest,
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
        assign=capture,
        claim=capture,
        schedule=capture,
        check=capture,
        submit=capture,
        review=capture,
        approve=capture,
        bounce=capture,
        merge=capture,
        resume=capture,
        abandon=capture,
    )

    results = (
        service.assign(AssignmentRequest("bh-1", "dev/a")),
        service.claim(ClaimRequest(bead="bh-1")),
        service.schedule(ScheduleRequest("bh-e")),
        service.check(CheckRequest("bh-1")),
        service.submit(SubmissionRequest(bead="bh-1")),
        service.review(ReviewRequest("bh-1")),
        service.approve(ApprovalRequest("bh-1")),
        service.bounce(BounceRequest("bh-1", "fix")),
        service.merge(MergeRequest(bead="bh-1")),
        service.resume(ResumeRequest("bh-1")),
        service.abandon(AbandonRequest("bh-1")),
    )

    assert len(calls) == 11
    assert results[2].plan["max_depth"] == 1
    assert all(getattr(result, "value", "legacy-result") == "legacy-result" for result in results)


def test_adapter_identity_and_notification_ports_remain_live_per_composition() -> None:
    events = []
    service = work_services.work_lifecycle_service(
        assign=lambda request: request.actor,
        resolve_identity=lambda actor, bead, hive, action: actor or f"dev/{bead}/{action}",
        notify=lambda action, subject, result: events.append((action, subject, result.value)),
    )

    result = service.assign(AssignmentRequest("bh-2", "dev/b"))

    assert result.value == "dev/bh-2/assign"
    assert events == [("assign", "bh-2", "dev/bh-2/assign")]
