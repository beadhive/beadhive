"""Determinism, drift, inventory, and independence proofs for config schema v1."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.metadata
import json
import socket
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from beadhive.modules.config.application.schema_artifacts import (
    CONFIG_SCHEMA_ARTIFACT_ID,
    CONFIG_SCHEMA_ARTIFACT_VERSION,
    generate_config_json_schema,
    generate_config_json_schema_bytes,
)

ROOT = Path(__file__).parents[4]
WIRE = ROOT / "docs/schemas/wire/v1.4.0"
ARTIFACT = ROOT / "src/beadhive/schemas/contracts/v1.0.0/artifacts/config-v1.schema.json"


def test_schema_generation_is_deterministic_and_matches_checked_artifact():
    first = generate_config_json_schema_bytes()
    second = generate_config_json_schema_bytes()
    document = json.loads(first)

    assert first == second == ARTIFACT.read_bytes()
    assert document["$id"] == CONFIG_SCHEMA_ARTIFACT_ID
    assert document["version"] == CONFIG_SCHEMA_ARTIFACT_VERSION == 1
    assert document["properties"]["schema_version"]["default"] == 1
    assert "validate" in document["$defs"]["WorkConfig"]["properties"]
    assert "validate_overrides" not in document["$defs"]["WorkConfig"]["properties"]
    Draft202012Validator.check_schema(document)


def test_official_schema_is_registered_and_validates_the_shipped_example():
    release = json.loads((WIRE / "release.json").read_text())
    registered = {row["id"]: row for row in release["artifacts"]}
    assert registered[CONFIG_SCHEMA_ARTIFACT_ID] == {
        "id": CONFIG_SCHEMA_ARTIFACT_ID,
        "contract_version": 1,
        "path": ARTIFACT.name,
    }

    config = YAML(typ="safe").load(
        (ROOT / "src/beadhive/templates/config.example.yaml").read_text()
    )
    Draft202012Validator(generate_config_json_schema()).validate(config)


def test_generator_import_and_execution_have_no_outer_runtime_effects(monkeypatch):
    forbidden_imports = (
        "beadhive.config",
        "beadhive.dolt",
        "beadhive.dolt_health",
        "beadhive.kernel.plugins",
        "beadhive.plugins",
    )

    class RefuseOuterLayer(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"schema generator imported outer runtime: {fullname}")
            return None

    def refuse(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"schema generator attempted {action}")

        return fail

    finder = RefuseOuterLayer()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(Path, "read_text", refuse("config read"))
    monkeypatch.setattr(Path, "write_text", refuse("config write"))
    monkeypatch.setattr(importlib.metadata, "entry_points", refuse("plugin discovery"))
    monkeypatch.setattr(socket, "create_connection", refuse("Dolt/network connection"))
    monkeypatch.setattr(subprocess, "run", refuse("process spawn"))
    try:
        assert generate_config_json_schema()["$id"] == CONFIG_SCHEMA_ARTIFACT_ID
    finally:
        sys.meta_path.remove(finder)
