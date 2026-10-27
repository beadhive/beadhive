"""``beadhive.state`` is the facade over the intake vocabulary (external-intake-module-adr.md
section 3): every moved name is the module's own object, ``STATE_DIMENSIONS`` is built from
``INTAKE_DIMENSIONS`` plus the entries state.py keeps, and nothing the facade promised is lost."""

from __future__ import annotations

import pytest

from beadhive import state
from beadhive.modules import intake
from beadhive.modules.intake.domain import vocabulary

_MOVED = (
    "DISPOSITION_STATE",
    "INTAKE_PROMOTED",
    "INTAKE_UNTRIAGED",
    "ORIGIN_BACKFILL",
    "ORIGIN_DIM",
    "ORIGIN_ESCALATION",
    "ORIGIN_FACTORY_SEED",
    "ORIGIN_GITHUB",
    "ORIGIN_IMPORT",
    "ORIGIN_REPORT",
    "channel_of",
    "disposition_state",
    "is_escalation_origin",
    "is_promoted",
    "is_report_origin",
    "is_untriaged_intake",
    "origin_from_source_system",
    "origin_of",
)
_KEPT = (
    "DISPATCH_CAUSES",
    "DISPATCH_DIM",
    "ESCALATION_DIM",
    "OUTBOUND_PENDING",
    "PUBLISH_APPROVED",
    "STATE_DIMENSIONS",
    "dispatch_cause_of",
    "is_escalation_raised",
    "is_escalation_resolved",
    "is_outbound_candidate",
)


@pytest.mark.parametrize("name", _MOVED)
def test_a_moved_name_is_the_modules_own_object(name: str) -> None:
    assert getattr(state, name) is getattr(vocabulary, name)
    assert getattr(state, name) is getattr(intake, name)


@pytest.mark.parametrize("name", _KEPT)
def test_the_contributor_and_dispatch_vocabularies_stay_in_state(name: str) -> None:
    assert hasattr(state, name)


def test_state_dimensions_are_intake_dimensions_plus_the_entries_state_keeps() -> None:
    for dimension, values in intake.INTAKE_DIMENSIONS.items():
        assert state.STATE_DIMENSIONS[dimension] is values
    assert list(state.STATE_DIMENSIONS) == [
        "intake",
        "outbound",
        "publish",
        "origin",
        "escalation",
        "dispatch",
    ]
    assert state.STATE_DIMENSIONS["outbound"] == {"pending"}
    assert state.STATE_DIMENSIONS["publish"] == {"approved"}
    assert state.STATE_DIMENSIONS["escalation"] == {"raised", "resolved"}
    assert state.STATE_DIMENSIONS["dispatch"] == state.DISPATCH_CAUSES


def test_state_does_not_reach_into_the_modules_internal_layers() -> None:
    source = state.__file__
    text = open(source, encoding="utf-8").read()  # noqa: SIM115 - one tiny read
    assert "modules.intake.domain" not in text and "modules.intake.application" not in text
