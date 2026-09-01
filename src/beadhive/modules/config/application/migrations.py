"""Versioned, port-driven configuration migrations."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..contracts import SCHEMA_VERSION
from ..domain.ports import ConfigDocumentLoadPort, ConfigDocumentSavePort, ConfigScope
from .resolution import SourceLayer, ensure_supported_schema_version


@dataclass(frozen=True, slots=True)
class KeyMigration:
    section: str
    old_key: str
    new_key: str | None
    introduced_in: int

    @property
    def description(self) -> str:
        destination = f"{self.section}.{self.new_key}" if self.new_key else "(removed)"
        return f"{self.section}.{self.old_key} -> {destination}"


HIVE_KEY_MIGRATIONS = (
    KeyMigration("otel", "rig", "hive", SCHEMA_VERSION),
    KeyMigration("git_workspace", "rig_match", "hive_match", SCHEMA_VERSION),
    KeyMigration("git_workspace", "enabled", None, SCHEMA_VERSION),
)


class MigrationStorePort(ConfigDocumentLoadPort, ConfigDocumentSavePort, Protocol):
    """The exact persistence surface migrations require."""


def migrate_document(
    document: Mapping[str, Any], migrations: tuple[KeyMigration, ...] = HIVE_KEY_MIGRATIONS
) -> tuple[MutableMapping[str, Any], tuple[str, ...]]:
    """Return a migrated copy and its stable change descriptions."""

    ensure_supported_schema_version(document, SourceLayer.HOST)
    migrated_document = copy.deepcopy(document)
    if not isinstance(migrated_document, MutableMapping):
        migrated_document = dict(migrated_document)
    applied: list[str] = []
    for migration in migrations:
        section = migrated_document.get(migration.section)
        if not isinstance(section, MutableMapping) or migration.old_key not in section:
            continue
        if migration.new_key is not None and migration.new_key not in section:
            section[migration.new_key] = section[migration.old_key]
        del section[migration.old_key]
        applied.append(migration.description)
    return migrated_document, tuple(applied)


class ConfigMigrationService:
    """Apply migrations through declared document ports."""

    def __init__(
        self,
        store: MigrationStorePort,
        report: Callable[[tuple[str, ...]], None],
    ) -> None:
        self._store = store
        self._report = report

    def migrate_host(self) -> tuple[str, ...]:
        try:
            document = self._store.load_document(ConfigScope.HOST)
        except FileNotFoundError:
            return ()
        migrated, applied = migrate_document(document)
        if not applied:
            return ()
        self._store.save_document(ConfigScope.HOST, migrated)
        self._report(applied)
        return applied


__all__ = (
    "ConfigMigrationService",
    "HIVE_KEY_MIGRATIONS",
    "KeyMigration",
    "migrate_document",
)
