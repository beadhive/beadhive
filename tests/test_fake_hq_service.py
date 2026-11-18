"""The in-process fake hq-service enforces the session rules the e2e test relies on (bh-zephr).

If the fake were lenient, ``tests/test_hq_session_staged_e2e.py`` could not fail, so each rule it
claims is pinned here against raw requests (signed bodies taken from the real adapter's output).
"""

from __future__ import annotations

import json

import httpx
import pytest

from beadhive.hq_service_adapter import PREFIX
from harness.fake_hq_service import CODES
from harness.hq_session_stack import CORPUS, SessionStack


@pytest.fixture
def stack(tmp_path, monkeypatch):
    return SessionStack("fake", tmp_path, monkeypatch)


def _post(stack, kind, body, *, epoch=None, content_type="application/json"):
    ident = stack.identity
    path = (
        f"{PREFIX}/sessions/{ident.principal}/{epoch if epoch is not None else ident.epoch}/{kind}"
    )
    client = httpx.Client(transport=stack.fake.transport(), base_url="http://hq")
    response = client.post(path, content=body, headers={"content-type": content_type})
    payload = response.json()
    return response.status_code, (payload.get("error") or {}).get("code") or payload["outcome"]


def _renewed(stack, tag="c1"):
    stack.tag = tag
    stack.timer_tick(stack.process)
    return stack.log[-1]["body"]


def _evidence_body(stack, tag):
    return json.dumps(stack.conformance_report(tag), sort_keys=True, separators=(",", ":")).encode()


def test_renew_then_evidence_for_the_beat_report_is_accepted_and_a_repeat_replays(stack):
    _renewed(stack, "c1")
    assert _post(stack, "evidence", _evidence_body(stack, "c1")) == (200, "accepted")
    assert _post(stack, "evidence", _evidence_body(stack, "c1")) == (200, "replayed")


def test_evidence_with_no_accepted_beat_is_no_current_beat(stack):
    assert _post(stack, "evidence", _evidence_body(stack, "c1")) == (409, "no_current_beat")


def test_evidence_must_equal_the_held_beats_report_digest(stack):
    _renewed(stack, "c1")
    assert _post(stack, "evidence", _evidence_body(stack, "other")) == (
        409,
        "evidence_digest_unmatched",
    )


def test_a_newer_beat_supersedes_the_digest_older_evidence_was_for(stack):
    _renewed(stack, "c1")
    stack.advance(5)
    _renewed(stack, "c2")
    assert _post(stack, "evidence", _evidence_body(stack, "c1"))[1] == "evidence_digest_unmatched"
    assert _post(stack, "evidence", _evidence_body(stack, "c2"))[1] == "accepted"


def test_evidence_report_shape_is_validated(stack):
    _renewed(stack)
    body = json.dumps({**stack.conformance_report("c1"), "extra": 1}).encode()
    assert _post(stack, "evidence", body) == (409, "evidence_invalid")
    assert _post(stack, "evidence", b"not json") == (400, "request_malformed")


def test_the_exact_beat_replays_even_after_a_newer_beat_advanced_the_floor(stack):
    first = _renewed(stack, "c1")
    stack.advance(5)
    _renewed(stack, "c2")  # a newer beat: the floor moved past the first
    assert _post(stack, "renew", first) == (200, "replayed")


def test_seq_must_advance_past_the_floor(stack):
    _renewed(stack)
    floor = stack.fake.principals[stack.identity.principal].floor_seq
    stack.fake.principals[stack.identity.principal].floor_seq = floor + 5
    stack.advance(1)
    # the next client seq is floor_before + 1 (+ a resync only after a refusal): below the floor
    with pytest.raises(Exception) as refused:
        stack.timer_tick(stack.process)
    assert getattr(refused.value, "code", None) == "seq_not_advanced"


def test_the_body_must_be_the_canonical_encoding(stack):
    body = _renewed(stack)
    pretty = json.dumps(json.loads(body), indent=2).encode()
    assert _post(stack, "renew", pretty) == (409, "request_not_canonical")


def test_a_beat_not_signed_by_the_granted_key_is_refused(stack):
    body = json.loads(_renewed(stack))
    body["spec"]["signature"]["value"] = "A" * 86 + "=="
    assert (
        _post(stack, "renew", json.dumps(body, sort_keys=True, separators=(",", ":")).encode())[1]
        == "beat_signature_invalid"
    )


def test_a_stale_or_future_beat_is_not_fresh(stack):
    body = _renewed(stack)
    stack.advance(10_000)  # the beat's first observation is long expired
    # freshness is judged before the replay lookup, as in the service
    assert _post(stack, "renew", body) == (409, "beat_not_fresh")


def test_the_beat_identity_must_match_the_grant(stack):
    body = _renewed(stack)
    stack.fake.principals[stack.identity.principal].receipts.clear()
    stack.fake.principals[stack.identity.principal].holder = "someone-else"
    assert _post(stack, "renew", body) == (409, "identity_mismatch")


def test_path_and_grant_rules(stack):
    body = _evidence_body(stack, "c1")
    assert _post(stack, "evidence", body, epoch=stack.identity.epoch - 1) == (409, "epoch_stale")
    assert _post(stack, "evidence", body, epoch=stack.identity.epoch + 1) == (409, "epoch_future")
    assert _post(stack, "evidence", body, epoch="03") == (400, "path_invalid")
    assert _post(stack, "evidence", body, content_type="text/plain") == (
        415,
        "content_type_invalid",
    )
    stack.fake.principals[stack.identity.principal].grant = "retired"
    assert _post(stack, "evidence", body) == (409, "grant_retired")


def test_an_unprovisioned_floor_refuses_the_first_beat(stack):
    stack.fake.principals[stack.identity.principal].floor_seq = None
    with pytest.raises(Exception) as refused:
        stack.timer_tick(stack.process)
    assert getattr(refused.value, "code", None) == "floor_unprovisioned"


def test_every_code_the_fake_answers_matches_the_corpus_registry():
    registry = json.loads((CORPUS / "refusal-codes.json").read_text())
    for code, (status, _) in CODES.items():
        assert registry[code]["status"] == status, code
