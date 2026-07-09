"""Sugar verbs — ws otel enable/disable/endpoint and ws rig enable/disable <feature>.

otel verbs: thin delegates to config.set_value for flat otel.* keys.
rig enable/disable: nested <feature>.enabled write into a triplet-keyed managed_repos entry,
leaving other entries and top-level config untouched. Round-trip comments + flow-style
managed_repos entries are preserved.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config
from beadhive.cli import app

FIXTURE = Path(__file__).parent / "fixture_config.yaml"

# "workspace" resolves via resolve_rig (flexible mode: prefix match) from the fixture.
RIG_ID = "workspace"


@pytest.fixture
def cfg_path(tmp_path, monkeypatch) -> Path:
    """A temp copy of the fixture config wired in via $WS_CONFIG."""
    p = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, p)
    monkeypatch.setenv("WS_CONFIG", str(p))
    return p


# ---- core helper unit test --------------------------------------------------


def test_set_rig_feature_flag_core(cfg_path):
    """set_rig_feature_flag mutates the entry in-place and persists after save."""
    cfg = config.load()
    entry = next(e for e in cfg["managed_repos"] if str(e["repo"]) == "workspace")

    res = config.set_rig_feature_flag(entry, "observaloop", True)

    assert res["ok"] is True
    assert res["new"] is True
    assert res["old"] is None  # was not previously set
    assert entry["observaloop"]["enabled"] is True

    config.save(cfg)

    cfg2 = config.load()
    entry2 = next(e for e in cfg2["managed_repos"] if str(e["repo"]) == "workspace")
    assert entry2["observaloop"]["enabled"] is True


def test_set_rig_feature_flag_toggle(cfg_path):
    """Enable then disable reports correct old/new values."""
    cfg = config.load()
    entry = next(e for e in cfg["managed_repos"] if str(e["repo"]) == "workspace")

    r1 = config.set_rig_feature_flag(entry, "observaloop", True)
    assert r1["old"] is None and r1["new"] is True

    r2 = config.set_rig_feature_flag(entry, "observaloop", False)
    assert r2["old"] is True and r2["new"] is False


# ---- otel sugar: flat key delegation ----------------------------------------


def test_otel_enable_sets_flat_key(cfg_path):
    res = CliRunner().invoke(app, ["otel", "enable"])
    assert res.exit_code == 0, res.output
    assert config.get_value("otel.enabled")["value"] is True


def test_otel_disable_sets_flat_key(cfg_path):
    config.set_value("otel.enabled", "true")  # pre-set so there is something to disable
    res = CliRunner().invoke(app, ["otel", "disable"])
    assert res.exit_code == 0, res.output
    assert config.get_value("otel.enabled")["value"] is False


def test_otel_endpoint_sets_flat_key(cfg_path):
    res = CliRunner().invoke(app, ["otel", "endpoint", "http://localhost:4317"])
    assert res.exit_code == 0, res.output
    assert config.get_value("otel.endpoint")["value"] == "http://localhost:4317"


def test_otel_enable_preserves_comments_and_flow_style(cfg_path):
    CliRunner().invoke(app, ["otel", "enable"])
    text = cfg_path.read_text()
    assert "round-trip must preserve this comment" in text
    assert '{"provider": "github", "org": "agentguides"' in text
    assert "otel:" in text


# ---- rig enable/disable: nested per-entry write --------------------------------


def test_rig_enable_sets_nested_feature_flag(cfg_path):
    res = CliRunner().invoke(app, ["rig", "enable", "observaloop", RIG_ID])
    assert res.exit_code == 0, res.output

    cfg = config.load()
    entry = next(e for e in cfg["managed_repos"] if str(e["repo"]) == "workspace")
    assert entry["observaloop"]["enabled"] is True


def test_rig_disable_sets_nested_feature_flag(cfg_path):
    CliRunner().invoke(app, ["rig", "enable", "observaloop", RIG_ID])
    res = CliRunner().invoke(app, ["rig", "disable", "observaloop", RIG_ID])
    assert res.exit_code == 0, res.output

    cfg = config.load()
    entry = next(e for e in cfg["managed_repos"] if str(e["repo"]) == "workspace")
    assert entry["observaloop"]["enabled"] is False


def test_rig_enable_leaves_other_entries_untouched(cfg_path):
    """Enabling a feature on workspace must not touch the agentguides/infra entry."""
    CliRunner().invoke(app, ["rig", "enable", "observaloop", RIG_ID])

    cfg = config.load()
    other = next(e for e in cfg["managed_repos"] if str(e["repo"]) == "infra")
    assert "observaloop" not in other


def test_rig_enable_preserves_round_trip_style(cfg_path):
    """Comments and flow-style entries survive a rig enable write."""
    CliRunner().invoke(app, ["rig", "enable", "observaloop", RIG_ID])
    text = cfg_path.read_text()
    assert "round-trip must preserve this comment" in text
    assert '{"provider": "github", "org": "agentguides"' in text


def test_rig_feature_resolved_by_triplet_not_by_arbitrary_name(cfg_path):
    """resolve_rig matches by prefix/triplet, not by an unrelated string."""
    # "workspace" resolves by prefix in flexible mode to briancripe/workspace
    res = CliRunner().invoke(app, ["rig", "enable", "observaloop", RIG_ID])
    assert res.exit_code == 0, res.output

    cfg = config.load()
    entry = next(
        e
        for e in cfg["managed_repos"]
        if str(e["org"]) == "briancripe" and str(e["repo"]) == "workspace"
    )
    assert entry["observaloop"]["enabled"] is True


def test_rig_enable_unknown_rig_exits_nonzero(cfg_path):
    """A rig id that does not match any entry in managed_repos must fail."""
    res = CliRunner().invoke(app, ["rig", "enable", "observaloop", "no-such-rig"])
    assert res.exit_code != 0
