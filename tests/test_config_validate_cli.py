"""`bh config validate` (cli.config_validate) — the user-facing entry point over the schema
validator (bh-5cgm.5).

Covers the acceptance: exit 0 on a clean current config, nonzero + printed problems on a
stale/ws-era config, and clear `config init` guidance (no traceback) when no config exists.

Note: the CLI root callback auto-migrates the two rig→hive keys (bh-41rh) before any
subcommand runs, so a config carrying ONLY those keys self-heals; the durable ws-era signal
`validate` surfaces is a missing `schema_version`. The rename-table rendering is exercised
with that auto-migration neutralised so the renamed keys reach the validator.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config
from beadhive.cli import app

runner = CliRunner()


@pytest.fixture
def cfg_at(tmp_path, monkeypatch):
    """Point `config.load()` at a temp config.yaml via $BH_CONFIG; return a writer for it."""
    p = tmp_path / "config.yaml"
    monkeypatch.setenv("BH_CONFIG", str(p))

    def _write(text: str) -> Path:
        p.write_text(text)
        return p

    return _write


def test_clean_current_config_exits_zero(cfg_at):
    dst = cfg_at("")
    shutil.copy(config.template("config.example.yaml"), dst)
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_stale_ws_era_config_exits_nonzero_and_prints_problems(cfg_at):
    # a ported ws-era config predates schema versioning (no schema_version) and points at the
    # old home — the durable staleness the validator gates on.
    cfg_at("providers:\n  - github\nworktrees:\n  path: ~/.ws/wt\n")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1, result.output
    assert "schema_version" in result.output


def test_renamed_keys_render_the_ws_to_bh_table(cfg_at, monkeypatch):
    # neutralise the unrelated bh-41rh auto-migration so the rig keys reach `validate`.
    monkeypatch.setattr(config, "migrate_hive_keys_if_needed", lambda: None)
    cfg_at("schema_version: 1\notel:\n  rig: my-hive\ngit_workspace:\n  rig_match: prefix\n")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1, result.output
    assert "otel.hive" in result.output
    assert "hive_match" in result.output
    assert "ws → bh renames" in result.output


def test_unknown_key_typo_surfaces_a_did_you_mean(cfg_at):
    # a typo of a known key (`providers`) is a gating error that now carries a did-you-mean
    # suggestion via config_schema.suggest_key (the helper .4 added).
    cfg_at("schema_version: 1\nprovidrs:\n  - github\n")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1, result.output
    assert "providrs" in result.output
    assert "did you mean `providers`" in result.output


def test_missing_config_gives_guidance_not_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "config init" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
