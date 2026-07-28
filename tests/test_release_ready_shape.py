"""`beadhive.release.ready_beads` — normalizing the two shapes `bd ready --json` returns.

`bd ready --json` emits a flat ARRAY of beads; `bd ready --gated --json` emits an ENVELOPE
(`{count, molecules[], schema_version}`) whose beads are each molecule's `ready_step`. `bh release
order` reads the gated form, so treating the envelope as the array iterated its string keys and
crashed with `AttributeError: 'str' object has no attribute 'get'` on any hive with gated-ready
work. These cases pin both shapes plus the degrade-to-empty path. AAA structure.
"""

from __future__ import annotations

from beadhive import release


def _bead(bead_id, *labels):
    return {"id": bead_id, "labels": list(labels)}


def test_flat_array_passes_through():
    # Arrange: the non-gated shape.
    payload = [_bead("a", "release:fix"), _bead("b", "release:feature")]

    # Act / Assert
    assert release.ready_beads(payload) == payload


def test_gated_envelope_yields_the_ready_steps():
    # Arrange: the --gated shape — beads live at molecules[].ready_step, not at the top level.
    payload = {
        "count": 2,
        "schema_version": 1,
        "molecules": [
            {"molecule_id": "m1", "ready_step": _bead("a", "release:fix")},
            {"molecule_id": "m2", "ready_step": _bead("b", "release:breaking")},
        ],
    }

    # Act
    beads = release.ready_beads(payload)

    # Assert: real bead dicts, in molecule order — this is the case that used to raise.
    assert [b["id"] for b in beads] == ["a", "b"]
    assert beads[0]["labels"] == ["release:fix"]


def test_unrecognized_payloads_degrade_to_empty():
    # Arrange/Act/Assert: None (bd.json's failure contract), a molecule with no ready_step, and a
    # scalar all yield [] rather than raising — a read miss must not break the verb.
    assert release.ready_beads(None) == []
    assert release.ready_beads({"molecules": [{"molecule_id": "m1"}]}) == []
    assert release.ready_beads("nope") == []


if __name__ == "__main__":  # pragma: no cover - manual run
    test_flat_array_passes_through()
    test_gated_envelope_yields_the_ready_steps()
    test_unrecognized_payloads_degrade_to_empty()
    print("ok")
