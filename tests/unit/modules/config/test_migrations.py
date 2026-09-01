"""Pure and port-driven migration contracts."""

from __future__ import annotations

import copy

import pytest

from beadhive.modules.config.adapters.yaml_store import RoundTripYamlStore
from beadhive.modules.config.application.migrations import (
    ConfigMigrationService,
    migrate_document,
)
from beadhive.modules.config.application.resolution import ConfigResolutionError
from beadhive.modules.config.domain.ports import ConfigScope


class MemoryStore:
    def __init__(self, document=None):
        self.document = document
        self.saved = []

    def load_document(self, scope, *, missing_ok=False):
        assert scope == ConfigScope.HOST
        if self.document is None and not missing_ok:
            raise FileNotFoundError
        return copy.deepcopy(self.document or {})

    def save_document(self, scope, document):
        assert scope == ConfigScope.HOST
        self.saved.append(copy.deepcopy(document))


def test_migration_is_pure_versioned_and_preserves_an_existing_destination():
    source = {
        "schema_version": 1,
        "otel": {"rig": "old", "hive": "new"},
        "git_workspace": {"enabled": True, "rig_match": "legacy"},
    }
    original = copy.deepcopy(source)

    migrated, applied = migrate_document(source)

    assert source == original
    assert migrated == {
        "schema_version": 1,
        "otel": {"hive": "new"},
        "git_workspace": {"hive_match": "legacy"},
    }
    assert applied == (
        "otel.rig -> otel.hive",
        "git_workspace.rig_match -> git_workspace.hive_match",
        "git_workspace.enabled -> (removed)",
    )


def test_migration_service_uses_only_load_and_save_ports():
    store = MemoryStore({"otel": {"rig": "demo"}})
    reports = []

    applied = ConfigMigrationService(store, reports.append).migrate_host()

    assert applied == ("otel.rig -> otel.hive",)
    assert store.saved == [{"otel": {"hive": "demo"}}]
    assert reports == [applied]


@pytest.mark.parametrize(
    ("raw_version", "code"),
    [
        (2, "future_schema_version"),
        ("2", "invalid_schema_version"),
        (2.0, "invalid_schema_version"),
        (True, "invalid_schema_version"),
        (None, "invalid_schema_version"),
        ([], "invalid_schema_version"),
        ({}, "invalid_schema_version"),
    ],
)
def test_migration_refuses_future_or_invalid_raw_versions_without_writing(raw_version, code):
    canary = "migration-secret-canary"
    store = MemoryStore({"schema_version": raw_version, "otel": {"rig": canary}})

    with pytest.raises(ConfigResolutionError) as captured:
        ConfigMigrationService(store, lambda _applied: None).migrate_host()

    assert store.saved == []
    assert captured.value.diagnostics[0].code == code
    if code == "invalid_schema_version":
        assert str(raw_version) not in str(captured.value)
    assert canary not in str(captured.value)


def test_migration_accepts_the_honest_current_integer_version():
    store = MemoryStore({"schema_version": 1, "otel": {"rig": "demo"}})

    assert ConfigMigrationService(store, lambda _applied: None).migrate_host() == (
        "otel.rig -> otel.hive",
    )
    assert store.saved == [{"schema_version": 1, "otel": {"hive": "demo"}}]


def test_missing_host_document_is_an_unchanged_noop():
    store = MemoryStore()

    assert ConfigMigrationService(store, lambda _applied: None).migrate_host() == ()
    assert store.saved == []


def test_real_yaml_adapter_keeps_comments_and_order_during_migration(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("# operator\notel:\n  rig: legacy\n  enabled: true\n")
    store = RoundTripYamlStore(path_for=lambda _scope: path)

    ConfigMigrationService(store, lambda _applied: None).migrate_host()

    assert path.read_text() == "# operator\notel:\n  enabled: true\n  hive: legacy\n"
