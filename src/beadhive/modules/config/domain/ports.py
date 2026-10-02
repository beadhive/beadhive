"""Inward-facing ports for configuration sources and round-trip documents."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, MutableMapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

ConfigDocument = MutableMapping[str, Any]
EditResult = TypeVar("EditResult")


@dataclass(frozen=True)
class FleetConfigDocument:
    """One non-secret source document; tuple order preserves workspace precedence."""

    path: str
    content: str


@dataclass(frozen=True)
class RawFleetConfigRevision:
    """Privileged repair input, never a usable settings or admission snapshot."""

    expected_revision: str
    documents: tuple[FleetConfigDocument, ...]


@dataclass(frozen=True)
class FleetConfigSnapshot:
    """Committed raw documents plus provenance outside persisted settings keys."""

    backend_identity: str
    commit_revision: str
    generation: str
    fetched_at: float
    valid_until: float
    documents: tuple[FleetConfigDocument, ...]

    @property
    def beadyard_id(self) -> str | None:
        """Portable HQ instance binding; ``None`` is explicit legacy absence.

        It is derived from the one raw document, never a second stored copy or
        a generated default. Signed Git and committed Dolt adapters retain the
        document unchanged through export/import and backend switches.
        """
        from ....beadyard_identity import identity_in_documents

        return identity_in_documents(self.documents, required=False)

    def __post_init__(self):
        if any(
            not isinstance(value, str) or not value
            for value in (self.backend_identity, self.commit_revision, self.generation)
        ):
            raise ValueError("committed configuration provenance is required")
        if (
            any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (self.fetched_at, self.valid_until)
            )
            or self.valid_until <= self.fetched_at
        ):
            raise ValueError("committed configuration validity is inconsistent")
        if not isinstance(self.documents, tuple) or any(
            not isinstance(document, FleetConfigDocument) for document in self.documents
        ):
            raise ValueError("committed configuration documents must be immutable")


@runtime_checkable
class FleetConfigRevisionPort(Protocol):
    """Publish against the originally loaded revision, then verify committed readback."""

    def load_snapshot(self, *, revision: str | None = None) -> FleetConfigSnapshot: ...

    def publish_snapshot(
        self, documents: tuple[FleetConfigDocument, ...], *, expected_revision: str
    ) -> FleetConfigSnapshot: ...


class ConfigScope(StrEnum):
    """Persisted configuration document scopes."""

    FLEET = "fleet"
    HOST = "host"


@runtime_checkable
class ConfigDocumentLoadPort(Protocol):
    """Load one raw round-trip document without activating typed settings."""

    def load_document(self, scope: ConfigScope, *, missing_ok: bool = False) -> ConfigDocument: ...


@runtime_checkable
class ConfigDocumentSavePort(Protocol):
    """Atomically persist one raw round-trip document."""

    def save_document(self, scope: ConfigScope, document: Mapping[str, Any]) -> None: ...


@runtime_checkable
class ConfigDocumentEditPort(Protocol):
    """Serialize a complete read/modify/write transaction for one document."""

    def transaction(self, scope: ConfigScope) -> AbstractContextManager[None]: ...

    def edit_document(
        self,
        scope: ConfigScope,
        edit: Callable[[ConfigDocument], EditResult],
        *,
        missing_ok: bool = False,
    ) -> EditResult: ...


@runtime_checkable
class ConfigDocumentPort(
    ConfigDocumentLoadPort, ConfigDocumentSavePort, ConfigDocumentEditPort, Protocol
):
    """Complete raw-document capability implemented by persistence adapters."""


@runtime_checkable
class EnvironmentSourcePort(Protocol):
    """Project an environment source into an explicit nested overlay."""

    def load_overlay(self) -> Mapping[str, Any]: ...


__all__ = (
    "ConfigDocument",
    "ConfigDocumentEditPort",
    "ConfigDocumentLoadPort",
    "ConfigDocumentPort",
    "ConfigDocumentSavePort",
    "ConfigScope",
    "EnvironmentSourcePort",
    "FleetConfigDocument",
    "RawFleetConfigRevision",
    "FleetConfigSnapshot",
    "FleetConfigRevisionPort",
)
