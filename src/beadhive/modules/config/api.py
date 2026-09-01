"""Supported application API for configuration resolution and persistence."""

from .application.migrations import ConfigMigrationService, KeyMigration, migrate_document
from .application.resolution import (
    ConfigResolutionError,
    ResolutionDiagnostic,
    ResolutionInputs,
    ResolvedConfig,
    SourceLayer,
    ValueProvenance,
    resolve_config,
)
from .domain.ports import (
    ConfigDocumentEditPort,
    ConfigDocumentLoadPort,
    ConfigDocumentPort,
    ConfigDocumentSavePort,
    ConfigScope,
    EnvironmentSourcePort,
)

__all__ = (
    "ConfigDocumentEditPort",
    "ConfigDocumentLoadPort",
    "ConfigDocumentPort",
    "ConfigDocumentSavePort",
    "ConfigMigrationService",
    "ConfigResolutionError",
    "ConfigScope",
    "EnvironmentSourcePort",
    "KeyMigration",
    "ResolvedConfig",
    "ResolutionDiagnostic",
    "ResolutionInputs",
    "SourceLayer",
    "ValueProvenance",
    "migrate_document",
    "resolve_config",
)
