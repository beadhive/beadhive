"""The route=api read parity runners (``tests/harness/hq_read_parity.py``, bh-h9dn1), without a
live Dolt server: the shadows run against a stand-in ``hq-parity`` executable and a stand-in
SQL read, and the verdicts and their classification are pinned."""

from __future__ import annotations

import json
import stat
import sys
from types import SimpleNamespace

from tests.harness import hq_read_parity as parity
from tests.test_hq_api_reads import (
    CONFIG_HEAD,
    FLEET_YAML,
    HEAD,
    authority,
    config_body,
    eligibility_body,
    grant,
    manifest,
)

from beadhive.hq_api_eligibility import ApiSessionObservation
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot


def _observation(**predicates) -> ApiSessionObservation:
    values = {
        "session_epoch_matches": True,
        "authenticated_fresh_heartbeat": True,
        "conformance_pass": True,
        "release_matches": True,
        **predicates,
    }
    return ApiSessionObservation(
        status="fresh",
        fresh=True,
        age_seconds=1.0,
        current_hive_lease_holder=False,
        principal="p",
        epoch=3,
        renewed_at="",
        measured_at="",
        evidence_age_seconds=1.0,
        evidence_status="conformant",
        evidence_digest="",
        session_ttl_s=300,
        evidence_ttl_s=900,
        **values,
    )


def _python_result(**predicates):
    desired = {"state": "active", "cordoned": False}
    return HEAD, desired, _observation(**predicates)


def _go_served() -> dict:
    active = grant()
    return {
        "outcome": "served",
        "eligibility": eligibility_body(active),
        "authority": authority(active),
    }


def _binary(tmp_path, reply: dict):
    """A stand-in ``hq-parity``: records its stdin, answers ``reply``."""
    seen = tmp_path / "seen.json"
    script = tmp_path / "hq-parity"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(seen)!r}, 'w').write(sys.stdin.read())\n"
        f"print(json.dumps({reply!r}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), seen


def _settings() -> dict:
    binding = {"host": "h", "port": 1, "database": "rt", "user": "u", "credential": "c"}
    return {
        "runtime": binding,
        "reader": {"database": "cfg"},
        "runtime_operator_public_key": "k",
        "runtime_backend_identity": "svc",
        "runtime_generation": "g",
        "runtime_initial_revision": "i",
    }


class _Broker:
    def get(self, _credential, *, deadline):
        return "pw"


def test_a_conforming_frame_matches_on_both_sides():
    go = parity.go_eligibility(_go_served(), manifest(), enforce=True)
    record = {
        "kind": "eligibility",
        "python": {"outcome": "served", "verdict": parity.python_eligibility(_python_result())},
        "go": {"outcome": "served", "verdict": go},
    }
    assert parity.classify(record) == "match"


def test_a_predicate_disagreement_is_a_diff():
    go = parity.go_eligibility(_go_served(), manifest(), enforce=True)
    python = parity.python_eligibility(_python_result(conformance_pass=False))
    record = {
        "kind": "eligibility",
        "python": {"outcome": "served", "verdict": python},
        "go": {"outcome": "served", "verdict": go},
    }
    assert parity.classify(record) == "diff"


def test_a_signed_beat_read_is_not_comparable():
    beat = SimpleNamespace(carrier="beat")
    python = parity.python_eligibility((HEAD, {"state": "active"}, beat))
    record = {
        "kind": "eligibility",
        "python": {"outcome": "served", "verdict": python},
        "go": {"outcome": "served", "verdict": {}},
    }
    assert parity.classify(record) == "not-comparable"


def test_config_verdicts_compare_head_and_documents():
    body = config_body(authority(grant()), [("fleet.yaml", FLEET_YAML)])
    snapshot = FleetConfigSnapshot(
        "sql:cfg",
        CONFIG_HEAD,
        "cfg-gen",
        0.0,
        30.0,
        (FleetConfigDocument("fleet.yaml", FLEET_YAML),),
    )
    record = {
        "kind": "config",
        "python": {"outcome": "served", "verdict": parity.python_config(snapshot)},
        "go": {"outcome": "served", "verdict": parity.go_config({"config": body})},
    }
    assert parity.classify(record) == "match"
    record["python"]["verdict"]["config_head"] = "d" * 32
    assert parity.classify(record) == "diff"


def test_v010_hq_parity_without_the_kind_is_unsupported_not_a_diff(tmp_path, monkeypatch):
    binary, seen = _binary(tmp_path, {"error": 'input: json: unknown field "frame"'})
    log = tmp_path / "parity.jsonl"
    monkeypatch.setenv("HQ_PARITY_LOG", str(log))
    plane = SimpleNamespace(
        _runtime_authority=lambda: SimpleNamespace(settings=_settings(), broker=_Broker())
    )
    shadowed = parity.shadow_eligibility(
        lambda _self, _manifest: _python_result(), binary, log, "t"
    )
    assert shadowed(plane, manifest()) == _python_result()  # the test's outcome is untouched
    sent = json.loads(seen.read_text())
    assert (sent["kind"], sent["frame"]) == ("eligibility", "frame-1")
    assert sent["measured"]["id"] == "rel-1"
    (record,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert parity.classify(record) == "unsupported"


def test_a_served_config_is_logged_and_compared(tmp_path, monkeypatch):
    body = config_body(authority(grant()), [("fleet.yaml", FLEET_YAML)])
    binary, _ = _binary(tmp_path, {"outcome": "served", "config": body})
    log = tmp_path / "parity.jsonl"
    monkeypatch.setenv("HQ_PARITY_LOG", str(log))
    snapshot = FleetConfigSnapshot(
        "sql:cfg",
        CONFIG_HEAD,
        "cfg-gen",
        0.0,
        30.0,
        (FleetConfigDocument("fleet.yaml", FLEET_YAML),),
    )
    store = SimpleNamespace(settings=_settings(), broker=_Broker())
    shadowed = parity.shadow_config(lambda _self: snapshot, binary, log, "t")
    assert shadowed(store) is snapshot
    (record,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["kind"] == "config" and parity.classify(record) == "match"
