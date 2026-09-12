"""Pure work lifecycle application tests over fake outbound ports."""

from __future__ import annotations

from beadhive.modules.work import (
    AbandonRequest,
    AbandonResult,
    ApprovalRequest,
    ApprovalResult,
    AssignmentRequest,
    AssignmentResult,
    BounceRequest,
    CheckRequest,
    CheckResult,
    ClaimRequest,
    ClaimResult,
    MergeRequest,
    MergeResult,
    ResumeRequest,
    ResumeResult,
    ReviewRequest,
    ReviewResult,
    ScheduleRequest,
    ScheduleResult,
    SubmissionRequest,
    SubmissionResult,
    WorkLifecycleService,
)


class FakeIdentity:
    def resolve(self, actor, *, bead, hive, action):
        return actor or f"dev/{action}"


class FakeNotifier:
    def __init__(self):
        self.events = []

    def completed(self, action, subject, result):
        self.events.append((action, subject, result))


class FakeBeads:
    def __init__(self):
        self.requests = []

    def assign(self, request):
        self.requests.append(request)
        return AssignmentResult(request.bead)

    def schedule(self, request):
        self.requests.append(request)
        return ScheduleResult(request.epic, {"groups": (), "singletons": ()})

    def approve(self, request):
        self.requests.append(request)
        return ApprovalResult(request.bead)

    def bounce(self, request):
        self.requests.append(request)
        return ApprovalResult(request.bead)

    def abandon(self, request):
        self.requests.append(request)
        return AbandonResult(request.bead)


class FakeWorktrees:
    def __init__(self):
        self.requests = []

    def claim(self, request):
        self.requests.append(request)
        return ClaimResult(request.subject)

    def resume(self, request):
        self.requests.append(request)
        return ResumeResult(request.bead)


class FakeExecution:
    def __init__(self):
        self.requests = []

    def check(self, request):
        self.requests.append(request)
        return CheckResult(request.bead)

    def submit(self, request):
        self.requests.append(request)
        return SubmissionResult(request.subject)

    def merge(self, request):
        self.requests.append(request)
        return MergeResult(request.subject)


class FakeEvidence:
    def __init__(self):
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        return ReviewResult(request.bead)


def _service():
    beads = FakeBeads()
    worktrees = FakeWorktrees()
    execution = FakeExecution()
    evidence = FakeEvidence()
    notifier = FakeNotifier()
    return (
        WorkLifecycleService(
            beads=beads,
            worktrees=worktrees,
            execution=execution,
            evidence=evidence,
            identity=FakeIdentity(),
            notifier=notifier,
        ),
        beads,
        worktrees,
        execution,
        evidence,
        notifier,
    )


def test_assignment_claim_and_resume_resolve_actor_before_effects() -> None:
    service, beads, worktrees, _, _, notifier = _service()

    service.assign(AssignmentRequest("bh-1", "dev/alice"))
    service.claim(ClaimRequest(bead="bh-1"))
    service.resume(ResumeRequest("bh-1"))

    assert beads.requests[0].actor == "dev/assign"
    assert worktrees.requests[0].actor == "dev/claim"
    assert worktrees.requests[1].actor == "dev/resume"
    assert [event[:2] for event in notifier.events] == [
        ("assign", "bh-1"),
        ("claim", "bh-1"),
        ("resume", "bh-1"),
    ]


def test_execution_and_review_paths_keep_typed_subjects() -> None:
    service, _, _, execution, evidence, notifier = _service()

    service.check(CheckRequest("bh-2"))
    service.submit(SubmissionRequest(group="bh-batch"))
    service.review(ReviewRequest("bh-2", run_validation=True, views=("diff",)))
    service.merge(MergeRequest(bead="bh-2", remove_worktree=True))

    assert execution.requests[1].subject == "bh-batch"
    assert evidence.requests == [ReviewRequest("bh-2", run_validation=True, views=("diff",))]
    assert [event[0] for event in notifier.events] == ["check", "submit", "review", "merge"]


def test_schedule_review_decisions_and_abandon_use_bead_store_port() -> None:
    service, beads, _, _, _, notifier = _service()

    plan = service.schedule(ScheduleRequest("bh-epic"))
    service.approve(ApprovalRequest("bh-3"))
    service.bounce(BounceRequest("bh-4", "fix the gate"))
    service.abandon(AbandonRequest("bh-5", remove_worktree=True))

    assert plan.plan["groups"] == ()
    assert beads.requests[1].actor == "dev/approve"
    assert beads.requests[2].actor == "dev/bounce"
    assert [event[0] for event in notifier.events] == [
        "schedule",
        "approve",
        "bounce",
        "abandon",
    ]


def test_service_rejects_port_identity_drift_before_notification() -> None:
    service, beads, _, _, _, notifier = _service()
    beads.assign = lambda request: AssignmentResult("other")

    try:
        service.assign(AssignmentRequest("bh-1", "dev/alice"))
    except ValueError as exc:
        assert "changed the requested bead identity" in str(exc)
    else:
        raise AssertionError("port identity drift was accepted")
    assert notifier.events == []


def test_selector_contracts_retain_empty_values_for_legacy_command_diagnostics() -> None:
    assert ClaimRequest().subject == ""
    assert ClaimRequest(collapse="bh-epic", group=object()).subject == "bh-epic"
    assert SubmissionRequest().subject == ""
    assert MergeRequest().subject == ""
