"""Typed application boundary for molecule planning operations."""

from __future__ import annotations

from ..contracts import (
    KickoffStore,
    MoleculeFiler,
    MoleculeRepairer,
    MoleculeVerifier,
    SpecValidator,
)
from ..domain import (
    FilingRequest,
    FilingResult,
    KickoffRequest,
    KickoffResult,
    MoleculeGraph,
    PlanningError,
    RepairRequest,
    RepairResult,
    ValidationRequest,
    ValidationResult,
    VerificationRequest,
    VerificationResult,
)


class PlanningService:
    """Coordinate planning policy while infrastructure remains behind explicit ports."""

    def __init__(
        self,
        *,
        validator: SpecValidator,
        filer: MoleculeFiler,
        kickoff: KickoffStore,
        verifier: MoleculeVerifier,
        repairer: MoleculeRepairer,
    ) -> None:
        self._validator = validator
        self._filer = filer
        self._kickoff = kickoff
        self._verifier = verifier
        self._repairer = repairer

    def validate(self, request: ValidationRequest) -> ValidationResult:
        return self._validator.validate(request)

    def file(self, request: FilingRequest) -> FilingResult:
        validation = self.validate(ValidationRequest(request.spec, request.config))
        if not validation.valid:
            raise PlanningError("invalid molecule spec: " + "; ".join(validation.problems))
        graph = MoleculeGraph.from_issues(request.spec.get("issues") or ())
        result = self._filer.file(request, graph)
        if result.issue_count != len(graph.order) or result.root_count != len(graph.roots):
            raise PlanningError("filing result does not match the validated molecule graph")
        return result

    def approve(self, request: KickoffRequest) -> KickoffResult:
        result = self._kickoff.approve(request)
        if result.epic_id != request.epic_id:
            raise PlanningError("kickoff result changed the requested epic identity")
        return result

    def verify(self, request: VerificationRequest) -> VerificationResult:
        result = self._verifier.verify(request)
        if result.epic_id != request.epic_id:
            raise PlanningError("verification result changed the requested epic identity")
        return result

    def repair(self, request: RepairRequest) -> RepairResult:
        result = self._repairer.repair(request)
        if result.epic_id != request.epic_id:
            raise PlanningError("repair result changed the requested epic identity")
        return result
