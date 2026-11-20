"""Replay of hq-service ``conformance/release-eligibility/v1`` against Python's reference.

Python is the reference here: the corpus's own ``source`` is ``beadhive
src/beadhive/release_policy.py admits``. Each case is the HQ's tables (the signed authority
state and the ``hq_release_catalog`` rows) plus the release a frame measured; the verdict is
whether the grant's release (an exact pin or a release policy over a catalog) admits it. The
runner reads the grant's ``desired`` out of the authority state, folds the catalog rows exactly as
the SQL reader does (:func:`beadhive.release_policy.catalog_row_for`) and decides with
:func:`beadhive.release_policy.admits`, the function the api route's verdict is bound to.

Set ``BH_HQ_SERVICE_CONFORMANCE`` to an hq-service export root to read the corpus in place; with
it unset the copy pinned in ``tests/fixtures/hq_service_contract/release_eligibility_v1`` (see
its PROVENANCE) is replayed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from beadhive.release_policy import admits, catalog_row_for

ENV = "BH_HQ_SERVICE_CONFORMANCE"
PINNED = Path(__file__).parent / "fixtures/hq_service_contract/release_eligibility_v1"


def _corpus() -> Path:
    root = os.environ.get(ENV)
    full = Path(root) / "conformance/release-eligibility/v1/cases" if root else None
    return full if full and full.is_dir() else PINNED


def _names() -> list[str]:
    return sorted(p.stem for p in _corpus().glob("*.json"))


def decide(case: dict) -> bool:
    """The Python reference's verdict for one case's tables."""
    (authority,) = case["rows"]["hq_authority"]
    state = json.loads(authority["state_json"])
    desired = state["frames"][case["frame_id"]]["active"]["desired"]
    policy = desired.get("release_policy")
    row = None
    if policy is not None:
        rows = [
            (r["catalog"], r["version"], r["measured_digest"], r["status"], r["algorithm"])
            for r in case["rows"].get("hq_release_catalog", [])  # an absent table has no row
        ]
        row = catalog_row_for(rows, policy["catalog"], case["measured"])
    return admits(case["measured"], desired, row)


@pytest.mark.parametrize("name", _names())
def test_release_eligibility_case_passes_under_python(name):
    case = json.loads((_corpus() / f"{name}.json").read_text())
    assert case["kind"] == "release-eligibility"
    assert decide(case) is case["expect"]["admits"]


def test_the_corpus_has_both_verdicts():
    verdicts = {
        json.loads((_corpus() / f"{n}.json").read_text())["expect"]["admits"] for n in _names()
    }
    assert verdicts == {True, False}  # a runner that admits (or refuses) everything is caught
