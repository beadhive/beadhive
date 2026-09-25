"""Pants build integration for Beadhive."""

from typing import Any

from .impact import PantsImpactBackend

__all__ = ["PantsBuildVerifier", "PantsImpactBackend"]


def __getattr__(name: str) -> Any:
    if name == "PantsBuildVerifier":
        from .verify import PantsBuildVerifier

        return PantsBuildVerifier
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
