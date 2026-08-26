"""Layered, open-vocabulary configuration for the Herdr plugin."""

from __future__ import annotations

import pytest

from beadhive import config
from beadhive.config_partition import HOST, partition_of
from beadhive.config_schema import BeadhiveConfig, ManagedRepoEntry
from beadhive.config_validate import validate_config


def test_herdr_kind_global_then_per_hive_override():
    cfg = {"herdr": {"kind": "claude"}}

    assert config.herdr_kind(cfg, {}) == "claude"
    assert config.herdr_kind(cfg, {"herdr": {"kind": "codex"}}) == "codex"
    assert config.herdr_kind({}, {}) is None
    assert partition_of("herdr.kind") == HOST


def test_herdr_kind_schema_accepts_future_external_kind_without_catalogue():
    cfg = BeadhiveConfig.model_validate(
        {
            "herdr": {"kind": "future-agent"},
            "managed_repos": [
                {
                    "provider": "github",
                    "org": "acme",
                    "repo": "app",
                    "prefix": "app",
                    "herdr": {"kind": "future-agent"},
                }
            ],
        }
    )

    assert cfg.herdr.kind == "future-agent"
    assert cfg.managed_repos[0].herdr.kind == "future-agent"
    assert validate_config(cfg.model_dump()) == []


@pytest.mark.parametrize("value", ["", " codex", "codex "])
def test_herdr_kind_schema_rejects_empty_or_untrimmed_values(value):
    with pytest.raises(ValueError, match="herdr.kind"):
        ManagedRepoEntry.model_validate({"herdr": {"kind": value}})
