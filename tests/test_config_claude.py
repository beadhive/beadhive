"""claude config section — source/scope/marketplace/plugin accessors with per-rig override."""

from __future__ import annotations

from ws import config

# ---- claude_source ----


def test_claude_source_default_is_plugin():
    assert config.claude_source({}, None) == "plugin"
    assert config.claude_source({"claude": {}}, {}) == "plugin"


def test_claude_source_global_then_per_rig_override():
    cfg = {"claude": {"source": "copy"}}
    assert config.claude_source(cfg, {}) == "copy"
    assert config.claude_source(cfg, {"claude": {"source": "plugin"}}) == "plugin"


def test_claude_source_unknown_falls_back_to_plugin():
    cfg = {"claude": {"source": "unknown_value"}}
    assert config.claude_source(cfg, {}) == "plugin"


# ---- claude_scope ----


def test_claude_scope_default_is_user():
    assert config.claude_scope({}, None) == "user"


def test_claude_scope_global_and_per_rig_override():
    cfg = {"claude": {"scope": "project"}}
    assert config.claude_scope(cfg, {}) == "project"
    assert config.claude_scope(cfg, {"claude": {"scope": "user"}}) == "user"


def test_claude_scope_unknown_falls_back_to_user():
    assert config.claude_scope({"claude": {"scope": "bad"}}, {}) == "user"


# ---- claude_marketplace ----


def test_claude_marketplace_default_is_dot():
    assert config.claude_marketplace({}, None) == "."


def test_claude_marketplace_override():
    url = "https://github.com/briancripe/workspace"
    cfg = {"claude": {"marketplace": url}}
    assert config.claude_marketplace(cfg, {}) == url
    # per-rig beats global
    assert config.claude_marketplace(cfg, {"claude": {"marketplace": "."}}) == "."


# ---- claude_plugin_name ----


def test_claude_plugin_name_default_is_agf():
    assert config.claude_plugin_name({}, None) == "agf"


def test_claude_plugin_name_override():
    cfg = {"claude": {"plugin": "myagf"}}
    assert config.claude_plugin_name(cfg, {}) == "myagf"
    # per-rig beats global
    assert config.claude_plugin_name(cfg, {"claude": {"plugin": "agf"}}) == "agf"


# ---- KNOWN_SECTIONS includes 'claude' ----


def test_claude_in_known_sections():
    assert "claude" in config.KNOWN_SECTIONS


# ---- per-rig override (managed_repos entry) ----


def test_per_rig_claude_override_independent_of_global():
    mp = "https://example.com"
    cfg = {"claude": {"source": "copy", "scope": "project", "marketplace": mp, "plugin": "custom"}}
    entry_global = {}  # use global
    entry_override = {
        "claude": {"source": "plugin", "scope": "user", "marketplace": ".", "plugin": "agf"}
    }

    assert config.claude_source(cfg, entry_global) == "copy"
    assert config.claude_source(cfg, entry_override) == "plugin"

    assert config.claude_scope(cfg, entry_global) == "project"
    assert config.claude_scope(cfg, entry_override) == "user"

    assert config.claude_marketplace(cfg, entry_global) == mp
    assert config.claude_marketplace(cfg, entry_override) == "."

    assert config.claude_plugin_name(cfg, entry_global) == "custom"
    assert config.claude_plugin_name(cfg, entry_override) == "agf"
