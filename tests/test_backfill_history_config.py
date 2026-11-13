"""The history pipeline's config models, plugin reader discovery and the unchanged old verbs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from beadhive import backfill_cli, backfill_history, cli
from beadhive.modules.config.contracts import BackfillConfig, BeadhiveConfig
from beadlog_sources import (
    BUILTIN_READERS,
    AttachOptions,
    ClusterOptions,
    IdentifyOptions,
    StatusOptions,
)

ENV = {"COLUMNS": "200", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}


def test_defaults_are_inert_and_equal_the_library_defaults() -> None:
    cfg = BackfillConfig()
    assert cfg.collate.create == "off"
    assert cfg.sources.github.enabled is False
    assert cfg.sources.docs.formats == ["markdown"]
    assert cfg.history.run_dir == "" and cfg.history.ledgers == []
    assert cfg.history.sources == ["git", "docs", "planning"]
    assert cfg.judge.resume is True and cfg.judge.cost_per_call_usd is None
    options = backfill_history.CollateOptions.from_config(cfg)
    assert options.cluster == ClusterOptions()
    assert options.attach == AttachOptions()
    assert options.identify == IdentifyOptions()
    assert options.status == StatusOptions()


def test_doc_default_is_from_doc_and_reaches_the_status_options() -> None:
    """bh-f4t6r: the default flips to from-doc; ``closed`` is the opt-out to 0.36 output."""
    cfg = BackfillConfig()
    assert cfg.collate.status.doc_default == "from-doc"
    assert cfg.collate.status.doc_open == ["draft", "proposed"]
    assert backfill_history.CollateOptions.from_config(cfg).status.doc_default == "from-doc"
    closed = BackfillConfig.model_validate({"collate": {"status": {"doc_default": "closed"}}})
    assert backfill_history.CollateOptions.from_config(closed).status.doc_default == "closed"
    with pytest.raises(ValidationError):
        BackfillConfig.model_validate({"collate": {"status": {"doc_default": "maybe"}}})
    with pytest.raises(ValidationError):
        BackfillConfig.model_validate({"collate": {"status": {"doc_open": ["wip"]}}})


def test_the_section_is_additive_to_the_root_config() -> None:
    assert BeadhiveConfig().backfill is None
    assert BeadhiveConfig(backfill={}).backfill.history.run_dir == ""


@pytest.mark.parametrize(
    "bad",
    [
        {"collate": {"create": "always"}},
        {"collate": {"rules": ["nope"]}},
        {"collate": {"attach": {"lexical_percent": 0}}},
        {"collate": {"identify": {"new_threshold": 101}}},
        {"collate": {"status": {"rules": ["nope"]}}},
        {"sources": {"docs": {"formats": ["pdf"]}}},
        {"sources": {"docs": {"formats": []}}},
        {"sources": {"github": {"enable": True}}},
        {"history": {"sources": ["svn"]}},
        {"history": {"nope": 1}},
    ],
)
def test_validation_refuses_bad_values(bad) -> None:
    with pytest.raises(ValidationError):
        BackfillConfig.model_validate(bad)


def test_github_reader_is_discoverable_only_with_the_plugin_enabled() -> None:
    assert "github" not in BUILTIN_READERS  # core never depends on the plugin
    assert backfill_history.plugin_reader_table({}) == {}
    on = {"plugins": {"tracker-github": {"enabled": True}}}
    table = backfill_history.plugin_reader_table(on)
    assert table == {"github": "beadhive_tracker_github.reader:GitHubReader"}
    found = backfill_history.discover(on)
    assert "github" in found
    assert found["github"].load().__name__ == "GitHubReader"


def test_a_plugin_switched_off_by_the_kernel_is_not_discoverable() -> None:
    cfg = {
        "plugins": {"tracker-github": {"enabled": True}},
        "plugin_kernel": {"enabled": {"tracker-github": False}},
    }
    assert backfill_history.plugin_reader_table(cfg) == {}


def test_github_needs_both_switches(monkeypatch) -> None:
    # An installed bh has no beadlog.readers entry point for the vendored plugin: model it.
    real = backfill_history.discover_readers
    monkeypatch.setattr(
        backfill_history,
        "discover_readers",
        lambda builtin=None, entry_points=None: real(builtin=builtin, entry_points=lambda: ()),
    )
    on = {"plugins": {"tracker-github": {"enabled": True}}}
    gated = BackfillConfig()
    with pytest.raises(Exception, match="backfill.sources.github.enabled"):
        backfill_history.resolve_sources(["github"], gated, on)
    allowed = BackfillConfig.model_validate({"sources": {"github": {"enabled": True}}})
    assert [n for n, _ in backfill_history.resolve_sources(["github"], allowed, on)] == ["github"]
    with pytest.raises(Exception, match="tracker-github"):
        backfill_history.resolve_sources(["github"], allowed, {})


def test_existing_source_verbs_keep_their_options() -> None:
    for name in ("commits", "docs", "planning"):
        text = CliRunner().invoke(cli.app, ["backfill", name, "--help"], env=ENV).stdout
        for flag in ("--apply", "--dry-run", "--plan", "--pre-state", "--audit", "--option"):
            assert flag in text, (name, flag)
    assert backfill_cli.SOURCE_PASS_NAMES == ("commits", "docs", "planning")


def test_timeline_settings_default_on_and_reach_the_status_options() -> None:
    """bh-1vkff.3: a re-run closes and starts backfilled beads on evidence unless told not to."""
    cfg = BackfillConfig()
    assert cfg.collate.status.close_on_evidence is True
    assert cfg.collate.status.in_progress_on_evidence is True
    status = backfill_history.CollateOptions.from_config(cfg).status
    assert status.close_on_evidence and status.in_progress_on_evidence
    off = BackfillConfig.model_validate(
        {"collate": {"status": {"close_on_evidence": False, "in_progress_on_evidence": False}}}
    )
    status = backfill_history.CollateOptions.from_config(off).status
    assert not status.close_on_evidence and not status.in_progress_on_evidence
    with pytest.raises(ValidationError):
        BackfillConfig.model_validate({"collate": {"status": {"close_on_evidence": "sometimes"}}})
