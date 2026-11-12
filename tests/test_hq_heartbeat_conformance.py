"""Run the language-neutral heartbeat corpus against the real SqlTrustedReceiver.

The corpus lives in beadhive/hq-service (conformance/heartbeat/v1). It is the
same harness as test_hq_lease_conformance.py: raw rows in SQLite, every
signature and validation real, calling accept_heartbeat.
"""

import json
import os
from pathlib import Path

import pytest

from beadhive.hq_sql_receiver import ReceiverError
from test_hq_lease_conformance import _receiver


def _corpus() -> Path | None:
    if env := os.environ.get("HQ_HEARTBEAT_CORPUS"):
        return Path(env)
    if ws := os.environ.get("GIT_WORKSPACE"):
        path = Path(ws) / "github/beadhive/hq-service/conformance/heartbeat/v1"
        if path.is_dir():
            return path
    return None


CORPUS = _corpus()
CASES = sorted((CORPUS / "cases").glob("*.json")) if CORPUS is not None else []
pytestmark = pytest.mark.skipif(
    not CASES, reason="heartbeat corpus not found (set HQ_HEARTBEAT_CORPUS or GIT_WORKSPACE)"
)


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_heartbeat_conformance(path, monkeypatch):
    case = json.loads(path.read_text())
    refusal_codes = json.loads((CORPUS / "refusal-codes.json").read_text())
    codes = {msg: code for code, msg in refusal_codes.items()}
    world = _receiver(case, monkeypatch)
    expect = case["expect"]
    try:
        digest = world.receiver.accept_heartbeat(
            case["call"]["principal"], case["call"]["request_id"]
        )
    except ReceiverError as exc:
        assert codes.get(str(exc), str(exc)) == expect.get("refuse"), expect
        return
    if "replay" in expect:
        assert (digest, world.committed) == (expect["replay"], False)
    else:
        assert "accept" in expect, f"accepted {digest}, expected {expect}"
        assert (digest, world.committed) == (expect["accept"]["revision"], True)
