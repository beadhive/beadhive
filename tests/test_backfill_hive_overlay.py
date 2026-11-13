"""Per-hive backfill config: ``backfill.hives.<prefix>`` over the host block (bh-g71fi, ADR
``backfill-rerun-control-adr.md`` Q1)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from beadhive import backfill_continuous, backfill_coverage
from beadhive.backfill_cli import gapfill_settings
from beadhive.backfill_effective import (
    BackfillHiveError,
    continuous_may_run,
    effective_backfill,
    effective_cfg,
    layer_of,
    merge,
)
from beadhive.cli import app
from beadhive.modules.config.contracts import BackfillConfig, BackfillHiveOverlay

ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
WIDGETS = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "wd"}
GADGETS = {"provider": "github", "org": "acme", "repo": "gadgets", "prefix": "gd"}
NOPREFIX = {"provider": "github", "org": "acme", "repo": "bare"}

HOST_BLOCK = {
    "collate": {
        "create": "off",
        "areas": {"core": "engine", "docs/": "documentation"},
        "include_types": ["feat", "fix"],
    },
    "judge": {"enabled": False},
}


def _cfg(block, *entries):
    return {"managed_repos": list(entries or (WIDGETS, GADGETS)), "backfill": block}


# ---- model -----------------------------------------------------------------------------


def test_overlay_is_backfill_config_minus_judge_and_hives():
    assert set(BackfillHiveOverlay.model_fields) == set(BackfillConfig.model_fields) - {
        "judge",
        "hives",
    }
    with pytest.raises(ValidationError):
        BackfillHiveOverlay.model_validate({"judge": {"enabled": True}})
    with pytest.raises(ValidationError):
        BackfillHiveOverlay.model_validate({"hives": {"x": {}}})


# ---- resolver --------------------------------------------------------------------------


def test_merge_mappings_by_key_lists_and_scalars_replace():
    host = {"a": {"x": 1, "y": {"p": 1, "q": 2}}, "l": [1, 2], "s": "h"}
    over = {"a": {"y": {"q": 9}}, "l": [3], "s": "o"}
    assert merge(host, over) == {"a": {"x": 1, "y": {"p": 1, "q": 9}}, "l": [3], "s": "o"}
    assert host["a"]["y"]["q"] == 2  # inputs untouched


def test_no_overlay_is_the_host_block_byte_for_byte():
    cfg = _cfg(HOST_BLOCK)
    for hive in (None, "wd", "gd", WIDGETS, NOPREFIX):
        assert effective_backfill(cfg, hive) == HOST_BLOCK
    # an overlay for another hive leaves this hive alone
    other = _cfg({**HOST_BLOCK, "hives": {"gd": {"collate": {"create": "propose"}}}})
    assert effective_backfill(other, "wd") == HOST_BLOCK
    assert json.dumps(effective_backfill(other, "wd"), sort_keys=True) == json.dumps(
        HOST_BLOCK, sort_keys=True
    )


def test_overlay_wins_key_by_key_and_hides_the_hives_map():
    cfg = _cfg(
        {
            **HOST_BLOCK,
            "hives": {
                "wd": {
                    "collate": {
                        "create": "propose",
                        "areas": {"core": "kernel", "ui": "frontend"},
                        "include_types": ["feat"],
                    }
                }
            },
        }
    )
    eff = effective_backfill(cfg, "wd")
    collate = eff["collate"]
    assert collate["create"] == "propose"
    assert collate["areas"] == {"core": "kernel", "docs/": "documentation", "ui": "frontend"}
    assert collate["include_types"] == ["feat"]  # list replaces
    assert eff["judge"] == HOST_BLOCK["judge"]
    assert "hives" not in eff
    # the sibling hive still sees the host
    assert effective_backfill(cfg, "gd")["collate"]["areas"] == HOST_BLOCK["collate"]["areas"]
    assert BackfillConfig.model_validate(eff).collate.areas["ui"] == "frontend"


def test_overlay_by_entry_and_by_cwd_hive(monkeypatch):
    cfg = _cfg({"hives": {"wd": {"collate": {"epics": False}}}})
    assert effective_backfill(cfg, WIDGETS)["collate"]["epics"] is False
    monkeypatch.setattr("beadhive.registry.current_hive", lambda c: WIDGETS)
    assert effective_backfill(cfg, "")["collate"]["epics"] is False


def test_hive_without_prefix_gets_a_clear_error_when_overlays_exist():
    cfg = _cfg({"hives": {"wd": {"collate": {"epics": False}}}}, WIDGETS, NOPREFIX)
    with pytest.raises(BackfillHiveError, match="no registered prefix"):
        effective_backfill(cfg, NOPREFIX)
    # without any overlay the hive keeps working, exactly as before
    assert effective_backfill(_cfg(HOST_BLOCK, NOPREFIX), NOPREFIX) == HOST_BLOCK


def test_overlay_judge_is_rejected():
    cfg = _cfg({"hives": {"wd": {"judge": {"enabled": True}}}})
    with pytest.raises(BackfillHiveError, match="judge"):
        effective_backfill(cfg, "wd")
    with pytest.raises(ValidationError):
        BackfillConfig.model_validate({"hives": {"wd": {"judge": {"enabled": True}}}})


def test_layer_of_names_the_deciding_layer():
    cfg = _cfg({**HOST_BLOCK, "hives": {"wd": {"collate": {"areas": {"ui": "frontend"}}}}})
    assert layer_of(cfg, "wd", ["collate", "areas"]) == "backfill.hives.wd"
    assert layer_of(cfg, "wd", ["collate", "create"]) == "host"
    assert layer_of(cfg, "gd", ["collate", "areas"]) == "host"


# ---- read sites ------------------------------------------------------------------------


def test_continuous_settings_resolve_per_hive():
    cfg = _cfg(
        {
            "continuous": {"enabled": False},
            "hives": {
                "wd": {"continuous": {"enabled": True, "passes": ["commits"], "triggers": ["sync"]}}
            },
        }
    )
    assert continuous_may_run(cfg) is True
    on = backfill_continuous.settings(cfg, WIDGETS)
    assert on.runs_on("sync") and on.passes == ("commits",)
    assert backfill_continuous.settings(cfg, GADGETS).enabled is False
    assert backfill_continuous.settings(cfg).enabled is False
    assert continuous_may_run(_cfg({"continuous": {"enabled": False}})) is False
    assert continuous_may_run({}) is False


def test_continuous_setting_off_when_the_overlay_cannot_resolve():
    cfg = _cfg({"hives": {"wd": {"continuous": {"enabled": True}}}}, NOPREFIX)
    assert backfill_continuous.settings(cfg, NOPREFIX).enabled is False


def test_coverage_and_gapfill_read_the_overlay():
    cfg = _cfg(
        {
            "coverage": {"enabled": False},
            "hives": {
                "wd": {
                    "coverage": {"enabled": True, "doc_roots": ["handbook"]},
                    "timeline": {"gapfill": {"enabled": True}},
                }
            },
        }
    )
    assert backfill_coverage.refresh_enabled(effective_cfg(cfg, WIDGETS)) is True
    assert backfill_coverage.refresh_enabled(effective_cfg(cfg, GADGETS)) is False
    assert backfill_coverage.coverage_settings(effective_cfg(cfg, WIDGETS)).doc_roots == (
        "handbook",
    )
    assert gapfill_settings(cfg, "wd").enabled is True
    assert gapfill_settings(cfg, "gd").enabled is False
    assert gapfill_settings(cfg).enabled is False


# ---- config set / get ------------------------------------------------------------------


@pytest.fixture
def bh_home(tmp_path, monkeypatch):
    home = tmp_path / "bh-home"
    (home / "hq").mkdir(parents=True)
    monkeypatch.setenv("BH_HOME", str(home))
    for var in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ"):
        monkeypatch.delenv(var, raising=False)
    (home / "hq" / "fleet.yaml").write_text(
        "schema_version: 1\ndelimiter: ':'\nproviders: [github]\nexclude:\n  orgs: []\n"
        "  repos: []\nmanaged_repos:\n"
        "  - {provider: github, org: acme, repo: widgets, prefix: wd}\n"
        "  - {provider: github, org: acme, repo: bare, prefix: ''}\n"
    )
    (home / "config.yaml").write_text("backfill:\n  collate:\n    areas:\n      core: engine\n")
    return home


def _cli(*args: str, code: int = 0):
    result = CliRunner().invoke(app, list(args), env=ENV)
    assert result.exit_code == code, result.output
    return result


def test_set_writes_the_overlay_through_the_existing_writer(bh_home):
    _cli("config", "set", "backfill.hives.wd.collate.areas", '{"ui": "frontend"}', "--json")
    got = _cli("config", "get", "backfill.hives.wd.collate.areas")
    assert json.loads(got.stdout) == {"ui": "frontend"}
    assert "hives:" in (bh_home / "config.yaml").read_text()


def test_set_overlay_judge_is_refused(bh_home):
    _cli("config", "set", "backfill.hives.wd.judge.enabled", "true", code=1)
    assert "hives" not in (bh_home / "config.yaml").read_text()


def test_fleet_scope_write_to_the_overlay_is_refused(bh_home):
    result = _cli(
        "config",
        "set",
        "backfill.hives.wd.collate.create",
        "propose",
        "--scope",
        "fleet",
        code=1,
    )
    assert "host-owned" in result.output
    assert "hives" not in (bh_home / "hq" / "fleet.yaml").read_text()


def test_get_hive_shows_effective_value_and_layer(bh_home):
    _cli("config", "set", "backfill.hives.wd.collate.areas", '{"ui": "fe"}', "--json")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "get", "backfill.collate.areas", "--hive", "wd"], env=ENV)
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {"core": "engine", "ui": "fe"}
    assert "layer: backfill.hives.wd" in r.stderr
    # a value the overlay does not set comes from the host layer
    _cli("config", "set", "backfill.collate.epics", "false")
    r = runner.invoke(app, ["config", "get", "backfill.collate.epics", "--hive", "wd"], env=ENV)
    assert r.stdout.strip() == "false"
    assert "layer: host" in r.stderr


def test_get_hive_without_prefix_errors_and_rejects_scope(bh_home):
    runner = CliRunner()
    r = runner.invoke(app, ["config", "get", "backfill.collate.areas", "--hive", "bare"], env=ENV)
    assert r.exit_code == 1
    assert "no registered prefix" in r.output
    r = runner.invoke(
        app, ["config", "get", "backfill.collate.areas", "--hive", "wd", "--scope", "host"], env=ENV
    )
    assert r.exit_code == 1


def test_get_without_hive_is_unchanged(bh_home):
    r = CliRunner().invoke(app, ["config", "get", "backfill.collate.areas"], env=ENV)
    assert r.exit_code == 0
    assert json.loads(r.stdout) == {"core": "engine"}
    assert "layer" not in r.output


def test_a_hive_overlay_turns_close_on_evidence_off_for_that_hive_only():
    """bh-1vkff.3: the timeline settings resolve through the overlay, as the history run reads
    them (``effective_cfg`` then ``CollateOptions.from_config``)."""
    from beadhive import backfill_history

    block = {
        **HOST_BLOCK,
        "hives": {"wd": {"collate": {"status": {"close_on_evidence": False}}}},
    }
    cfg = _cfg(block)

    def status(entry):
        eff = effective_cfg(cfg, entry)
        return backfill_history.CollateOptions.from_config(
            BackfillConfig.model_validate(eff["backfill"])
        ).status

    assert status(WIDGETS).close_on_evidence is False
    assert status(GADGETS).close_on_evidence is True  # another hive keeps the host default
    assert status(WIDGETS).in_progress_on_evidence is True  # only the overlaid key moved
