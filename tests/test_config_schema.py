"""BeadhiveConfig (config_schema.py) — the pydantic-settings schema layer.

Covers: SCHEMA_VERSION / schema_version defaults, validating the shipped
config.example.yaml, extra="forbid" rejecting unknown top-level + nested keys, BH_ env
overrides (including the deprecated-name-free nested delimiter form), and a partial nested
override merging with a section's defaults rather than wiping its sibling fields
(nested_model_default_partial_update).

This is a schema/validation-layer test module — it does NOT exercise the ~40 existing
config.py getters or the ruamel round-trip read/write path (untouched by this bead).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from beadhive import config
from beadhive.config_schema import SCHEMA_VERSION, BeadhiveConfig

_EXAMPLE_YAML = Path(config.template("config.example.yaml"))


def _load_example() -> dict:
    y = YAML()
    return dict(y.load(_EXAMPLE_YAML.read_text()))


# ---- schema_version / SCHEMA_VERSION -----------------------------------------


def test_schema_version_constant_is_1():
    assert SCHEMA_VERSION == 1


def test_fresh_model_carries_schema_version_1():
    assert BeadhiveConfig().schema_version == 1


def test_example_yaml_and_config_init_output_carry_schema_version_1():
    """config_init copies config.example.yaml verbatim (src/beadhive/cli.py config_init), so
    stamping the template is sufficient for both the shipped example and a freshly-scaffolded
    config to carry schema_version: 1."""
    data = _load_example()
    assert data["schema_version"] == 1


# ---- validates the shipped config.example.yaml -------------------------------


def test_beadhive_config_validates_shipped_example_with_no_errors():
    data = _load_example()
    cfg = BeadhiveConfig(**data)
    assert cfg.schema_version == 1
    assert cfg.providers == ["github", "gitlab", "gitea"]
    assert cfg.dolt.backend == "docker"
    assert cfg.worktrees.bead_branch == "bead/{kind}/{id}"
    assert cfg.managed_repos == []


# ---- extra="forbid" -----------------------------------------------------------


def test_unknown_top_level_key_rejected():
    with pytest.raises(ValidationError):
        BeadhiveConfig(this_key_does_not_exist=True)


def test_unknown_nested_key_rejected():
    with pytest.raises(ValidationError):
        BeadhiveConfig(dolt={"backend": "docker", "this_key_does_not_exist": True})


def test_known_top_level_and_nested_keys_accepted():
    cfg = BeadhiveConfig(dolt={"backend": "podman"}, work={"max_commits": 5})
    assert cfg.dolt.backend == "podman"
    assert cfg.work.max_commits == 5


# ---- BH_ env overrides (env_prefix + env_nested_delimiter) --------------------


def test_env_override_beats_file_value(monkeypatch):
    monkeypatch.setenv("BH_DOLT__BACKEND", "podman")
    cfg = BeadhiveConfig(**{"dolt": {"backend": "docker"}})
    assert cfg.dolt.backend == "podman"


def test_env_override_top_level_scalar(monkeypatch):
    monkeypatch.setenv("BH_DELIMITER", "/")
    cfg = BeadhiveConfig(**{"delimiter": ":"})
    assert cfg.delimiter == "/"


def test_env_override_deeply_nested_field(monkeypatch):
    monkeypatch.setenv("BH_WORK__DISPATCH__MODE", "collapsed")
    cfg = BeadhiveConfig()
    assert cfg.work.dispatch.mode == "collapsed"


# ---- nested_model_default_partial_update --------------------------------------


def test_partial_nested_env_override_merges_with_sibling_defaults(monkeypatch):
    """Setting only BH_WORK__MAX_COMMITS must not wipe WorkConfig's other fields — they
    keep their defaults rather than the whole `work` section failing/blanking out."""
    monkeypatch.setenv("BH_WORK__MAX_COMMITS", "3")
    cfg = BeadhiveConfig()
    assert cfg.work.max_commits == 3
    assert cfg.work.validate_cmd == "just check"
    assert cfg.work.review_gate == "human"
    assert cfg.work.landing == "local"


def test_partial_nested_kwarg_override_merges_with_sibling_defaults():
    """Same guarantee via direct kwargs (init-source partial update, not just env)."""
    cfg = BeadhiveConfig(work={"review_gate": "gh:pr"})
    assert cfg.work.review_gate == "gh:pr"
    assert cfg.work.validate_cmd == "just check"
    assert cfg.work.max_commits == 10


def test_partial_override_does_not_wipe_dispatch_sub_section():
    """A partial override of one work.* field leaves the nested dispatch sub-model at its
    own defaults, not missing/blank."""
    cfg = BeadhiveConfig(work={"max_commits": 7})
    assert cfg.work.max_commits == 7
    assert cfg.work.dispatch.mode == "fanout"
    assert cfg.work.dispatch.max_depth == 2
