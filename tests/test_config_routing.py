"""Typed ``work.routing`` configuration and its reader-facing normalization contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from beadhive import config
from beadhive.complexity import ComplexityTier
from beadhive.config_partition import FLEET, partition_of
from beadhive.config_schema import (
    BeadhiveConfig,
    ManagedRepoEntry,
    RoutingTierConfig,
    iter_schema_fields,
)


def test_routing_defaults_are_loose_and_empty():
    routing = BeadhiveConfig().work.routing
    assert routing.policy == "loose"
    assert routing.tiers == []


def test_omitted_bounds_normalize_to_the_full_complexity_interval():
    tier = RoutingTierConfig(model="new-provider/model-release-2030")
    assert tier.floor is ComplexityTier.SIMPLE
    assert tier.ceiling is ComplexityTier.REASONING
    assert tier.endpoint is None
    assert tier.model_dump(mode="json") == {
        "model": "new-provider/model-release-2030",
        "floor": "SIMPLE",
        "ceiling": "REASONING",
        "endpoint": None,
    }


def test_explicit_bounds_use_the_merged_complexity_contract():
    tier = RoutingTierConfig(model="anthropic/claude-opus-4-1", floor="MEDIUM", ceiling="REASONING")
    assert tier.floor is ComplexityTier.MEDIUM
    assert tier.ceiling is ComplexityTier.REASONING


@pytest.mark.parametrize("field", ["floor", "ceiling"])
@pytest.mark.parametrize("value", ["simple", "Medium", "HARD", ""])
def test_invalid_complexity_spellings_fail_actionably(field, value):
    with pytest.raises(ValidationError, match=r"SIMPLE\|MEDIUM\|COMPLEX\|REASONING"):
        RoutingTierConfig(model="openai/gpt-5", **{field: value})


def test_floor_must_not_exceed_ceiling():
    with pytest.raises(ValidationError, match="floor REASONING must not exceed ceiling COMPLEX"):
        RoutingTierConfig(model="openai/gpt-5", floor="REASONING", ceiling="COMPLEX")


@pytest.mark.parametrize("model", ["", "gpt-5", "/gpt-5", "openai/", "open ai/gpt-5"])
def test_model_requires_non_empty_provider_model_form(model):
    with pytest.raises(ValidationError, match="provider/model-name"):
        RoutingTierConfig(model=model)


def test_provider_and_model_catalogues_remain_open():
    tier = RoutingTierConfig(model="provider-created-tomorrow/model-99-preview")
    assert tier.model == "provider-created-tomorrow/model-99-preview"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://gateway.example/v1",
        "http://localhost:8080/v1",
        "primary-gateway",
        "profile:primary-gateway",
    ],
)
def test_endpoint_accepts_supported_urls_and_profile_references(endpoint):
    assert RoutingTierConfig(model="openai/gpt-5", endpoint=endpoint).endpoint == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        " gateway ",
        "gateway profile",
        "ftp://gateway.example",
        "https://",
        "https://gateway example/v1",
        "https://gateway.example:not-a-port",
    ],
)
def test_malformed_endpoint_fails_actionably(endpoint):
    with pytest.raises(ValidationError, match="endpoint"):
        RoutingTierConfig(model="openai/gpt-5", endpoint=endpoint)


def test_endpoint_rejects_embedded_authentication():
    with pytest.raises(ValidationError, match="must not embed authentication"):
        RoutingTierConfig(model="openai/gpt-5", endpoint="https://token@gateway.example/v1")


@pytest.mark.parametrize("compatibility_key", ["access", "provider", "launch_model"])
def test_compatibility_fields_are_not_introduced(compatibility_key):
    with pytest.raises(ValidationError, match=compatibility_key):
        RoutingTierConfig(model="openai/gpt-5", **{compatibility_key: "legacy"})


def test_endpoint_authentication_and_tls_fields_stay_outside_tier_entry():
    with pytest.raises(ValidationError):
        RoutingTierConfig(model="openai/gpt-5", api_key="secret", tls={"verify": False})


def test_managed_repo_accepts_a_typed_per_hive_routing_override():
    entry = ManagedRepoEntry(
        work={
            "routing": {
                "policy": "strict",
                "tiers": [{"model": "openai/gpt-5", "floor": "COMPLEX"}],
            }
        }
    )
    assert entry.work is not None
    assert entry.work.routing.policy == "strict"
    assert entry.work.routing.tiers[0].floor is ComplexityTier.COMPLEX
    assert entry.work.routing.tiers[0].ceiling is ComplexityTier.REASONING


def test_readers_layer_global_and_per_hive_routing_leaves():
    cfg = {
        "work": {
            "routing": {
                "policy": "loose",
                "tiers": [{"model": "openai/gpt-5-mini", "ceiling": "MEDIUM"}],
            }
        }
    }
    entry = {
        "work": {
            "routing": {
                "policy": "strict",
                "tiers": [{"model": "anthropic/claude-opus-4-1", "floor": "COMPLEX"}],
            }
        }
    }

    assert config.routing_policy(cfg, {}) == "loose"
    assert config.routing_tiers(cfg, {})[0].floor is ComplexityTier.SIMPLE
    assert config.routing_policy(cfg, entry) == "strict"
    per_hive = config.routing_tiers(cfg, entry)
    assert [tier.model for tier in per_hive] == ["anthropic/claude-opus-4-1"]
    assert per_hive[0].ceiling is ComplexityTier.REASONING


def test_reader_policy_tolerates_hand_edited_drift_without_implementing_resolution():
    assert config.routing_policy({"work": {"routing": {"policy": "bogus"}}}, {}) == "loose"


def test_schema_introspection_describes_routing_and_dynamic_tier_members():
    fields = {field.path: field for field in iter_schema_fields()}
    assert fields["work.routing.policy"].default == '"loose"'
    assert "loose" in fields["work.routing.policy"].type
    assert fields["work.routing.tiers"].default == "[]"
    assert fields["work.routing.tiers[].model"].default == "(required)"
    assert "provider/model-name" in fields["work.routing.tiers[].model"].description
    assert fields["work.routing.tiers[].floor"].default == '"SIMPLE"'
    assert "omitted means SIMPLE" in fields["work.routing.tiers[].floor"].description


def test_routing_configuration_is_fleet_partitioned():
    assert partition_of("work.routing.policy") == FLEET
    assert partition_of("work.routing.tiers") == FLEET


def test_dotted_write_validates_and_round_trips_tiers_with_comments(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("# routing comment must survive\nschema_version: 1\nwork: {}\n")
    monkeypatch.setenv("BH_CONFIG", str(path))

    tiers = [
        {"model": "openai/gpt-5-mini", "ceiling": "MEDIUM"},
        {
            "model": "anthropic/claude-opus-4-1",
            "floor": "COMPLEX",
            "endpoint": "primary-gateway",
        },
    ]
    result = config.set_value("work.routing.tiers", json.dumps(tiers), as_json=True)

    assert result["ok"] is True
    assert "# routing comment must survive" in path.read_text()
    assert config.get_value("work.routing.tiers", scope=config.SCOPE_HOST)["value"] == tiers
    assert config.routing_tiers(config.load_host(), None)[0].floor is ComplexityTier.SIMPLE


def test_dotted_write_rejects_malformed_tier_without_mutating_config():
    cfg = {"work": {"routing": {"tiers": [{"model": "openai/gpt-5"}]}}}
    before = json.loads(json.dumps(cfg))
    result = config.set_value(
        "work.routing.tiers",
        '[{"model":"missing-provider","floor":"simple"}]',
        as_json=True,
        cfg=cfg,
    )
    assert result["ok"] is False
    assert cfg == before
    assert "provider/model-name" in result["problems"][0]["message"]


def test_shipped_template_documents_bounds_endpoint_and_default_policy():
    text = Path(config.template("config.example.yaml")).read_text()
    assert "policy: loose" in text
    assert "model: openai/gpt-5-mini" in text
    assert "floor omitted => SIMPLE" in text
    assert "endpoint omitted => configured role/harness default" in text
