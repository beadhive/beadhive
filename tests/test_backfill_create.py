"""Create mode of ``bh backfill history``: the labels, the prefix and the bh-l1m3p rule.

``create_mode_options`` / ``create_mode_prefix`` supply what the neutral projector needs from the
hive. The regression test folds a created bead's labels through ``adopt`` and proves it is *work*:
``origin:backfill`` with no ``intake:`` value is provenance, never an adopted report.
"""

from __future__ import annotations

import pytest

import beadlog
from beadhive import adopt
from beadhive.backfill_create import (
    ORIGIN_LABEL,
    create_mode_options,
    create_mode_prefix,
    identity_labels,
    parse_create,
)
from beadhive_backfill import BackfillError, HistoryPass
from beadlog_sources import CachedLedgerDirectory, IdentifyOptions, ProjectOptions
from beadlog_sources.testing import evidence as ev

ENTRY = {"provider": "github", "org": "beadhive", "repo": "fixture", "prefix": "fx"}
CFG = {"managed_repos": [ENTRY]}
GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000ff"


def test_off_is_the_default_and_adds_no_labels() -> None:
    assert parse_create(None) == "off"
    assert create_mode_options("fixture", CFG, "off") == ProjectOptions()
    assert create_mode_options(ENTRY, CFG, None).creation_labels == ()


def test_propose_labels_are_origin_backfill_and_the_identity_triplet() -> None:
    options = create_mode_options(ENTRY, CFG, "propose")
    assert options.creation_labels == (
        ORIGIN_LABEL,
        "provider:github",
        "org:beadhive",
        "repo:fixture",
    )
    assert options.creation_labels == (
        "origin:backfill",
        *identity_labels(ENTRY),
    )


def test_an_unknown_create_value_is_refused() -> None:
    with pytest.raises(BackfillError, match="create"):
        parse_create("always")


def test_the_prefix_is_the_registered_one_and_propose_refuses_none() -> None:
    assert create_mode_prefix(ENTRY, CFG, "propose") == "fx"
    assert create_mode_prefix("fixture", CFG, "off") == "fx"
    bare = {"provider": "github", "org": "beadhive", "repo": "bare"}
    assert create_mode_prefix(bare, {"managed_repos": [bare]}, "off") is None
    with pytest.raises(BackfillError, match="prefix"):
        create_mode_prefix(bare, {"managed_repos": [bare]}, "propose")


def test_propose_needs_the_identity_triplet() -> None:
    with pytest.raises(BackfillError, match="identity"):
        create_mode_options({"prefix": "fx"}, CFG, "propose")


def _created_beads() -> dict[str, dict]:
    when = "2026-10-01T10:00:00Z"
    corpus = ev.corpus(
        ev.change(
            f"{1:040x}", occurred_at=when, committed_at=when, cc_type="feat", paths=["api/a.py"]
        )
    )
    pass_ = HistoryPass(
        corpus,
        CachedLedgerDirectory({}),
        local="fx",
        group=GROUP,
        prefix=create_mode_prefix(ENTRY, CFG, "propose"),
        create=True,
        project_options=create_mode_options(ENTRY, CFG, "propose"),
        identify_options=IdentifyOptions(new_threshold=100),
    )
    events = pass_.propose((), extracted_at="2026-11-12T00:00:00Z", judge=None)  # type: ignore[arg-type]
    return {
        ref.id: {"id": ref.id, "issue_type": s.type, "status": s.phase, "labels": sorted(s.labels)}
        for ref, s in beadlog.fold(events).items()
    }


def test_created_beads_are_work_under_the_l1m3p_rule_not_adopted_reports() -> None:
    beads = _created_beads()
    assert beads
    for bead in beads.values():
        assert ORIGIN_LABEL in bead["labels"]
        assert not any(label.startswith("intake:") for label in bead["labels"])
        assert adopt.report_role(bead) is None
        assert adopt.is_report_child(bead) is False
