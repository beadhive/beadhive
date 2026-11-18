"""Session renew against the hq-service conformance corpus (bh-nqr4b).

Fixtures under ``tests/fixtures/hq_service_contract/session_v1`` are copied verbatim from
hq-service ``conformance/session/v1`` at commit 205fd5b (see its PROVENANCE.md): one renew case
per distinct ``(status, code)``, plus ``refusal-codes.json``. Time is a fixed value: the
adapter's clock is replaced, nothing sleeps and nothing retries.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import hq_service_adapter
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_port import (
    FrameAnchor,
    HqDeadline,
    HqOutcomeUnknown,
    HqRefused,
    HqUnavailable,
)
from beadhive.hq_service_adapter import PREFIX, RENEW_REFUSALS, HqServiceAdapter
from beadhive.hq_sql_signatures import canonical, fingerprint, verify_heartbeat

CORPUS = Path(__file__).parent / "fixtures/hq_service_contract/session_v1"
CASES = {p.stem: json.loads(p.read_text()) for p in sorted(CORPUS.glob("renew-*.json"))}
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

# Registry codes only the evidence endpoint emits; renew never does.
EVIDENCE_ONLY = {"no_current_beat", "evidence_digest_unmatched", "evidence_invalid"}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(hq_service_adapter, "time", types.SimpleNamespace(monotonic=lambda: NOW))


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    private = Ed25519PrivateKey.generate()
    path = tmp_path_factory.mktemp("frame-key") / "id_ed25519"
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return str(path), public


def _beat(case_name: str = "renew-accepted-first-beat", *, key_id: str) -> HeartbeatLease:
    """The corpus beat's spec, re-keyed to the test signer."""
    spec = json.loads(CASES[case_name]["request"]["body"])["spec"]
    spec.pop("signature")
    spec["key_id"] = key_id
    return HeartbeatLease.model_validate(spec)


def _adapter(key, handler, *, beat=None):
    path, public = key
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    lease = beat or _beat(key_id=fingerprint(public))
    client = httpx.Client(transport=httpx.MockTransport(record), base_url="http://hq")
    adapter = HqServiceAdapter(client, beat_source=lambda anchor: lease, signing_key=path)
    return adapter, calls


def _reply(case: dict):
    """The service's answer to a case, rebuilt from its ``expect`` and the code registry."""
    expect = case["expect"]
    if expect["status"] == 200:
        body = {k: expect[k] for k in ("outcome", "revision", "accepted_until")}
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


# ---- the request: accepted-first-beat / not-canonical ------------------------------------


def test_corpus_beat_is_the_canonical_signed_envelope():
    """The wire form the client targets: the corpus's accepted body is exactly
    ``canonical(envelope)`` and verifies under the case's granted key; the not-canonical case
    is the same document in another encoding."""
    case = CASES["renew-accepted-first-beat"]
    body = case["request"]["body"]
    granted = case["given"]["principals"]["frame_principal_a"]["public_key"]
    assert canonical(json.loads(body)).decode() == body
    lease, digest = verify_heartbeat(json.loads(body), granted_public_key=granted)
    assert digest == case["expect"]["revision"]  # the revision is the digest of the sent bytes
    other = CASES["renew-not-canonical"]["request"]["body"]
    assert json.loads(other) != {} and canonical(json.loads(other)).decode() != other


def test_renew_posts_one_canonical_signed_framelease(key):
    case = CASES["renew-accepted-first-beat"]
    adapter, calls = _adapter(key, _reply(case))
    route = adapter.renew_session(ANCHOR, deadline=NOW + 30)
    (request,) = calls
    assert (request.method, request.url.path, request.url.query) == (
        "POST",
        case["request"]["path"],
        b"",
    )
    assert request.url.path == f"{PREFIX}/sessions/frame_principal_a/3/renew"
    assert request.headers["content-type"] == case["request"]["content_type"]
    assert request.headers["X-Beadhive-Deadline-Ms"] == "30000"
    sent = request.content.decode()
    envelope = json.loads(sent)
    assert canonical(envelope).decode() == sent  # canonical bytes, not re-encoded
    lease, _ = verify_heartbeat(envelope, granted_public_key=key[1])
    assert lease.seq == 1 and lease.frame_id == "frame-a"
    corpus = json.loads(case["request"]["body"])
    assert set(envelope) == set(corpus) and set(envelope["spec"]) == set(corpus["spec"])
    assert envelope["spec"]["signature"]["scope"] == "payload"
    assert (route.outcome, route.revision, route.accepted_until) == (
        "accepted",
        case["expect"]["revision"],
        case["expect"]["accepted_until"],
    )
    assert (route.principal, route.epoch) == ("frame_principal_a", 3)


def test_replayed_is_a_success(key):
    case = CASES["renew-replay-keeps-first-seen"]
    adapter, _ = _adapter(key, _reply(case))
    route = adapter.renew_session(ANCHOR, deadline=NOW + 30)
    assert route.outcome == "replayed" and route.accepted_until == case["expect"]["accepted_until"]


# ---- the refusals ------------------------------------------------------------------------

