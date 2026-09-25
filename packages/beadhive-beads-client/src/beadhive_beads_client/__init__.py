"""Beadhive's supported Beads v1.3 service boundary."""

from .session import (
    BeadsSession,
    CapabilityMissing,
    CliCompatibilityRequired,
    ExpectedContext,
    IncompatibleService,
    IndeterminateWrite,
    LocalEndpoint,
    RemoteEndpoint,
    ServiceProblem,
    SessionTimeout,
)

__all__ = [
    "BeadsSession",
    "CapabilityMissing",
    "CliCompatibilityRequired",
    "ExpectedContext",
    "IncompatibleService",
    "IndeterminateWrite",
    "LocalEndpoint",
    "RemoteEndpoint",
    "ServiceProblem",
    "SessionTimeout",
]
