"""Run the language-neutral grant corpus against the pure grant rules (bh-exm7k, D3).

The corpus lives in beadhive/hq-service (``conformance/grant/v1``), owned there per the
bh-hq-2p6 decision; beadhive keeps no copy and reads it in place (``HQ_GRANT_CORPUS``, else
``$GIT_WORKSPACE/github/beadhive/hq-service/conformance/grant/v1``), as the other corpus runners
do. hq-service's Go admission rules run the same cases.

Case shape (one JSON file per case under ``cases/``)::

    {"call": {"original": <authority state>, "head": str, "expected": str,
              "authority": <ObservationAuthority fields>, "public_key": str,
              "desired": <desired policy>, "admitted": bool, "now": number,
              "operator_public_key": str},
     "expect": {"refuse": <code>}
            | {"accept": {"state": <next state>, "provisioned_route": [six fields]}}}

``refusal-codes.json`` maps each stable code to the current Python message, as the
registration corpus does.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from beadhive.host_heartbeat_core import ObservationAuthority
from beadhive.hq_grant_rules import REFUSALS, GrantRefused, grant_transition

DESIRED = {
    "declared": True,
    "release": {"id": "r1", "digest": "sha256:" + "1" * 64},
    "caps": {
        "isolation": "container",
        "trust_zone": "self-hosted",
        "arch": "arm64",
        "harnesses": ["codex"],
        "max_sessions": 1,
    },
    "profile": "p",
}


def _corpus() -> Path | None:
    if env := os.environ.get("HQ_GRANT_CORPUS"):
        return Path(env)
    if ws := os.environ.get("GIT_WORKSPACE"):
        path = Path(ws) / "github/beadhive/hq-service/conformance/grant/v1"
        if path.is_dir():
            return path
    return None


def run_case(case: dict, refusal_codes: dict) -> None:
    """Decide one corpus case with :func:`grant_transition` and assert its expectation."""
    call, expect = case["call"], case["expect"]
    codes = {message: code for code, message in refusal_codes.items() if code != "_comment"}
    try:
        result = grant_transition(
            call["original"],
            head=call["head"],
            expected=call["expected"],
            authority=ObservationAuthority(**call["authority"]),
            public_key=call["public_key"],
            desired=call["desired"],
            admitted=call["admitted"],
            now=call["now"],
            operator_public_key=call["operator_public_key"],
        )
    except GrantRefused as exc:
        assert codes.get(str(exc), str(exc)) == expect.get("refuse"), expect
        return
    assert "accept" in expect, f"accepted, expected {expect}"
    assert result.state == expect["accept"]["state"]
    assert list(result.provisioned_route) == expect["accept"]["provisioned_route"]


CORPUS = _corpus()
CASES = sorted((CORPUS / "cases").glob("*.json")) if CORPUS is not None else []


@pytest.mark.skipif(
    not CASES, reason="grant corpus not found (set HQ_GRANT_CORPUS or GIT_WORKSPACE)"
)
@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_grant_conformance(path):
    run_case(
        json.loads(path.read_text()),
        json.loads((CORPUS / "refusal-codes.json").read_text()),
    )


def test_the_runner_decides_a_case_in_the_corpus_shape(tmp_path):
    """The runner itself, on one accept and one refuse case written in the corpus shape."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from beadhive import hq_authority_guard as guard
    from beadhive.hq_sql_signatures import fingerprint, sql_principal

    def key():
        return (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )

    candidate, operator = key(), key()
    authority = {
        "frame_id": "frame2",
        "holder_identity": "host2",
        "instance_ref": "vm2",
        "key_fingerprint": fingerprint(candidate),
        "epoch": 1,
        "audience": "fleet",
        "config_revision": "cfg",
        "candidate_expires_at": 1600.0,
        "beadyard_id": None,
    }
    original = {
        "domain": guard.DOMAIN,
        "generation": "g",
        "revision": 4,
        "issued_at": 900.0,
        "expires_at": 9000.0,
        "frames": {},
    }
    call = {
        "original": original,
        "head": "h",
        "expected": "h",
        "authority": authority,
        "public_key": candidate,
        "desired": DESIRED,
        "admitted": False,
        "now": 1000.0,
        "operator_public_key": operator,
    }
    payload = {k: v for k, v in authority.items() if k != "beadyard_id"}
    state = json.loads(json.dumps(original))
    state.update(revision=5, issued_at=1000.0)
    state["frames"]["frame2"] = {
        "active": None,
        "candidate": {
            "authority": payload,
            "public_key": candidate,
            "state": "pending",
            "desired": DESIRED,
            "cordoned": False,
            "drain_deadline": None,
            "receipt": {
                "sequence": 0,
                "sha": "",
                "first_seen": None,
                "consecutive": 0,
                "lease": None,
                "registration": None,
            },
        },
        "retired": [],
        "epoch_floor": 1,
    }
    route = [
        sql_principal(ObservationAuthority(**authority)),
        "frame2",
        "host2",
        "vm2",
        1,
        authority["key_fingerprint"],
    ]
    # Round-trip through JSON so the case is exactly what a corpus file would hold.
    accept = json.loads(
        json.dumps(
            {"call": call, "expect": {"accept": {"state": state, "provisioned_route": route}}}
        )
    )
    refuse = json.loads(
        json.dumps(
            {"call": {**call, "head": "moved"}, "expect": {"refuse": "identity_or_cas_invalid"}}
        )
    )
    codes = dict(REFUSALS)
    run_case(accept, codes)
    run_case(refuse, codes)
    with pytest.raises(AssertionError):
        run_case({**refuse, "expect": {"refuse": "frame_already_active"}}, codes)
