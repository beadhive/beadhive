"""Isolated contract tests for the configuration module."""

from __future__ import annotations

from beadhive.modules.config.contracts import (
    CONFIG_SECTION_COMPATIBILITY_ALIASES,
    SCHEMA_VERSION,
    BeadhiveConfig,
    field_default,
    iter_schema_fields,
    known_sections,
)


def test_canonical_models_own_version_defaults_metadata_and_aliases():
    assert SCHEMA_VERSION == 1
    assert BeadhiveConfig().schema_version == SCHEMA_VERSION
    assert field_default("work.max_commits") == 10
    by_path = {field.path: field for field in iter_schema_fields()}
    assert by_path["work.max_commits"].description

    config = BeadhiveConfig(work={"validate": {"submit": "just focused"}})
    assert config.work.validate_overrides == {"submit": "just focused"}


def test_top_level_section_inventory_is_derived_with_explicit_compatibility_aliases():
    sections = known_sections()

    assert set(BeadhiveConfig.model_fields) <= sections
    assert {"git_workspace", "orca", "hitch"} <= sections
    assert CONFIG_SECTION_COMPATIBILITY_ALIASES == frozenset({"beads"})
    assert "beads" in sections
    assert "definitely_not_config" not in sections