REFUSED = [n for n, c in CASES.items() if c["expect"]["status"] in (400, 409, 413, 415)]


@pytest.mark.parametrize("name", REFUSED)
def test_corpus_refusal_is_a_port_refusal_with_the_service_code(key, name):
    case = CASES[name]
    adapter, _ = _adapter(key, _reply(case))
    with pytest.raises(HqRefused) as err:
        adapter.renew_session(ANCHOR, deadline=NOW + 30)
    assert err.value.code == case["expect"]["code"]
    assert err.value.reason == CODES[case["expect"]["code"]]["message"]


def test_request_not_canonical_is_refused_not_retried(key):
    adapter, calls = _adapter(key, _reply(CASES["renew-not-canonical"]))
    with pytest.raises(HqRefused) as err:
        adapter.renew_session(ANCHOR, deadline=NOW + 30)
    assert (err.value.code, len(calls)) == ("request_not_canonical", 1)


def test_attestation_failure_is_unavailable_not_a_refusal(key):
    adapter, _ = _adapter(key, _reply(CASES["renew-attestation-wrong-scope"]))
    with pytest.raises(HqUnavailable, match="upstream_authentication_failed"):
        adapter.renew_session(ANCHOR, deadline=NOW + 30)


def test_every_renew_registry_code_is_mapped_and_every_mapping_is_in_the_registry():
    renew = {
        code
        for code, row in CODES.items()
        if code not in EVIDENCE_ONLY and row["status"] != 401 and not row["retryable"]
    }
    assert renew == set(RENEW_REFUSALS)
    # every renew case's code is covered by the sampled corpus
    assert {c["expect"].get("code") for c in CASES.values()} - {None} <= set(RENEW_REFUSALS) | {
        "upstream_authentication_failed"
    }


# ---- transient shapes (conformance/http/v1/codes.json; no corpus case) -------------------


def _envelope(status: int, code: str, retryable: bool):
    body = {"error": {"code": code, "message": code, "retryable": retryable, "requestId": "r"}}
    return lambda request: httpx.Response(status, json=body)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (503, "request_unavailable", True),
        (503, "acceptance_unavailable", True),
        (503, "storage_unavailable", True),
        (409, "floor_cas_conflict", True),
        (409, "observation_cas_conflict", True),
        (405, "method_not_allowed", False),
    ],
)
def test_transient_and_route_defects_are_unavailable(key, status, code, retryable):
    adapter, _ = _adapter(key, _envelope(status, code, retryable))
    with pytest.raises(HqUnavailable, match=code):
        adapter.renew_session(ANCHOR, deadline=NOW + 30)


def test_service_deadline_is_a_port_deadline(key):
    adapter, _ = _adapter(key, _envelope(503, "deadline_exceeded", True))
    with pytest.raises(HqDeadline):
        adapter.renew_session(ANCHOR, deadline=NOW + 30)


def test_gateway_failure_without_an_envelope_is_unavailable(key):
    adapter, _ = _adapter(key, lambda request: httpx.Response(502, text="bad gateway"))
    with pytest.raises(HqUnavailable):
        adapter.renew_session(ANCHOR, deadline=NOW + 30)


def test_timeout_after_send_is_outcome_unknown(key):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    adapter, _ = _adapter(key, boom)
    with pytest.raises(HqOutcomeUnknown):
        adapter.renew_session(ANCHOR, deadline=NOW + 30)


def test_spent_deadline_sends_nothing(key):
    adapter, calls = _adapter(key, _reply(CASES["renew-accepted-first-beat"]))
    with pytest.raises(HqDeadline):
        adapter.renew_session(ANCHOR, deadline=NOW - 1)
    assert calls == []


def test_unusable_success_body_is_unavailable(key):
    adapter, _ = _adapter(key, lambda request: httpx.Response(200, json={"revision": "r"}))
    with pytest.raises(HqUnavailable):
        adapter.renew_session(ANCHOR, deadline=NOW + 30)


# ---- local guards ------------------------------------------------------------------------


def test_unconfigured_renew_never_sends_an_unsigned_body():
    calls = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)),
        base_url="http://hq",
    )
    with pytest.raises(HqUnavailable):
        HqServiceAdapter(client).renew_session(ANCHOR, deadline=NOW + 30)
    assert calls == []


def test_beat_for_another_incarnation_is_refused_before_sending(key):
    adapter, calls = _adapter(key, _reply(CASES["renew-accepted-first-beat"]))
    other = FrameAnchor(principal="frame_principal_a", frame="frame-a", epoch=4)
    with pytest.raises(HqRefused) as err:
        adapter.renew_session(other, deadline=NOW + 30)
    assert err.value.code == "identity_mismatch" and calls == []


def test_signing_key_not_matching_the_beat_is_refused_before_sending(key):
    lease = _beat(key_id="SHA256:" + "A" * 43)
    adapter, calls = _adapter(key, _reply(CASES["renew-accepted-first-beat"]), beat=lease)
    with pytest.raises(HqRefused) as err:
        adapter.renew_session(ANCHOR, deadline=NOW + 30)
    assert err.value.code == "beat_signature_invalid" and calls == []
