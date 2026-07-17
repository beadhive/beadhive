"""Stale config → paste-ready agentic-update offer (config_validate offer helpers + the
`bh config validate --fix` / inline-offer CLI wiring, bh-5cgm.7).

Covers: an offer is produced for a stale/ws-era config (referencing schema v1 and the
specific deltas) and absent for a clean config; `--fix` prints just the prompt.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config
from beadhive.cli import app
from beadhive.config_schema import SCHEMA_VERSION
from beadhive.config_validate import agentic_update_prompt, is_stale, stale_deltas

runner = CliRunner()


# ---- offer helpers -----------------------------------------------------------


def test_clean_config_is_not_stale_and_offers_nothing():
    clean = {"schema_version": SCHEMA_VERSION, "otel": {"hive": "h"}}
    assert is_stale(clean) is False
    assert stale_deltas(clean) == []
    assert agentic_update_prompt(clean) is None


def test_ws_era_config_is_stale_with_specific_deltas():
    ws_era = {"otel": {"rig": "h"}, "worktrees": {"path": "~/.ws/wt"}}
    assert is_stale(ws_era) is True
    deltas = "\n".join(stale_deltas(ws_era))
    assert "schema_version" in deltas  # missing → add
    assert "otel.rig" in deltas and "otel.hive" in deltas  # renamed key
    assert "~/.ws" in deltas and "~/.beadhive" in deltas  # old home path


def test_offer_prompt_references_schema_v1_and_deltas():
    prompt = agentic_update_prompt({"otel": {"rig": "h"}})
    assert prompt is not None
    assert f"schema version {SCHEMA_VERSION}" in prompt
    assert "otel.rig" in prompt and "otel.hive" in prompt
    assert "WS_" in prompt and "BH_" in prompt  # env-prefix guidance


def test_older_schema_version_offers_a_bump():
    prompt = agentic_update_prompt({"schema_version": SCHEMA_VERSION - 1})
    assert prompt is not None
    assert "bump" in prompt and "schema_version" in prompt


# ---- CLI: inline offer + --fix ----------------------------------------------


@pytest.fixture
def cfg_at(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    monkeypatch.setenv("BH_CONFIG", str(p))

    def _write(text: str) -> Path:
        p.write_text(text)
        return p

    return _write


def test_validate_appends_offer_on_stale_config(cfg_at):
    cfg_at("providers:\n  - github\n")  # no schema_version → stale
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1, result.output
    assert "paste this to a coding agent" in result.output
    assert f"schema version {SCHEMA_VERSION}" in result.output


def test_validate_prints_no_offer_on_clean_config(cfg_at):
    dst = cfg_at("")
    shutil.copy(config.template("config.example.yaml"), dst)
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "paste this to a coding agent" not in result.output


def test_fix_prints_just_the_prompt(cfg_at):
    cfg_at("providers:\n  - github\n")  # stale
    result = runner.invoke(app, ["config", "validate", "--fix"])
    assert result.exit_code == 0, result.output
    assert f"schema version {SCHEMA_VERSION}" in result.output
    assert "add `schema_version" in result.output
    # --fix is prompt-only: none of the validate problem markers
    assert "✗" not in result.output


def test_fix_on_clean_config_says_nothing_to_fix(cfg_at):
    dst = cfg_at("")
    shutil.copy(config.template("config.example.yaml"), dst)
    result = runner.invoke(app, ["config", "validate", "--fix"])
    assert result.exit_code == 0, result.output
    assert "nothing to fix" in result.output
