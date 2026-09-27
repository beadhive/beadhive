"""Typed application boundary for molecule planning operations."""

from __future__ import annotations

from ..contracts import (
    KickoffStore,
    MoleculeRepairer,
    MoleculeVerifier,
    SpecValidator,
)
from ..domain import (
    KickoffRequest,
    KickoffResult,
    PlanningError,
    RepairRequest,
    RepairResult,
    ValidationRequest,
    ValidationResult,
    VerificationRequest,
    VerificationResult,
)


class PlanningService:
    """Coordinate planning policy while infrastructure remains behind explicit ports.

    Molecule FILING is not this boundary's concern (bh-sy36q.2): it moved to
    ``beadhive_core.planning.PlanningCommands`` + ``beadhive.plan_filing``, which compile and
    submit the molecule through a single Beads BatchApply request instead of this callback-per-
    operation shape. This service still coordinates validate/approve/verify/repair.
    """

    def __init__(
        self,
        *,
        validator: SpecValidator,
        kickoff: KickoffStore,
        verifier: MoleculeVerifier,
        repairer: MoleculeRepairer,
    ) -> None:
        self._validator = validator
        self._kickoff = kickoff
        self._verifier = verifier
        self._repairer = repairer

    def validate(self, request: ValidationRequest) -> ValidationResult:
        return self._validator.validate(request)

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
