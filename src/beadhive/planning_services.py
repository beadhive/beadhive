"""Live compatibility adapters for :mod:`beadhive.modules.planning`.

The capability is pure.  This composition layer binds its ports to the existing planning
facades on each call, preserving monkeypatch seams without caching mutable process state.
"""

from __future__ import annotations

from collections.abc import Callable

from .modules.planning import (
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


def _unbound(request):
    raise RuntimeError(f"planning adapter is not bound for {type(request).__name__}")


class CallbackSpecValidator:
    def __init__(self, callback: Callable = _unbound) -> None:
        self._callback = callback

    def validate(self, request: ValidationRequest) -> ValidationResult:
        return ValidationResult(tuple(self._callback(request)))


class CallbackKickoffStore:
    def __init__(self, callback: Callable = _unbound) -> None:
        self._callback = callback

    def approve(self, request: KickoffRequest) -> KickoffResult:
        return self._callback(request)


class CallbackMoleculeVerifier:
    def __init__(self, callback: Callable = _unbound) -> None:
        self._callback = callback

    def verify(self, request: VerificationRequest) -> VerificationResult:
        return VerificationResult(request.epic_id, tuple(self._callback(request)))


class CallbackMoleculeRepairer:
    def __init__(self, callback: Callable = _unbound) -> None:
        self._callback = callback

    def repair(self, request: RepairRequest) -> RepairResult:
        value = self._callback(request)
        return RepairResult(request.epic_id, tuple(value.fixes), tuple(value.problems))


def planning_service(
    *,
    validate: Callable = _unbound,
    approve: Callable = _unbound,
    verify: Callable = _unbound,
    repair: Callable = _unbound,
) -> PlanningService:
    """Construct one uncached planning service from the currently live callbacks.

    Molecule filing is not a callback here (bh-sy36q.2): see
    :func:`beadhive.plan_filing.file`, which composes ``beadhive_core.planning.PlanningCommands``
    directly instead of routing through this validate/approve/verify/repair callback shape.
    """

    return PlanningService(
        validator=CallbackSpecValidator(validate),
        kickoff=CallbackKickoffStore(approve),
        verifier=CallbackMoleculeVerifier(verify),
        repairer=CallbackMoleculeRepairer(repair),
    )
