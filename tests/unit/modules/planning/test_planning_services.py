"""Pure planning application tests over fake outbound ports.

Molecule filing is no longer part of this boundary (bh-sy36q.2): it moved to
``beadhive_core.planning.PlanningCommands`` (policy tests in
``packages/beadhive-core/tests/test_core_planning_policy.py``) composed by
``beadhive.plan_filing`` (shell tests in ``tests/test_plan_filing_shell.py``). This module keeps
validate/approve/verify/repair coverage only.
"""

from __future__ import annotations

from pathlib import Path

from beadhive.modules.planning import (
    KickoffRequest,
    KickoffResult,
    PlanningService,
    RepairRequest,
    RepairResult,
    ValidationRequest,
    ValidationResult,
    VerificationRequest,
    VerificationResult,
)


class FakeValidator:
    def __init__(self, problems=()):
        self.problems = tuple(problems)
        self.requests = []

    def validate(self, request):
        self.requests.append(request)
        return ValidationResult(self.problems)


class FakeKickoff:
    def approve(self, request):
        return KickoffResult(request.epic_id, 2)


class FakeVerifier:
    def verify(self, request):
        return VerificationResult(request.epic_id, ())


class FakeRepairer:
    def repair(self, request):
        return RepairResult(request.epic_id, ("created swarm",), ())


def _service(problems=()):
    validator = FakeValidator(problems)
    return (
        PlanningService(
            validator=validator,
            kickoff=FakeKickoff(),
            verifier=FakeVerifier(),
            repairer=FakeRepairer(),
        ),
        validator,
    )


def test_validate_records_the_request_and_returns_the_configured_problems() -> None:
    service, validator = _service(("missing acceptance",))
    spec = {"epic": {"title": "Capability"}, "issues": []}

    result = service.validate(ValidationRequest(spec, {}))

    assert validator.requests == [ValidationRequest(spec, {})]
    assert result.problems == ("missing acceptance",)
    assert not result.valid


def test_kickoff_verification_and_repair_keep_epic_identity() -> None:
    service, _ = _service()

    assert service.approve(KickoffRequest("bh-1", Path("/hive"), "planner", {})).resolved_gates == 2
    assert service.verify(VerificationRequest("bh-1", Path("/hive"), {})).valid
    assert service.repair(RepairRequest("bh-1", Path("/hive"), "planner", {})).fixes == (
        "created swarm",
    )
