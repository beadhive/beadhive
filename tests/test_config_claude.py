"""claude config section — source/scope/marketplace/plugin accessors with per-hive override."""

from __future__ import annotations

import json
from pathlib import Path

from beadhive import config

# ---- claude_source ----


def test_claude_source_default_is_plugin():
    assert config.claude_source({}, None) == "plugin"
    assert config.claude_source({"claude": {}}, {}) == "plugin"


def test_claude_source_global_then_per_hive_override():
    cfg = {"claude": {"source": "copy"}}
    assert config.claude_source(cfg, {}) == "copy"
    assert config.claude_source(cfg, {"claude": {"source": "plugin"}}) == "plugin"


def test_claude_source_unknown_falls_back_to_plugin():
    cfg = {"claude": {"source": "unknown_value"}}
    assert config.claude_source(cfg, {}) == "plugin"


# ---- claude_scope ----


def test_claude_scope_default_is_user():
    assert config.claude_scope({}, None) == "user"


def test_claude_scope_global_and_per_hive_override():
    cfg = {"claude": {"scope": "project"}}
    assert config.claude_scope(cfg, {}) == "project"
    assert config.claude_scope(cfg, {"claude": {"scope": "user"}}) == "user"


def test_claude_scope_unknown_falls_back_to_user():
    assert config.claude_scope({"claude": {"scope": "bad"}}, {}) == "user"


# ---- claude_marketplace ----


def test_claude_marketplace_default_resolves_to_remote_form():
    """Regression (v0.2.0 field report): with no registered hive vending the plugin and
    no manifest at the package anchor (this repo — the marketplace lives in the separate
    beadhive/claude-plugin repo), the default '.' must resolve to the canonical REMOTE
    form, never the package anchor (under a uv tool install that anchor is the
    interpreter lib dir, where no marketplace can exist)."""
    val = config.claude_marketplace({}, None)
    assert val == config.REMOTE_MARKETPLACE
    assert val == "beadhive/claude-plugin"


def test_claude_marketplace_remote_forms_pass_through():
    for mp in ("owner/repo", "https://github.com/briancripe/workspace"):
        assert config.claude_marketplace({"claude": {"marketplace": mp}}, {}) == mp


def test_claude_marketplace_override():
    url = "https://github.com/briancripe/workspace"
    cfg = {"claude": {"marketplace": url}}
    assert config.claude_marketplace(cfg, {}) == url
    # per-hive beats global; a local '.' with no local marketplace → remote fallback
    per_hive = config.claude_marketplace(cfg, {"claude": {"marketplace": "."}})
    assert per_hive == config.REMOTE_MARKETPLACE


# ---- claude_marketplace: primary-clone anchor ----


def _mk_marketplace(root: Path, plugin: str = "bh") -> None:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "workspace", "plugins": [{"name": plugin}]})
    )


def test_claude_marketplace_anchors_at_registered_hive_primary_clone(tmp_path, monkeypatch):
    """Regression: local marketplace values must anchor at the
    registered hive's PRIMARY CLONE ($GIT_WORKSPACE/provider/org/repo), not the running
    package — a dev CLI run from an ephemeral bead worktree otherwise re-points the
    user-level marketplace at a path that is reclaimed after merge."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    clone = tmp_path / "github" / "acme" / "ws"
    _mk_marketplace(clone)
    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "ws"}]}

    assert config.claude_marketplace(cfg, None) == str(clone.resolve())
    # relative local values resolve inside the primary clone too
    per_hive = config.claude_marketplace(cfg, {"claude": {"marketplace": "./sub"}})
    assert per_hive == str((clone / "sub").resolve())


def test_claude_marketplace_skips_hives_that_do_not_vend_the_plugin(tmp_path, monkeypatch):
    """A registered hive whose manifest lacks the configured plugin is not the anchor —
    the scan picks the hive that actually vends it."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    other = tmp_path / "github" / "acme" / "other"
    _mk_marketplace(other, plugin="not-bh")
    host = tmp_path / "github" / "acme" / "host"
    _mk_marketplace(host)
    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "other"},
            {"provider": "github", "org": "acme", "repo": "host"},
        ]
    }

    assert config.claude_marketplace(cfg, None) == str(host.resolve())


def test_claude_marketplace_falls_back_to_remote_form(tmp_path, monkeypatch):
    """No registered hive hosts the plugin's marketplace AND the package anchor has no
    manifest (wheel / uv tool install) → the canonical remote form, which the Claude CLI
    fetches itself. Never the package anchor (the uv-tool lib dir of the field report)."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "bare"}]}
    assert config.claude_marketplace(cfg, None) == config.REMOTE_MARKETPLACE


def test_claude_marketplace_keeps_package_anchor_when_manifest_present(tmp_path, monkeypatch):
    """The package anchor survives ONLY when it really hosts a marketplace manifest
    vending the plugin (a genuine src checkout of the marketplace repo)."""
    fake_pkg = tmp_path / "src" / "beadhive" / "config.py"  # parents[2] == tmp_path
    fake_pkg.parent.mkdir(parents=True)
    fake_pkg.touch()
    monkeypatch.setattr(config, "__file__", str(fake_pkg))
    _mk_marketplace(tmp_path)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    cfg = {"managed_repos": []}
    assert config.claude_marketplace(cfg, None) == str(tmp_path.resolve())


def test_claude_marketplace_explicit_absolute_path_resolves_without_anchor(tmp_path):
    """An explicit absolute local value resolves directly — no anchor, no remote fallback."""
    cfg = {"claude": {"marketplace": str(tmp_path)}}
    assert config.claude_marketplace(cfg, {}) == str(tmp_path.resolve())


# ---- claude_plugin_name ----


def test_claude_plugin_name_default_is_bh():
    assert config.claude_plugin_name({}, None) == "bh"


def test_claude_plugin_name_override():
    cfg = {"claude": {"plugin": "myagf"}}
    assert config.claude_plugin_name(cfg, {}) == "myagf"
    # per-hive beats global
    assert config.claude_plugin_name(cfg, {"claude": {"plugin": "agf"}}) == "agf"


# ---- KNOWN_SECTIONS includes 'claude' ----


def test_claude_in_known_sections():
    assert "claude" in config.KNOWN_SECTIONS


# ---- per-hive override (managed_repos entry) ----


def test_per_hive_claude_override_independent_of_global():
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
    # per-hive '.' with no local marketplace anywhere → remote fallback
    assert config.claude_marketplace(cfg, entry_override) == config.REMOTE_MARKETPLACE

    assert config.claude_plugin_name(cfg, entry_global) == "custom"
    assert config.claude_plugin_name(cfg, entry_override) == "agf"
