"""Pure planning application tests over fake outbound ports."""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive.modules.planning import (
    FilingRequest,
    FilingResult,
    KickoffRequest,
    KickoffResult,
    PlanningError,
    PlanningService,
    RepairRequest,
    RepairResult,
    ValidationRequest,
    ValidationResult,
    VerificationRequest,
    VerificationResult,
)

SPEC = {
    "epic": {"title": "Capability"},
    "issues": [
        {"handle": "build", "title": "Build", "deps": ["design"]},
        {"handle": "design", "title": "Design"},
        {"handle": "docs", "title": "Document"},
    ],
}


class FakeValidator:
    def __init__(self, problems=()):
        self.problems = tuple(problems)
        self.requests = []

    def validate(self, request):
        self.requests.append(request)
        return ValidationResult(self.problems)


class FakeFiler:
    def __init__(self):
        self.calls = []

    def file(self, request, graph):
        self.calls.append((request, graph))
        return FilingResult("bh-new", len(graph.order), len(graph.roots), 1)


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
    filer = FakeFiler()
    return (
        PlanningService(
            validator=validator,
            filer=filer,
            kickoff=FakeKickoff(),
            verifier=FakeVerifier(),
            repairer=FakeRepairer(),
        ),
        validator,
        filer,
    )


def test_file_validates_then_passes_stable_dag_to_filing_port() -> None:
    service, validator, filer = _service()
    request = FilingRequest(SPEC, Path("/hive"), "planner", {})

    result = service.file(request)

    assert validator.requests == [ValidationRequest(SPEC, {})]
    assert filer.calls[0][1].order == ("design", "docs", "build")
    assert filer.calls[0][1].roots == ("design", "docs")
    assert result == FilingResult("bh-new", 3, 2, 1)


def test_invalid_spec_never_reaches_filing_port() -> None:
    service, _, filer = _service(("missing acceptance",))

    with pytest.raises(PlanningError, match="missing acceptance"):
        service.file(FilingRequest(SPEC, Path("/hive"), "planner", {}))

    assert filer.calls == []


def test_kickoff_verification_and_repair_keep_epic_identity() -> None:
    service, _, _ = _service()

    assert service.approve(KickoffRequest("bh-1", Path("/hive"), "planner", {})).resolved_gates == 2
    assert service.verify(VerificationRequest("bh-1", Path("/hive"), {})).valid
    assert service.repair(RepairRequest("bh-1", Path("/hive"), "planner", {})).fixes == (
        "created swarm",
    )
