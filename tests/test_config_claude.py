"""claude config section — source/scope/marketplace/plugin accessors with per-rig override."""

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


def test_claude_marketplace_default_resolves_to_ws_repo_root():
    """Regression: the default must be an absolute path — the
    current Claude CLI rejects a bare '.', and a cwd-relative path would register
    the invoker's cwd instead of the marketplace repo."""
    val = config.claude_marketplace({}, None)
    assert Path(val).is_absolute()
    assert val == str(Path(config.__file__).resolve().parents[2])


def test_claude_marketplace_remote_forms_pass_through():
    for mp in ("owner/repo", "https://github.com/briancripe/workspace"):
        assert config.claude_marketplace({"claude": {"marketplace": mp}}, {}) == mp


def test_claude_marketplace_override():
    url = "https://github.com/briancripe/workspace"
    cfg = {"claude": {"marketplace": url}}
    assert config.claude_marketplace(cfg, {}) == url
    # per-rig beats global; local '.' still resolves absolute
    per_hive = config.claude_marketplace(cfg, {"claude": {"marketplace": "."}})
    assert per_hive == str(Path(config.__file__).resolve().parents[2])


# ---- claude_marketplace: primary-clone anchor ----


def _mk_marketplace(root: Path, plugin: str = "bh") -> None:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "workspace", "plugins": [{"name": plugin}]})
    )


def test_claude_marketplace_anchors_at_registered_hive_primary_clone(tmp_path, monkeypatch):
    """Regression: local marketplace values must anchor at the
    registered rig's PRIMARY CLONE ($GIT_WORKSPACE/provider/org/repo), not the running
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
    """A registered rig whose manifest lacks the configured plugin is not the anchor —
    the scan picks the rig that actually vends it."""
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


def test_claude_marketplace_falls_back_to_package_anchor(tmp_path, monkeypatch):
    """No registered rig hosts the plugin's marketplace (e.g. wheel install with an
    unregistered workspace repo, or a bare dev checkout) → package anchor, old behavior."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "bare"}]}
    assert config.claude_marketplace(cfg, None) == str(Path(config.__file__).resolve().parents[2])


# ---- claude_plugin_name ----


def test_claude_plugin_name_default_is_bh():
    assert config.claude_plugin_name({}, None) == "bh"


def test_claude_plugin_name_override():
    cfg = {"claude": {"plugin": "myagf"}}
    assert config.claude_plugin_name(cfg, {}) == "myagf"
    # per-rig beats global
    assert config.claude_plugin_name(cfg, {"claude": {"plugin": "agf"}}) == "agf"


# ---- KNOWN_SECTIONS includes 'claude' ----


def test_claude_in_known_sections():
    assert "claude" in config.KNOWN_SECTIONS


# ---- per-rig override (managed_repos entry) ----


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
    assert config.claude_marketplace(cfg, entry_override) == str(
        Path(config.__file__).resolve().parents[2]
    )

    assert config.claude_plugin_name(cfg, entry_global) == "custom"
    assert config.claude_plugin_name(cfg, entry_override) == "agf"
