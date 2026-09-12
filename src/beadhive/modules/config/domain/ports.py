"""Inward-facing ports for configuration sources and round-trip documents."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from contextlib import AbstractContextManager
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

ConfigDocument = MutableMapping[str, Any]
EditResult = TypeVar("EditResult")


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
)
