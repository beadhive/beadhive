"""Per-case authority mode must not leak between conformance cases (bh-4m061).

The runners pin ``authority_mode`` through ``monkeypatch`` for one case (tests/harness/
hq_authority_mode.py). These tests run a trusted case, an enforced case and an unmoded case back to
back in ONE process (the same position as consecutive cases on one ``-n`` worker) and check that
neither the environment nor the outcomes carry over.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from beadhive import hq_authority_enforce as enforce
from beadhive.hq_sql_receiver import ReceiverError
from harness import hq_authority_mode as modes

MODE_VARS = (enforce.MODE_ENV, enforce.ENFORCE_ENV, "BH_HOME")


def _child_mode() -> str:
    """The mode a fresh subprocess resolves from the current environment."""
    out = subprocess.run(
        [sys.executable, "-c", "from beadhive import hq_authority_enforce as e; print(e.mode())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_pin_is_scoped_to_one_case():
    before = {name: os.environ.get(name) for name in MODE_VARS}
    for mode, expected in (("trusted", "trusted"), ("enforced", "signed"), ("trusted", "trusted")):
        with pytest.MonkeyPatch.context() as mp:
            assert modes.pin({"authority_mode": mode}, mp) == mode
            assert enforce.mode() == expected
            assert _child_mode() == expected  # receiver subprocesses inherit the pinned mode
        assert {name: os.environ.get(name) for name in MODE_VARS} == before
        with pytest.MonkeyPatch.context() as mp:
            assert modes.pin({}, mp) is None  # no field: nothing is pinned
            assert os.environ.get(enforce.MODE_ENV) == before[enforce.MODE_ENV]
            assert enforce.mode() == enforce.resolve(enforce._configured_mode()).mode


def test_unknown_mode_is_refused():
    with pytest.MonkeyPatch.context() as mp, pytest.raises(AssertionError):
        modes.pin({"authority_mode": "signed"}, mp)  # the corpus words are trusted | enforced


def _lease_corpus() -> Path | None:
    from test_hq_lease_conformance import CORPUS

    return CORPUS if CORPUS is not None and (CORPUS / "cases").is_dir() else None


def _run_lease(corpus: Path, name: str):
    from test_hq_lease_conformance import _receiver

    case = json.loads((corpus / "cases" / f"{name}.json").read_text())
    with pytest.MonkeyPatch.context() as mp:
        world = _receiver(case, mp)
        call = case["call"]
        try:
            world.receiver.accept_hive_lease(call["principal"], call["request_id"])
            outcome = ("accept", None)
        except ReceiverError:
            outcome = ("refuse", None)
    return case, outcome[0]


def test_outcomes_do_not_leak_between_cases():
    corpus = _lease_corpus()
    marker = "trusted-authority-signature-invalid.json"
    if corpus is None or not (corpus / "cases" / marker).exists():
        pytest.skip("hive-lease corpus with authority_mode cases not found (HQ_LEASE_CORPUS)")
    before = {name: os.environ.get(name) for name in MODE_VARS}
    # trusted accepts a bad authority signature; the enforced case right after must refuse it;
    # an unmoded case after that runs as it always did; trusted again is unaffected by enforced.
    sequence = (
        ("trusted-authority-signature-invalid", "accept"),
        ("enforced-authority-signature-invalid", "refuse"),
        ("adopt-from-absent", "accept"),
        ("authority-signature-invalid", "refuse"),
        ("trusted-authority-signature-invalid", "accept"),
        ("enforced-authority-signature-invalid", "refuse"),
    )
    for name, expected in sequence:
        case, outcome = _run_lease(corpus, name)
        assert outcome == expected, name
        assert outcome == ("accept" if "accept" in case["expect"] else "refuse"), name
        assert {n: os.environ.get(n) for n in MODE_VARS} == before, name
