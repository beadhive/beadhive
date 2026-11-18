"""Session evidence against the hq-service conformance corpus (bh-xtznm).

Fixtures under ``tests/fixtures/hq_service_contract/session_v1`` are copied verbatim from
hq-service ``conformance/session/v1`` at commit 205fd5b (see its PROVENANCE.md): one evidence
case per distinct ``(status, code)``, plus ``refusal-codes.json``. Time is a fixed value: the
adapter's clock is replaced, nothing sleeps and nothing retries.
"""

from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path

import httpx
import pytest

from beadhive import hq_service_adapter
from beadhive.hq_port import (
    EvidenceReport,
    FrameAnchor,
    HqDeadline,
    HqNoCurrentBeat,
    HqOutcomeUnknown,
    HqRefused,
    HqUnavailable,
)
from beadhive.hq_service_adapter import EVIDENCE_REFUSALS, PREFIX, HqServiceAdapter

CORPUS = Path(__file__).parent / "fixtures/hq_service_contract/session_v1"
CASES = {p.stem: json.loads(p.read_text()) for p in sorted(CORPUS.glob("evidence-*.json"))}
REGISTRY = json.loads((CORPUS / "refusal-codes.json").read_text())
CODES = {k: v for k, v in REGISTRY.items() if not k.startswith("_")}
ANCHOR = FrameAnchor(
    principal="frame_principal_a",
    frame="frame-a",
    epoch=3,
    holder="host-a",
    instance="inst-host-a",
)
NOW = 1000.0
ACCEPTED = "evidence-accepted"

# Registry codes only the renew endpoint emits; evidence never does.
RENEW_ONLY = {
    "beat_not_fresh",
    "beat_schema_invalid",
    "beat_signature_invalid",
    "candidate_expired",
    "floor_unprovisioned",
    "identity_mismatch",
    "lease_duration_below_intervals",
    "lease_duration_out_of_range",
    "request_not_canonical",
    "seq_not_advanced",
}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(hq_service_adapter, "time", types.SimpleNamespace(monotonic=lambda: NOW))


def _report(name: str = ACCEPTED) -> EvidenceReport:
    return EvidenceReport(json.loads(CASES[name]["request"]["body"]))


def _adapter(handler):
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record), base_url="http://hq")
    return HqServiceAdapter(client), calls


def _reply(case: dict):
    """The service's answer to a case, rebuilt from its ``expect`` and the code registry."""
    expect = case["expect"]
    if expect["status"] == 200:
        body = {k: expect[k] for k in ("outcome", "digest")}
    else:
        code = expect["code"]
        body = {
            "error": {
                "code": code,
                "message": CODES[code]["message"],
                "retryable": expect["retryable"],
                "requestId": "req-1",
                "details": None,
            }
        }
    return lambda request: httpx.Response(expect["status"], json=body)


def _envelope(status: int, code: str, retryable: bool):
    body = {"error": {"code": code, "message": code, "retryable": retryable, "requestId": "r"}}
    return lambda request: httpx.Response(status, json=body)


# ---- the request: evidence-accepted / -non-ascii / -key-order-irrelevant -------------------


def test_posts_the_report_unsigned_to_the_evidence_route():
    case = CASES[ACCEPTED]
    adapter, calls = _adapter(_reply(case))
    route = adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)
    (request,) = calls
    assert (request.method, request.url.path, request.url.query) == (
        "POST",
        case["request"]["path"],
        b"",
    )
    assert request.url.path == f"{PREFIX}/sessions/frame_principal_a/3/evidence"
    assert request.headers["content-type"] == case["request"]["content_type"]
    assert request.headers["X-Beadhive-Deadline-Ms"] == "30000"
    # exactly the report: no signature, no scope, no envelope, no extra keys
    assert json.loads(request.content) == json.loads(case["request"]["body"])
    assert set(json.loads(request.content)) == {"method", "checks", "free_sessions"}
    # the corpus's own accepted body is the canonical encoding of the report
    assert request.content.decode() == case["request"]["body"].replace(": ", ":").replace(", ", ",")
    assert (route.outcome, route.digest, route.revision) == (
        "accepted",
        case["expect"]["digest"],
        None,
    )
    assert (route.principal, route.epoch) == ("frame_principal_a", 3)


def test_digest_is_the_sha256_of_the_canonical_report_bytes_sent():
    case = CASES[ACCEPTED]
    adapter, calls = _adapter(_reply(case))
    adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)
    assert "sha256:" + hashlib.sha256(calls[0].content).hexdigest() == case["expect"]["digest"]


def test_key_order_does_not_change_the_bytes_or_the_digest():
    case = CASES["evidence-key-order-irrelevant"]
    adapter, calls = _adapter(_reply(case))
    route = adapter.publish_evidence(
        ANCHOR, _report("evidence-key-order-irrelevant"), deadline=NOW + 30
    )
    assert calls[0].content.decode() == CASES[ACCEPTED]["request"]["body"].replace(
        ": ", ":"
    ).replace(", ", ",")
    assert route.digest == case["expect"]["digest"]


