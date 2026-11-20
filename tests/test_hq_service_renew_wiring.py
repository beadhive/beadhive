"""Config wiring of session renew: beat source and frame key (bh-1bsvh).

``adapter_from_config`` / ``select_hq_port`` give the adapter the sender's own beat builder
(``heartbeat_report.generate(plane, cached=True)``, the plane being this frame's grant read through
the same adapter, bh-65s71) and the recorded per-host signing key. Missing
or unusable pieces leave renew raising ``HqUnavailable`` with nothing sent, and neither key paths
nor beat-builder text reach an exception. Time is a fixed value; nothing sleeps or retries.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import heartbeat_report, hq_service_adapter, hq_sql_signatures
from beadhive.hq_frame_grant import ApiFrameAuthority
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_port import FrameAnchor, HqRefused, HqUnavailable
from beadhive.hq_service_adapter import HqServiceAdapter, adapter_from_config, select_hq_port
from beadhive.hq_sql_signatures import fingerprint, verify_heartbeat

CORPUS = Path(__file__).parent / "fixtures/hq_service_contract/session_v1"
ANCHOR = FrameAnchor(
    principal="frame_principal_a", frame="frame-a", epoch=3, holder="host-a", instance="inst-host-a"
)
NOW = 1000.0
CFG = {"hq": {"route": "api", "api": {"url": "https://hq.example"}}}
SECRET = "s3cr3t-credential-ref"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(hq_service_adapter, "time", types.SimpleNamespace(monotonic=lambda: NOW))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """The renew seq state lives under the bh home: never the developer's own."""
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bh-home"))


@pytest.fixture
def key(tmp_path):
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "id_ed25519"
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


def _lease(public: str) -> HeartbeatLease:
    spec = json.loads(
        json.loads((CORPUS / "renew-accepted-first-beat.json").read_text())["request"]["body"]
    )["spec"]
    spec.pop("signature")
    spec["key_id"] = fingerprint(public)
    return HeartbeatLease.model_validate(spec)


def _record(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"outcome": "accepted", "revision": "r1", "accepted_until": 1500.0}
        )

    return httpx.MockTransport(handler)


def _generate_returns(monkeypatch, lease, seen=None):
    """Stand in for the generator, recording the plane and arguments it was called with."""

    def generate(got_plane, **kwargs):
        if seen is not None:
            seen.append((got_plane, kwargs))
        return lease

    monkeypatch.setattr(heartbeat_report, "generate", generate)


def _host_key(monkeypatch, value):
    from beadhive import host

    def signing_key():
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(host, "signing_key", signing_key)


def test_configured_adapter_signs_and_sends_a_real_beat(key, monkeypatch):
    path, public = key
    seen: list = []
    _generate_returns(monkeypatch, _lease(public), seen)
    _host_key(monkeypatch, path)
    calls: list[httpx.Request] = []
    adapter = adapter_from_config(CFG, transport=_record(calls))
    route = adapter.renew_session(ANCHOR, deadline=NOW + 30)
    (request,) = calls
    assert request.url.path.endswith("/sessions/frame_principal_a/3/renew")
    verified, _ = verify_heartbeat(json.loads(request.content), granted_public_key=public)
    assert verified.key_id == fingerprint(public)
    assert route.outcome == "accepted"
    # The beat is the sender's own cached-conformance projection, of this frame's grant read
    # through this same adapter (no SQL plane).
    ((plane, kwargs),) = seen
    assert kwargs == {"cached": True}
    assert isinstance(plane, ApiFrameAuthority)
    assert (plane.port, plane.frame) == (adapter, ANCHOR.frame)


def test_select_hq_port_wires_the_same_beat_source_and_key(key, monkeypatch):
    path, _ = key
    _host_key(monkeypatch, path)
    port = select_hq_port(CFG)
    assert isinstance(port, HqServiceAdapter)
    assert port._signing_key == path
    assert port._beat_source.func is hq_service_adapter._frame_beat_source
    assert port._beat_source.keywords == {"port": port}


@pytest.mark.parametrize("recorded", ["", None, "relative/id_ed25519", FileNotFoundError(SECRET)])
def test_no_usable_recorded_key_is_unavailable_and_sends_nothing(recorded, key, monkeypatch):
    _generate_returns(monkeypatch, _lease(key[1]))
    _host_key(monkeypatch, recorded)
    calls: list = []
    adapter = adapter_from_config(CFG, transport=_record(calls))
    with pytest.raises(HqUnavailable) as raised:
        adapter.renew_session(ANCHOR, deadline=NOW + 30)
    assert calls == []
    assert SECRET not in str(raised.value) and "relative" not in str(raised.value)


def test_an_unverifiable_grant_is_unavailable_and_renews_nothing(key, monkeypatch):
    """No response verifier: the grant read fails closed, so no beat is built or posted."""
    _host_key(monkeypatch, key[0])
    calls: list = []
    adapter = adapter_from_config(CFG, transport=_record(calls))
    with pytest.raises(HqUnavailable):
        adapter.renew_session(ANCHOR, deadline=NOW + 30)
    assert not [c for c in calls if c.method == "POST"]


def test_beat_builder_failure_text_never_reaches_the_exception(key, monkeypatch):
    monkeypatch.setattr(heartbeat_report, "_sql_plane", lambda: object())

    def explode(*_a, **_k):
        raise OSError(f"connect failed password={SECRET} key={key[0]}")

    monkeypatch.setattr(heartbeat_report, "generate", explode)
    _host_key(monkeypatch, key[0])
    calls: list = []
    adapter = adapter_from_config(CFG, transport=_record(calls))
    with pytest.raises(HqUnavailable) as raised:
        adapter.renew_session(ANCHOR, deadline=NOW + 30)
    assert calls == []
    # ``from None``: no cause, and the original is suppressed from any printed traceback.
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    text = repr(raised.value)
    assert SECRET not in text and key[0] not in text


def test_unusable_key_file_refuses_without_naming_the_path(key, tmp_path, monkeypatch):
    _generate_returns(monkeypatch, _lease(key[1]))
    bad = tmp_path / "not-a-key"
    bad.write_text("garbage")
    _host_key(monkeypatch, str(bad))
    calls: list = []
    adapter = adapter_from_config(CFG, transport=_record(calls))
    with pytest.raises(HqRefused) as raised:
        adapter.renew_session(ANCHOR, deadline=NOW + 30)
    assert calls == [] and str(bad) not in str(raised.value)
    assert isinstance(raised.value.__cause__, hq_sql_signatures.SqlSignatureError)


@pytest.mark.parametrize("status", [403, 404, 422])
def test_unknown_non_retryable_4xx_code_stays_unavailable(status, key):
    """The decision recorded in ``_raise_envelope``: a code the corpus does not define on a 4xx
    outside 400/409/413/415 is not a definite refusal of this request."""
    body = {"error": {"code": "brand_new_code", "message": "m", "retryable": False}}
    adapter = HqServiceAdapter(
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)),
            base_url="http://hq",
        ),
        beat_source=lambda anchor: _lease(key[1]),
        signing_key=key[0],
    )
    with pytest.raises(HqUnavailable):
        adapter.renew_session(ANCHOR, deadline=NOW + 30)
