"""Outbound ports for planning validation, persistence, kickoff, and repair."""

from __future__ import annotations

from typing import Protocol

from ..domain import (
    KickoffRequest,
    KickoffResult,
    RepairRequest,
    RepairResult,
    ValidationRequest,
    ValidationResult,
    VerificationRequest,
    VerificationResult,
)


class SpecValidator(Protocol):
    def validate(self, request: ValidationRequest) -> ValidationResult: ...


class KickoffStore(Protocol):
    def approve(self, request: KickoffRequest) -> KickoffResult: ...


class MoleculeVerifier(Protocol):
    def verify(self, request: VerificationRequest) -> VerificationResult: ...


class MoleculeRepairer(Protocol):
    def repair(self, request: RepairRequest) -> RepairResult: ...
