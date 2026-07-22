"""harness_name — the layered getter for `bh role <seat>`'s harness (claude|opencode).

Precedence: BH_HARNESS env > per-hive `entry['harness']` > global `harness` > "claude".
Also covers otel_genai_system following harness_name once no explicit gen_ai.system override
is configured (bh-73rz.1).
"""

from __future__ import annotations

from beadhive import config

# ---- harness_name ----


def test_harness_name_default_is_claude():
    assert config.harness_name({}, None) == "claude"
    assert config.harness_name({"harness": "claude"}, {}) == "claude"


def test_harness_name_global_then_per_hive_override():
    cfg = {"harness": "opencode"}
    # global wins when the hive has no override
    assert config.harness_name(cfg, {}) == "opencode"
    # per-hive entry overrides the global
    assert config.harness_name(cfg, {"harness": "claude"}) == "claude"


def test_harness_name_env_wins_over_everything(monkeypatch):
    monkeypatch.setenv("BH_HARNESS", "opencode")
    cfg = {"harness": "claude"}
    entry = {"harness": "claude"}
    assert config.harness_name(cfg, entry) == "opencode"


def test_harness_name_env_unset_falls_through_to_config(monkeypatch):
    monkeypatch.delenv("BH_HARNESS", raising=False)
    assert config.harness_name({"harness": "opencode"}, {}) == "opencode"


# ---- otel_genai_system follows harness_name ----


def test_otel_genai_system_defaults_to_harness_name():
    assert config.otel_genai_system({"harness": "opencode"}, None) == "opencode"
    assert config.otel_genai_system({}, None) == "claude"


def test_otel_genai_system_explicit_override_wins_over_harness():
    cfg = {"harness": "opencode", "otel": {"genai": {"system": "custom-system"}}}
    assert config.otel_genai_system(cfg, None) == "custom-system"


def test_otel_genai_system_env_wins_over_harness(monkeypatch):
    monkeypatch.setenv("BH_GENAI_SYSTEM", "env-system")
    assert config.otel_genai_system({"harness": "opencode"}, None) == "env-system"
