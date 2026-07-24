"""release-setting resolution (bh-k2j8.1) — release.* follows the same per-hive > global >
default layering as work.* (config.layered, mirroring dispatch_value). Advisory-only: an
absent `release` section resolves every key to its built-in default."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beadhive import config
from beadhive.config_schema import BeadhiveConfig, ManagedRepoEntry, ReleaseConfig

# ---- release.strategy ---------------------------------------------------------


def test_release_strategy_default_and_override():
    assert config.release_strategy({}, None) == "stable-versioning"
    assert config.release_strategy({"release": {}}, {}) == "stable-versioning"
    glob = {"release": {"strategy": "stable-versioning"}}
    assert config.release_strategy(glob, {}) == "stable-versioning"
    # per-hive override beats the global value
    hive = {"release": {"strategy": "future-strategy"}}
    assert config.release_strategy(glob, hive) == "future-strategy"


# ---- release.enforce_hold ------------------------------------------------------


def test_release_enforce_hold_default_and_override():
    assert config.release_enforce_hold({}, None) is False
    glob = {"release": {"enforce_hold": True}}
    assert config.release_enforce_hold(glob, {}) is True
    # per-hive override beats the global value
    assert config.release_enforce_hold(glob, {"release": {"enforce_hold": False}}) is False


# ---- release.fix_churn_budget --------------------------------------------------


def test_release_fix_churn_budget_default_and_override():
    assert config.release_fix_churn_budget({}, None) == 3
    glob = {"release": {"fix_churn_budget": 5}}
    assert config.release_fix_churn_budget(glob, {}) == 5
    assert config.release_fix_churn_budget(glob, {"release": {"fix_churn_budget": 1}}) == 1


# ---- release.conflict_estimator ------------------------------------------------


def test_release_conflict_estimator_default_and_override():
    assert config.release_conflict_estimator({}, None) == "file-overlap"
    glob = {"release": {"conflict_estimator": "file-overlap"}}
    assert config.release_conflict_estimator(glob, {}) == "file-overlap"
    hive = {"release": {"conflict_estimator": "structural"}}
    assert config.release_conflict_estimator(glob, hive) == "structural"


# ---- layered precedence: per-hive > global > default (acceptance criterion) ----


def test_release_layering_precedence_all_keys():
    """All four keys resolve per-hive > global > default in one pass, exactly like the
    existing work.dispatch.* tiers."""
    glob = {
        "release": {
            "strategy": "stable-versioning",
            "enforce_hold": True,
            "fix_churn_budget": 4,
            "conflict_estimator": "file-overlap",
        }
    }
    # no per-hive entry -> global wins
    assert config.release_strategy(glob, None) == "stable-versioning"
    assert config.release_enforce_hold(glob, None) is True
    assert config.release_fix_churn_budget(glob, None) == 4
    assert config.release_conflict_estimator(glob, None) == "file-overlap"

    # per-hive entry only overrides the keys it sets; the rest still fall through to global
    entry = {"release": {"fix_churn_budget": 0}}
    assert config.release_strategy(glob, entry) == "stable-versioning"
    assert config.release_enforce_hold(glob, entry) is True
    assert config.release_fix_churn_budget(glob, entry) == 0
    assert config.release_conflict_estimator(glob, entry) == "file-overlap"


def test_release_value_generic_helper_matches_specific_getters():
    cfg = {"release": {"strategy": "x"}}
    assert config.release_value(cfg, None, "strategy") == "x"
    assert config.release_value(cfg, None, "missing", "fallback") == "fallback"


def test_release_cfg_returns_global_section_or_empty_dict():
    assert config.release_cfg({}) == {}
    assert config.release_cfg({"release": {"strategy": "x"}}) == {"strategy": "x"}


# ---- config_schema.ReleaseConfig -----------------------------------------------


def test_release_config_defaults():
    rc = ReleaseConfig()
    assert rc.strategy == "stable-versioning"
    assert rc.enforce_hold is False
    assert rc.fix_churn_budget == 3
    assert rc.conflict_estimator == "file-overlap"


def test_beadhive_config_carries_release_section_by_default():
    cfg = BeadhiveConfig()
    assert cfg.release.strategy == "stable-versioning"
    assert cfg.release.enforce_hold is False


def test_beadhive_config_accepts_release_overrides():
    cfg = BeadhiveConfig(release={"fix_churn_budget": 7, "enforce_hold": True})
    assert cfg.release.fix_churn_budget == 7
    assert cfg.release.enforce_hold is True
    # a partial override doesn't wipe sibling fields (nested_model_default_partial_update)
    assert cfg.release.strategy == "stable-versioning"
    assert cfg.release.conflict_estimator == "file-overlap"


def test_beadhive_config_rejects_unknown_release_key():
    with pytest.raises(ValidationError):
        BeadhiveConfig(release={"strategy": "x", "bogus_key": True})


def test_managed_repo_entry_accepts_release_override():
    entry = ManagedRepoEntry(release={"strategy": "y"})
    assert entry.release is not None
    assert entry.release.strategy == "y"


def test_env_override_release_field(monkeypatch):
    monkeypatch.setenv("BH_RELEASE__FIX_CHURN_BUDGET", "9")
    cfg = BeadhiveConfig()
    assert cfg.release.fix_churn_budget == 9
    # sibling fields keep their defaults (partial update)
    assert cfg.release.strategy == "stable-versioning"


# ---- dotted config get/set/unset + validate ------------------------------------


def test_dotted_get_set_unset_round_trips_release_keys():
    cfg = {}
    res = config.set_value("release.strategy", "stable-versioning", cfg=cfg)
    assert res["ok"] is True
    assert config.get_value("release.strategy", cfg=cfg)["value"] == "stable-versioning"

    res = config.set_value("release.enforce_hold", "true", cfg=cfg)
    assert res["ok"] is True
    assert config.get_value("release.enforce_hold", cfg=cfg)["value"] is True

    res = config.unset_value("release.enforce_hold", cfg=cfg)
    assert res["ok"] is True
    assert config.get_value("release.enforce_hold", cfg=cfg)["ok"] is False


def test_dotted_set_release_is_a_known_section_no_warning():
    cfg = {}
    res = config.set_value("release.strategy", "stable-versioning", cfg=cfg)
    unknown_warnings = [
        p
        for p in res["problems"]
        if p["level"] == "warning" and "unknown config section" in p["message"]
    ]
    assert unknown_warnings == []


def test_validate_config_accepts_release_section():
    from beadhive import config_validate

    cfg = {
        "schema_version": 1,
        "release": {"strategy": "stable-versioning", "fix_churn_budget": 2},
    }
    problems = config_validate.validate_config(cfg)
    assert problems == []


def test_validate_config_rejects_unknown_release_key():
    from beadhive import config_validate

    cfg = {"schema_version": 1, "release": {"bogus_key": True}}
    problems = config_validate.validate_config(cfg)
    assert any("release" in p["message"] for p in problems)
