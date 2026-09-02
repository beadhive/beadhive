"""Outbound ports for planning validation, persistence, kickoff, and repair."""

from __future__ import annotations

from typing import Protocol

from ..domain import (
    FilingRequest,
    FilingResult,
    KickoffRequest,
    KickoffResult,
    MoleculeGraph,
    RepairRequest,
    RepairResult,
    ValidationRequest,
    ValidationResult,
    VerificationRequest,
    VerificationResult,
)


class SpecValidator(Protocol):
    def validate(self, request: ValidationRequest) -> ValidationResult: ...


class MoleculeFiler(Protocol):
    def file(self, request: FilingRequest, graph: MoleculeGraph) -> FilingResult: ...


class KickoffStore(Protocol):
    def approve(self, request: KickoffRequest) -> KickoffResult: ...


class MoleculeVerifier(Protocol):
    def verify(self, request: VerificationRequest) -> VerificationResult: ...


class MoleculeRepairer(Protocol):
    def repair(self, request: RepairRequest) -> RepairResult: ...
