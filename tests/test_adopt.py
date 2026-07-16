"""Unit tests for `ws.adopt` — the pure frame-seeding + provenance helpers behind `ws plan adopt`
(bead). These exercise the bd-free shaping logic directly; the bd reads/writes
(the `plan.run` seam) and the file-time report↔epic linking are covered in test_plan.py.
"""

from __future__ import annotations

import pytest

from beadhive import adopt, state


def _report(bead_id, title, description="", *, labels=None, source_system="", external_ref=""):
    return {
        "id": bead_id,
        "title": title,
        "description": description,
        "labels": labels if labels is not None else [state.INTAKE_PROMOTED],
        "source_system": source_system,
        "external_ref": external_ref,
    }


# ---- frame_from_beads -------------------------------------------------------


def test_frame_from_beads_seeds_title_description_and_adopts():
    """The frame seeds the epic title/description from the report text and records the origin id."""
    frame = adopt.frame_from_beads([_report("rep-1", "login broken", "cannot log in at all")])
    epic = frame["epic"]
    assert "login broken" in epic["title"]
    assert "rep-1" in epic["description"] and "cannot log in at all" in epic["description"]
    assert epic["adopts"] == ["rep-1"]
    assert frame["issues"] == []  # a frame is a stub the planner decomposes before filing


def test_frame_from_beads_folds_multiple_reports():
    """Several reports fold into one frame: all ids under `adopts`, the count noted in the title."""
    frame = adopt.frame_from_beads([_report("rep-1", "first ask"), _report("rep-2", "second ask")])
    assert frame["epic"]["adopts"] == ["rep-1", "rep-2"]
    assert "(+1 more)" in frame["epic"]["title"]


def test_frame_from_beads_carries_first_native_provenance():
    """System-of-record provenance survives: the FIRST report carrying source_system/external_ref
    wins, so a GitHub-sourced request stays traceable to gh-<n> on the epic."""
    frame = adopt.frame_from_beads(
        [
            _report("rep-1", "born-native cross-hive report"),  # no source_system
            _report("rep-2", "github import", source_system="github", external_ref="gh-9"),
        ]
    )
    assert frame["epic"]["source_system"] == "github"
    assert frame["epic"]["external_ref"] == "gh-9"


def test_frame_from_beads_omits_provenance_when_born_native():
    """A born-native cross-rig report (no source_system/external_ref) leaves the epic's native
    provenance UNSET — it must not overload source_system (see ws/state.py)."""
    frame = adopt.frame_from_beads([_report("rep-1", "cross-hive report")])
    assert "source_system" not in frame["epic"]
    assert "external_ref" not in frame["epic"]


def test_frame_from_beads_empty_raises():
    with pytest.raises(adopt.AdoptError):
        adopt.frame_from_beads([])


# ---- file-time accessors ----------------------------------------------------


def test_adopts_of_and_provenance_of():
    epic = {"adopts": ["rep-1", "rep-2"], "source_system": "github", "external_ref": "gh-9"}
    assert adopt.adopts_of(epic) == ["rep-1", "rep-2"]
    assert adopt.provenance_of(epic) == ("github", "gh-9")
    assert adopt.adopts_of({}) == []
    assert adopt.provenance_of({}) == ("", "")


def test_epic_import_record_carries_provenance_and_labels():
    """The import record births the epic with type=epic + native provenance + the triplet labels
    (source_system is settable only at bead birth, so an adopted epic is imported, not created)."""
    epic = {
        "title": "Adopt: login broken",
        "description": "why",
        "design": "arch",
        "source_system": "github",
        "external_ref": "gh-9",
    }
    record = adopt.epic_import_record(epic, ["provider:github", "org:o", "repo:r"])
    assert record["issue_type"] == "epic"
    assert record["title"] == "Adopt: login broken"
    assert record["source_system"] == "github" and record["external_ref"] == "gh-9"
    assert record["labels"] == ["provider:github", "org:o", "repo:r"]
    assert record["description"] == "why" and record["design"] == "arch"


# ---- is_origin_report -------------------------------------------------------


def test_is_origin_report_true_for_promoted_or_origin_channel():
    """An adopted origin report is identified by intake:promoted OR an origin: channel label — so
    it is held out of the molecule's work-sibling set."""
    assert adopt.is_origin_report([state.INTAKE_PROMOTED])
    assert adopt.is_origin_report([state.ORIGIN_GITHUB])
    assert adopt.is_origin_report([state.INTAKE_PROMOTED, "provider:github"])


def test_is_origin_report_false_for_plain_work_sibling():
    """A normal work issue (no intake/origin labels) is NOT an origin report — stays a sibling."""
    assert not adopt.is_origin_report(["provider:github", "org:o", "repo:r", "model:sonnet"])
    assert not adopt.is_origin_report([])
    assert not adopt.is_origin_report(None)
