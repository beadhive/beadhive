"""Pure resolution, provenance, environment, and refusal policy."""

from __future__ import annotations

import copy

import pytest

from beadhive.modules.config.adapters.environment import MappingEnvironmentSource
from beadhive.modules.config.application.resolution import (
    ConfigResolutionError,
    ResolutionInputs,
    SourceLayer,
    ensure_supported_schema_version,
    resolve_config,
)
from beadhive.modules.config.contracts import BeadhiveConfig


def test_resolution_is_pure_and_applies_the_declared_precedence(monkeypatch):
    monkeypatch.setenv("BH_OTEL__PROTOCOL", "ambient-must-not-win")
    inputs = ResolutionInputs(
        fleet={"otel": {"protocol": "grpc"}},
        host={"otel": {"protocol": "http/protobuf"}},
        hive={"otel": {"protocol": "grpc"}},
        environment={"otel": {"protocol": "http/protobuf"}},
        runtime={"otel": {"protocol": "grpc"}},
        runtime_allowed_paths=frozenset({"otel.protocol"}),
    )
    original = copy.deepcopy(inputs)

    result = resolve_config(inputs)

    assert result.settings.otel.protocol == "grpc"
    assert type(result.settings) is BeadhiveConfig
    assert result.provenance["otel.protocol"].layer == SourceLayer.RUNTIME
    assert result.provenance["otel.protocol"].source_key == "runtime.otel.protocol"
    assert inputs == original


def test_defaults_are_resolved_without_ambient_environment(monkeypatch):
    monkeypatch.setenv("BH_DOLT__BACKEND", "podman")

    result = resolve_config(ResolutionInputs())

    assert result.settings.dolt.backend == "docker"
    assert result.provenance["dolt.backend"].layer == SourceLayer.DEFAULT


def test_host_cannot_override_fleet_policy_even_with_the_same_value():
    inputs = ResolutionInputs(
        fleet={"work": {"validate_cmd": "just check"}},
        host={"work": {"validate_cmd": "just check"}},
    )

    with pytest.raises(ConfigResolutionError) as captured:
        resolve_config(inputs)

    diagnostic = captured.value.diagnostics[0]
    assert diagnostic.code == "forbidden_host_override"
    assert diagnostic.path == "work.validate_cmd"


@pytest.mark.parametrize(
    ("runtime", "allowed", "code"),
    [
        ({"work": {"validate_cmd": "secret"}}, {"work.validate_cmd"}, "forbidden_runtime_override"),
        ({"otel": {"protocol": "secret"}}, set(), "undeclared_runtime_override"),
        ({"schema_version": 1}, {"schema_version"}, "forbidden_runtime_override"),
    ],
)
def test_runtime_overrides_are_declared_and_cannot_bypass_policy(runtime, allowed, code):
    with pytest.raises(ConfigResolutionError) as captured:
        resolve_config(ResolutionInputs(runtime=runtime, runtime_allowed_paths=frozenset(allowed)))

    assert captured.value.diagnostics[0].code == code


@pytest.mark.parametrize("layer", ["fleet", "host", "hive", "environment", "runtime"])
@pytest.mark.parametrize(
    ("raw_version", "code"),
    [
        (2, "future_schema_version"),
        ("2", "invalid_schema_version"),
        (2.0, "invalid_schema_version"),
        (True, "invalid_schema_version"),
        (False, "invalid_schema_version"),
        (None, "invalid_schema_version"),
        ([], "invalid_schema_version"),
        ({}, "invalid_schema_version"),
        ("top-secret-version-canary", "invalid_schema_version"),
    ],
)
def test_every_typed_source_refuses_future_or_non_integer_raw_versions(layer, raw_version, code):
    with pytest.raises(ConfigResolutionError) as captured:
        resolve_config(ResolutionInputs(**{layer: {"schema_version": raw_version}}))

    assert captured.value.diagnostics[0].code == code
    assert captured.value.diagnostics[0].layer == SourceLayer(layer)
    if code == "invalid_schema_version":
        if raw_version == "top-secret-version-canary":
            assert raw_version not in str(captured.value)
            assert raw_version not in repr(captured.value.diagnostics)


@pytest.mark.parametrize("layer", ["fleet", "host", "hive", "environment", "runtime"])
def test_raw_guard_accepts_the_honest_current_integer_version(layer):
    ensure_supported_schema_version({"schema_version": 1}, SourceLayer(layer))


def test_validation_diagnostics_do_not_retain_or_render_source_values():
    canary = "top-secret-canary"

    with pytest.raises(ConfigResolutionError) as captured:
        resolve_config(ResolutionInputs(environment={"otel": {"enabled": canary}}))

    assert canary not in str(captured.value)
    assert canary not in repr(captured.value.diagnostics)
    assert captured.value.diagnostics[0].path == "otel.enabled"


def test_environment_adapter_is_explicit_allowlisted_and_transport_safe():
    overlay = MappingEnvironmentSource(
        {
            "BH_DOLT__BACKEND": '"podman"',
            "BH_WORK__MAX_COMMITS": "7",
            "BH_WORKTREES": "/secret/path",
            "BH_SCHEMA_VERSION": "99",
            "BH_UNKNOWN__TOKEN": "canary",
            "OTHER": "ignored",
        }
    ).load_overlay()

    assert overlay == {"dolt": {"backend": "podman"}, "work": {"max_commits": 7}}
