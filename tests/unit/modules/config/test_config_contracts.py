"""Isolated contract tests for the configuration module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beadhive.modules.config.application import partition
from beadhive.modules.config.contracts import (
    CONFIG_SECTION_COMPATIBILITY_ALIASES,
    DEFAULT_JUNK_GLOBS,
    DEFAULT_PRECIOUS_GLOBS,
    DEFAULT_PRECIOUS_MIN_BYTES,
    SCHEMA_VERSION,
    BeadhiveConfig,
    field_default,
    iter_schema_fields,
    known_sections,
)


def test_canonical_models_own_version_defaults_metadata_and_aliases():
    assert SCHEMA_VERSION == 1
    assert BeadhiveConfig().schema_version == SCHEMA_VERSION
    assert field_default("work.max_commits") == 10
    by_path = {field.path: field for field in iter_schema_fields()}
    assert by_path["work.max_commits"].description

    work = BeadhiveConfig().work
    assert work.precious_globs == list(DEFAULT_PRECIOUS_GLOBS)
    assert work.junk_globs == list(DEFAULT_JUNK_GLOBS)
    assert work.precious_min_bytes == DEFAULT_PRECIOUS_MIN_BYTES

    config = BeadhiveConfig(work={"validate": {"submit": "just focused"}})
    assert config.work.validate_overrides == {"submit": "just focused"}


def test_top_level_section_inventory_is_derived_with_explicit_compatibility_aliases():
    sections = known_sections()

    assert set(BeadhiveConfig.model_fields) <= sections
    assert {"git_workspace", "orca", "hitch"} <= sections
    assert CONFIG_SECTION_COMPATIBILITY_ALIASES == frozenset({"beads"})
    assert "beads" in sections
    assert "definitely_not_config" not in sections


def test_backfill_judge_section_is_additive_and_every_default_is_off():
    assert BeadhiveConfig().backfill is None  # absent by default: dumps change for nobody
    judge = BeadhiveConfig(backfill={}).backfill.judge
    assert judge.enabled is False
    assert judge.command == "bh-judge-haiku"
    assert judge.timeout_seconds == 30.0
    assert judge.model == "claude-haiku-5-5"
    assert judge.auto_accept == {}
    assert judge.max_calls_per_run == 100
    assert field_default("backfill.judge.enabled") is False
    assert "backfill" in known_sections()
    paths = [f.path for f in iter_schema_fields() if f.path.startswith("backfill.")]
    assert "backfill.judge.enabled" in paths
    assert {partition.partition_of(path) for path in paths} == {partition.HOST}

    tuned = BeadhiveConfig(backfill={"judge": {"enabled": True, "auto_accept": {"m": 0.9}}})
    assert tuned.backfill.judge.enabled is True
    assert tuned.backfill.judge.auto_accept == {"m": 0.9}


@pytest.mark.parametrize(
    "judge",
    [
        {"auto_accept": {"m": 1.5}},
        {"timeout_seconds": 0},
        {"max_calls_per_run": -1},
        {"command": ""},
        {"unknown": True},
    ],
)
def test_backfill_judge_section_refuses_invalid_values(judge):
    with pytest.raises(ValidationError):
        BeadhiveConfig(backfill={"judge": judge})


def test_backfill_timeline_gapfill_is_additive_and_off_by_default():
    assert BeadhiveConfig().backfill is None  # still absent unless configured
    gap = BeadhiveConfig(backfill={}).backfill.timeline.gapfill
    assert (gap.enabled, gap.batch_size, gap.timeout_seconds) == (False, 25, 180.0)
    assert field_default("backfill.timeline.gapfill.enabled") is False
    assert field_default("backfill.timeline.gapfill.batch_size") == 25
    paths = {f.path for f in iter_schema_fields() if f.path.startswith("backfill.timeline.")}
    assert paths >= {
        "backfill.timeline.gapfill.enabled",
        "backfill.timeline.gapfill.batch_size",
        "backfill.timeline.gapfill.timeout_seconds",
    }
    assert {partition.partition_of(path) for path in paths} == {partition.HOST}

    tuned = BeadhiveConfig(
        backfill={
            "timeline": {"gapfill": {"enabled": True, "batch_size": 5, "timeout_seconds": 60}}
        }
    )
    assert tuned.backfill.timeline.gapfill.enabled is True
    assert tuned.backfill.timeline.gapfill.batch_size == 5
    assert tuned.backfill.judge.enabled is False  # the sibling section is untouched


@pytest.mark.parametrize(
    "gapfill",
    [{"batch_size": 0}, {"timeout_seconds": 0}, {"timeout_seconds": -1}, {"unknown": True}],
)
def test_backfill_timeline_gapfill_refuses_invalid_values(gapfill):
    with pytest.raises(ValidationError):
        BeadhiveConfig(backfill={"timeline": {"gapfill": gapfill}})


def test_backfill_coverage_is_additive_and_off_by_default():
    cov = BeadhiveConfig(backfill={}).backfill.coverage
    assert cov.enabled is False
    assert cov.code_types == ["bug", "chore", "feature", "task"]
    assert cov.doc_roots == ["docs"] and cov.example_limit == 5
    assert field_default("backfill.coverage.enabled") is False
    paths = {f.path for f in iter_schema_fields() if f.path.startswith("backfill.coverage.")}
    assert paths >= {
        "backfill.coverage.enabled",
        "backfill.coverage.code_types",
        "backfill.coverage.doc_roots",
        "backfill.coverage.example_limit",
    }
    assert {partition.partition_of(path) for path in paths} == {partition.HOST}

    tuned = BeadhiveConfig(backfill={"coverage": {"enabled": True, "doc_roots": ["docs", "adr"]}})
    assert tuned.backfill.coverage.enabled is True
    assert tuned.backfill.coverage.doc_roots == ["docs", "adr"]
    assert tuned.backfill.judge.enabled is False  # the sibling section is untouched


@pytest.mark.parametrize(
    "coverage",
    [{"doc_roots": []}, {"example_limit": -1}, {"enabled": "maybe"}, {"unknown": True}],
)
def test_backfill_coverage_refuses_invalid_values(coverage):
    with pytest.raises(ValidationError):
        BeadhiveConfig(backfill={"coverage": coverage})
