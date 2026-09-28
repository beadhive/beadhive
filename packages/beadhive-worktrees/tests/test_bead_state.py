"""Bead-state resolution policy behind the ``BeadStateLookup`` port (bh-167s0, bh-qdezo.5).

Re-homed from root ``tests/test_worktree.py`` (the ``_store_readable`` message table) and
``tests/test_worktree_inventory_boundaries.py`` (the disposition edge-direction rejection) by
bh-qdezo.9: the policy lives here, so it is proven on an in-memory store with the package alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive_worktrees import (
    bead_states,
    disposition_relations,
    dispositions_needing_evidence,
    format_disposition,
    store_reason,
)
from beadhive_worktrees.testing import InMemoryBeadStateLookup

MAIN = Path("/repo")


def test_an_unreadable_store_is_named_as_the_store_not_the_data() -> None:
    """None from the probe means bd exited non-zero / returned no JSON — naming it is what stops
    a reader concluding the hive is simply empty."""
    assert "could not be READ" in store_reason(InMemoryBeadStateLookup(readable=False), MAIN)


def test_a_store_that_answers_with_nothing_is_named() -> None:
    """The measured signature of bd's schema-fork guard: zero issues and exit 0."""
    assert "ZERO issues" in store_reason(InMemoryBeadStateLookup(), MAIN)


def test_a_store_that_answers_is_silent() -> None:
    assert store_reason(InMemoryBeadStateLookup({"x": {"status": "open"}}), MAIN) == ""


def test_a_readable_store_missing_one_bead_names_the_retired_prefix_case() -> None:
    lookup = InMemoryBeadStateLookup({"ag-run-4gk.1": {"status": "closed"}})

    statuses, close_reasons, unknown, reason = bead_states(
        lookup, MAIN, ["ag-rt-4gk.1", "ag-run-4gk.1"], ""
    )

    assert statuses == {"ag-rt-4gk.1": "", "ag-run-4gk.1": "closed"}
    assert close_reasons == {"ag-rt-4gk.1": "", "ag-run-4gk.1": ""}
    assert list(unknown) == ["ag-rt-4gk.1"]
    assert "no longer exists" in unknown["ag-rt-4gk.1"]
    assert reason == ""


def test_an_unreadable_store_reports_one_hive_reason_not_one_per_bead() -> None:
    lookup = InMemoryBeadStateLookup(readable=False)
    hive_reason = store_reason(lookup, MAIN)

    statuses, _close, unknown, reason = bead_states(lookup, MAIN, ["a", "b"], hive_reason)

    assert statuses == {"a": "", "b": ""}
    assert unknown == {}
    assert reason == hive_reason


def test_bead_ids_are_read_once_each() -> None:
    lookup = InMemoryBeadStateLookup({"a": {"status": "open"}})

    bead_states(lookup, MAIN, ["a", "a"], "")

    assert lookup.shows == [("a", MAIN)]


def _reasons() -> dict[str, str]:
    return {
        "old-retained": format_disposition("retained", "pivot", "consumer"),
        "old-superseded": format_disposition("superseded", "superseded", "replacement"),
        "plain": "merged",
    }


def test_only_retained_and_superseded_records_need_evidence() -> None:
    assert set(dispositions_needing_evidence(_reasons())) == {"old-retained", "old-superseded"}
    assert dispositions_needing_evidence({"a": "merged"}) == {}


@pytest.mark.parametrize(
    ("retained_type", "superseded_type"),
    [("supersedes", "relates-to"), ("blocks", "blocks")],
)
def test_reversed_or_wrong_typed_edges_confirm_nothing(retained_type, superseded_type) -> None:
    lookup = InMemoryBeadStateLookup(
        {
            "old-retained": {
                "dependencies": [{"depends_on_id": "consumer", "type": retained_type}]
            },
            "replacement": {
                "dependencies": [{"depends_on_id": "old-superseded", "type": superseded_type}]
            },
        }
    )

    assert disposition_relations(lookup, MAIN, dispositions_needing_evidence(_reasons())) == {}
