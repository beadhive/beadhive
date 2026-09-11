"""Compatibility boundary for canonical config contracts and the legacy facade."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import config, config_schema
from beadhive.cli import app
from beadhive.modules.config import contracts
from beadhive.modules.config.application.schema_artifacts import (
    LEGACY_SCHEMA_ROWS_PROJECTION_VERSION,
    legacy_schema_rows,
)


def test_legacy_config_schema_imports_forward_to_canonical_contracts():
    assert config_schema.__all__ == contracts.__all__
    for name in contracts.__all__:
        assert getattr(config_schema, name) is getattr(contracts, name), name
    assert config_schema._DEFAULT_WORKTREE_INIT is contracts._DEFAULT_WORKTREE_INIT
    assert contracts.BeadhiveConfig.__module__ == "beadhive.modules.config.contracts"


def test_schema_derived_section_inventory_covers_fixed_regressions_and_alias():
    assert config.KNOWN_SECTIONS == contracts.known_sections()
    assert {"git_workspace", "orca", "hitch", "beads"} <= config.KNOWN_SECTIONS
    for section in ("git_workspace", "orca", "hitch", "beads"):
        problems = config._validate([section, "sentinel"], "value")
        assert not [p for p in problems if "unknown config section" in p["message"]]


def test_unknown_section_still_warns_and_writes():
    target = {}
    result = config.set_value("future_extension.enabled", "true", cfg=target)

    assert result["ok"] is True
    assert target == {"future_extension": {"enabled": True}}
    assert any(
        problem["level"] == "warning" and "unknown config section" in problem["message"]
        for problem in result["problems"]
    )


def test_cli_json_retains_the_explicit_v1_legacy_row_projection():
    assert LEGACY_SCHEMA_ROWS_PROJECTION_VERSION == 1
    result = CliRunner().invoke(app, ["config", "schema", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == legacy_schema_rows()
