"""The intake and origin vocabulary owned by the module (moved from ``beadhive.state``)."""

from __future__ import annotations

import pytest

from beadhive.modules import intake
from beadhive.modules.intake.domain import vocabulary as vocab


def test_intake_dimensions_are_the_closed_sets() -> None:
    assert vocab.INTAKE_DIMENSIONS["intake"] == {
        "untriaged",
        "accepted",
        "rejected",
        "rerouted",
        "promoted",
    }
    assert vocab.INTAKE_DIMENSIONS["origin"] == {
        "report",
        "github",
        "import",
        "escalation",
        "factory-seed",
        "backfill",
    }
    assert set(vocab.INTAKE_DIMENSIONS) == {"intake", "origin"}


def test_label_constants_agree_with_the_dimensions() -> None:
    for label in (
        vocab.INTAKE_UNTRIAGED,
        vocab.INTAKE_PROMOTED,
        vocab.ORIGIN_REPORT,
        vocab.ORIGIN_GITHUB,
        vocab.ORIGIN_IMPORT,
        vocab.ORIGIN_ESCALATION,
        vocab.ORIGIN_FACTORY_SEED,
        vocab.ORIGIN_BACKFILL,
    ):
        dimension, _, value = label.partition(":")
        assert value in vocab.INTAKE_DIMENSIONS[dimension], label
    assert vocab.ORIGIN_DIM == "origin" and vocab.INTAKE_DIM == "intake"


def test_every_disposition_maps_to_a_registered_terminal_value() -> None:
    assert set(vocab.DISPOSITION_STATE) == {d.value for d in intake.Disposition}
    for disposition, state in vocab.DISPOSITION_STATE.items():
        assert vocab.disposition_state(disposition) == state
        assert state in vocab.INTAKE_DIMENSIONS["intake"] and state != "untriaged"
    assert vocab.disposition_state("bogus") is None


def test_queue_predicates() -> None:
    assert vocab.is_untriaged_intake(["x", "intake:untriaged"])
    assert not vocab.is_untriaged_intake(["intake:accepted"])
    assert not vocab.is_untriaged_intake(None)
    assert vocab.is_promoted(["intake:promoted"]) and not vocab.is_promoted([])
    assert vocab.is_report_origin(["origin:report"]) and not vocab.is_report_origin(None)
    assert vocab.is_escalation_origin(["origin:escalation"])


def test_origin_of_reads_only_registered_values() -> None:
    assert vocab.origin_of(["a", "origin:github"]) == "github"
    assert vocab.origin_of(["origin:bogus"]) is None
    assert vocab.origin_of(None) is None


@pytest.mark.parametrize(
    ("source_system", "expected"),
    [
        ("github", "github"),
        (" GitHub ", "github"),
        ("import", "import"),
        ("jira", None),
        ("", None),
    ],
)
def test_origin_from_source_system_maps_known_channels_on_read(source_system, expected) -> None:
    assert vocab.origin_from_source_system(source_system) == expected
    assert vocab.origin_from_source_system(None) is None


def test_channel_of_prefers_the_label_then_the_source_system() -> None:
    assert vocab.channel_of(["origin:report"], "github") == "report"
    assert vocab.channel_of([], "github") == "github"
    assert vocab.channel_of([], "jira") is None
    assert vocab.channel_of(None) is None


def test_tracks_edge_is_the_reserved_link_type() -> None:
    assert vocab.TRACKS_EDGE == "tracks"
