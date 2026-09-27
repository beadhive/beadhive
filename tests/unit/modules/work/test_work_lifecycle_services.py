"""Pure work lifecycle application tests over fake outbound ports."""

from __future__ import annotations

from beadhive.modules.work import (
    CheckRequest,
    CheckResult,
    MergeRequest,
    MergeResult,
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

    def schedule(self, request):
        self.requests.append(request)
        return ScheduleResult(request.epic, {"groups": (), "singletons": ()})


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
    execution = FakeExecution()
    evidence = FakeEvidence()
    notifier = FakeNotifier()
    return (
        WorkLifecycleService(
            beads=beads,
            execution=execution,
            evidence=evidence,
            identity=FakeIdentity(),
            notifier=notifier,
        ),
        beads,
        execution,
        evidence,
        notifier,
    )


def test_submission_resolves_actor_before_effects() -> None:
    service, _, execution, _, notifier = _service()

    service.submit(SubmissionRequest(bead="bh-1"))

    assert execution.requests[0].actor == "dev/submit"
    assert [event[:2] for event in notifier.events] == [("submit", "bh-1")]


def test_execution_and_review_paths_keep_typed_subjects() -> None:
    service, _, execution, evidence, notifier = _service()

    service.check(CheckRequest("bh-2"))
    service.submit(SubmissionRequest(group="bh-batch"))
    service.review(ReviewRequest("bh-2", run_validation=True, views=("diff",)))
    service.merge(MergeRequest(bead="bh-2", remove_worktree=True))

    assert execution.requests[1].subject == "bh-batch"
    assert evidence.requests == [ReviewRequest("bh-2", run_validation=True, views=("diff",))]
    assert [event[0] for event in notifier.events] == ["check", "submit", "review", "merge"]


def test_schedule_uses_bead_store_port() -> None:
    service, _, _, _, notifier = _service()

    plan = service.schedule(ScheduleRequest("bh-epic"))

    assert plan.plan["groups"] == ()
    assert [event[0] for event in notifier.events] == ["schedule"]


def test_service_rejects_port_identity_drift_before_notification() -> None:
    service, _, execution, _, notifier = _service()
    execution.submit = lambda request: SubmissionResult("other")

    try:
        service.submit(SubmissionRequest(bead="bh-1"))
    except ValueError as exc:
        assert "changed the requested bead identity" in str(exc)
    else:
        raise AssertionError("port identity drift was accepted")
    assert notifier.events == []


def test_selector_contracts_retain_empty_values_for_legacy_command_diagnostics() -> None:
    assert SubmissionRequest().subject == ""
    assert SubmissionRequest(group="bh-batch", bead=object()).subject == "bh-batch"
    assert MergeRequest().subject == ""