def test_non_ascii_report_is_ascii_escaped_so_it_digests_like_python():
    case = CASES["evidence-accepted-non-ascii"]
    adapter, calls = _adapter(_reply(case))
    route = adapter.publish_evidence(
        ANCHOR, _report("evidence-accepted-non-ascii"), deadline=NOW + 30
    )
    assert calls[0].content.isascii() and b"\\ud83d\\ude00" in calls[0].content
    assert route.digest == case["expect"]["digest"]


def test_replayed_is_a_success():
    case = CASES["evidence-replayed"]
    adapter, _ = _adapter(_reply(case))
    route = adapter.publish_evidence(ANCHOR, _report("evidence-replayed"), deadline=NOW + 30)
    assert (route.outcome, route.digest) == ("replayed", case["expect"]["digest"])


def test_resending_the_same_report_sends_identical_bytes():
    adapter, calls = _adapter(_reply(CASES["evidence-replayed"]))
    adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)
    adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)
    assert calls[0].content == calls[1].content


# ---- the refusals ------------------------------------------------------------------------

REFUSED = [n for n, c in CASES.items() if c["expect"]["status"] in (400, 409, 413, 415)]


@pytest.mark.parametrize("name", REFUSED)
def test_corpus_refusal_is_a_port_refusal_with_the_service_code(name):
    case = CASES[name]
    adapter, calls = _adapter(_reply(case))
    with pytest.raises(HqRefused) as err:
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)
    assert err.value.code == case["expect"]["code"]
    assert err.value.reason == CODES[case["expect"]["code"]]["message"]
    assert len(calls) == 1  # refused once, never retried


def test_no_current_beat_is_a_distinct_typed_refusal():
    adapter, calls = _adapter(_reply(CASES["evidence-no-current-beat"]))
    with pytest.raises(HqNoCurrentBeat) as err:
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)
    assert isinstance(err.value, HqRefused) and err.value.code == "no_current_beat"
    assert len(calls) == 1


@pytest.mark.parametrize("name", [n for n in REFUSED if n != "evidence-no-current-beat"])
def test_only_no_current_beat_is_typed(name):
    adapter, _ = _adapter(_reply(CASES[name]))
    with pytest.raises(HqRefused) as err:
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)
    assert type(err.value) is HqRefused


def test_attestation_failure_is_unavailable_not_a_refusal():
    adapter, _ = _adapter(_reply(CASES["evidence-attestation-wrong-scope"]))
    with pytest.raises(HqUnavailable, match="upstream_authentication_failed"):
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)


def test_every_evidence_registry_code_is_mapped_and_every_mapping_is_in_the_registry():
    evidence = {
        code
        for code, row in CODES.items()
        if code not in RENEW_ONLY and row["status"] != 401 and not row["retryable"]
    }
    assert evidence == set(EVIDENCE_REFUSALS)
    assert set(EVIDENCE_REFUSALS) <= set(CODES)
    # every evidence case's code is a mapped refusal
    assert {c["expect"].get("code") for c in CASES.values()} - {None} <= set(EVIDENCE_REFUSALS) | {
        "upstream_authentication_failed"
    }


# ---- transient shapes (conformance/http/v1/codes.json; no corpus case) -------------------


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (503, "request_unavailable", True),  # hq_live_evidence not provisioned
        (503, "acceptance_unavailable", True),
        (503, "storage_unavailable", True),
        (405, "method_not_allowed", False),
    ],
)
def test_transient_and_route_defects_are_unavailable(status, code, retryable):
    adapter, _ = _adapter(_envelope(status, code, retryable))
    with pytest.raises(HqUnavailable, match=code):
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)


def test_service_deadline_is_a_port_deadline():
    adapter, _ = _adapter(_envelope(503, "deadline_exceeded", True))
    with pytest.raises(HqDeadline):
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)


def test_timeout_after_send_is_outcome_unknown():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    adapter, _ = _adapter(boom)
    with pytest.raises(HqOutcomeUnknown):
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)


def test_spent_deadline_sends_nothing():
    adapter, calls = _adapter(_reply(CASES[ACCEPTED]))
    with pytest.raises(HqDeadline):
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW - 1)
    assert calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"revision": "r"},
        {"outcome": "accepted"},
        {"outcome": "pending", "digest": "sha256:x"},
        {"outcome": "accepted", "digest": "sha256:" + "0" * 64},  # not the report we sent
    ],
)
def test_unusable_success_body_is_unavailable(body):
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=body))
    with pytest.raises(HqUnavailable):
        adapter.publish_evidence(ANCHOR, _report(), deadline=NOW + 30)


def test_unencodable_report_is_refused_before_sending():
    adapter, calls = _adapter(_reply(CASES[ACCEPTED]))
    with pytest.raises(HqRefused) as err:
        adapter.publish_evidence(
            ANCHOR, EvidenceReport({"free_sessions": float("nan")}), deadline=NOW + 30
        )
    assert err.value.code == "evidence_invalid" and calls == []
