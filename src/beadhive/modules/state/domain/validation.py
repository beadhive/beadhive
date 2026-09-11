"""Typed durable-validation facts without storage or execution dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    """Immutable view of one canonical validation run manifest.

    ``payload`` retains the complete versioned manifest so introducing the type does not narrow
    the existing schema or discard migration/provenance fields.
    """

    run_id: str
    lifecycle: str
    verdict: str
    tree: str
    command_hash: str
    payload: Mapping[str, object]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ValidationRecord:
        payload = MappingProxyType(dict(value))
        return cls(
            run_id=str(value.get("run_id") or ""),
            lifecycle=str(value.get("lifecycle") or ""),
            verdict=str(value.get("verdict") or "none"),
            tree=str(value.get("tree") or ""),
            command_hash=str(value.get("command_hash") or ""),
            payload=payload,
        )

    def to_mapping(self) -> dict[str, object]:
        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class ValidationQuery:
    tree: str
    command_hash: str

    def __post_init__(self) -> None:
        if not self.tree or not self.command_hash:
            raise ValueError("validation query requires tree and command_hash")
