"""The grant rules as a pure function, and the SQL adapter's typed admit (bh-exm7k, D3)."""

from __future__ import annotations

import copy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import hq_authority_guard as guard
from beadhive import hq_sql_adapter
from beadhive.host_heartbeat_core import ObservationAuthority
from beadhive.hq_grant_rules import (
    CANDIDATE_EXISTS_OR_EPOCH_STALE,
    FRAME_ALREADY_ACTIVE,
    IDENTITY_OR_CAS_INVALID,
    GrantRefused,
    grant_transition,
)
from beadhive.hq_port import AdmissionRequest, HqRefused
from beadhive.hq_sql_signatures import fingerprint, sql_principal

NOW = 1_000_000.0
CAPS = {
    "isolation": "container",
    "trust_zone": "self-hosted",
    "arch": "arm64",
    "harnesses": ["codex"],
    "max_sessions": 1,
}
DESIRED = {
    "declared": True,
    "release": {"id": "r1", "digest": "sha256:" + "1" * 64},
    "caps": CAPS,
    "profile": "p",
}
UNDECLARED = {k: v for k, v in DESIRED.items() if k != "declared"}


def _key() -> str:
    return (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


OPERATOR = _key()


def _state(frames=None, expires_at=NOW + 7200):
    return {
        "domain": guard.DOMAIN,
        "generation": "g",
        "revision": 4,
        "issued_at": NOW - 100,
        "expires_at": expires_at,
        "frames": frames or {},
    }


def _authority(key, *, frame="frame2", holder="host2", epoch=1, expires=NOW + 600, beadyard=None):
    return ObservationAuthority(
        frame, holder, "vm2", fingerprint(key), epoch, "fleet", "cfg", expires, beadyard
    )


def _transition(original, key, authority, *, admitted=False, head="h", expected="h"):
    return grant_transition(
        original,
        head=head,
        expected=expected,
        authority=authority,
        public_key=key,
        desired=DESIRED,
        admitted=admitted,
        now=NOW,
        operator_public_key=OPERATOR,
    )


@pytest.mark.parametrize("admitted", [False, True])
def test_a_grant_records_the_incarnation_and_advances_the_state(admitted):
    key = _key()
    authority = _authority(key)
    original = _state()
    snapshot = copy.deepcopy(original)
    result = _transition(original, key + " comment", authority, admitted=admitted)
    assert original == snapshot  # pure: the input is never mutated
    entry = result.state["frames"]["frame2"]
    record = entry["active" if admitted else "candidate"]
    assert entry["active" if not admitted else "candidate"] is None
    assert record["state"] == ("active" if admitted else "pending")
    assert record["public_key"] == key + " comment"
    assert record["desired"] == DESIRED
    assert entry["epoch_floor"] == 1
    assert result.state["revision"] == 5
    assert result.state["issued_at"] == NOW
    assert result.state["expires_at"] == NOW + 7200
    assert result.state["domain"] == guard.DOMAIN
    assert result.provisioned_route == (
        sql_principal(authority),
        "frame2",
        "host2",
        "vm2",
        1,
        authority.key_fingerprint,
    )


def test_a_bound_authority_upgrades_the_domain_and_the_expiry_keeps_an_hour_floor():
    key = _key()
    result = _transition(
        _state(expires_at=NOW + 10),
        key,
        _authority(key, beadyard="2f1c3e4a-5b6d-4e7f-8a9b-0c1d2e3f4a5b"),
    )
    assert result.state["domain"] == guard.DOMAIN_V2
    assert result.state["expires_at"] == NOW + 3600


def test_the_same_inputs_give_the_same_state():
    key = _key()
    authority = _authority(key)
    assert _transition(_state(), key, authority) == _transition(_state(), key, authority)


def _existing(key, *, frame="frame1", holder="host1", slot="active", floor=1):
    other = _authority(key, frame=frame, holder=holder)
    record = {
        "authority": {k: v for k, v in vars(other).items() if k != "beadyard_id"},
        "public_key": key,
        "state": "active" if slot == "active" else "pending",
        "desired": DESIRED,
        "cordoned": False,
        "drain_deadline": None,
        "receipt": {},
    }
    entry = {"active": None, "candidate": None, "retired": [], "epoch_floor": floor}
    if slot is not None:
        entry[slot] = record
    return {frame: entry}


@pytest.mark.parametrize(
    "case",
    [
        "stale-head",
        "candidate-no-expiry",
        "candidate-expired",
        "key-mismatch",
        "operator-key",
        "signer-reuse",
        "holder-reuse",
    ],
)
def test_identity_or_cas_refusals(case):
    key = _key()
    authority = _authority(key)
    original = _state()
    kwargs = {}
    if case == "stale-head":
        kwargs["head"] = "other"
    elif case == "candidate-no-expiry":
        authority = _authority(key, expires=None)
    elif case == "candidate-expired":
        authority = _authority(key, expires=NOW)
    elif case == "key-mismatch":
        authority = _authority(_key())
    elif case == "operator-key":
        key, authority = OPERATOR, _authority(OPERATOR)
    elif case == "signer-reuse":
        original = _state(_existing(key))
    elif case == "holder-reuse":
        original = _state(_existing(_key(), holder="host2"))
    with pytest.raises(GrantRefused) as excinfo:
        _transition(original, key, authority, **kwargs)
    assert excinfo.value.code == IDENTITY_OR_CAS_INVALID
    assert str(excinfo.value) == "candidate grant identity or original CAS invalid"


def test_an_open_admission_needs_no_candidate_expiry():
    key = _key()
    result = _transition(_state(), key, _authority(key, expires=None), admitted=True)
    assert result.state["frames"]["frame2"]["active"]["state"] == "active"


@pytest.mark.parametrize(
    ("slot", "floor", "admitted", "code"),
    [
        ("candidate", 0, False, CANDIDATE_EXISTS_OR_EPOCH_STALE),
        (None, 1, False, CANDIDATE_EXISTS_OR_EPOCH_STALE),
        ("active", 0, True, FRAME_ALREADY_ACTIVE),
    ],
)
def test_frame_entry_refusals(slot, floor, admitted, code):
    key = _key()
    original = _state(_existing(_key(), frame="frame2", holder="host9", slot=slot, floor=floor))
    with pytest.raises(GrantRefused) as excinfo:
        _transition(original, key, _authority(key), admitted=admitted)
    assert excinfo.value.code == code


# ---- SqlHqAdapter.admit ----------------------------------------------------------------


class _Operator:
    def __init__(self, state, head="h"):
        self.state, self.head, self.published = state, head, []

    def load(self, **_kwargs):
        return self.head, copy.deepcopy(self.state), (), {}

    def publish(self, state, **kwargs):
        self.published.append((state, kwargs))
        return "head-2"


class _Plane:
    settings = {"runtime_operator_public_key": OPERATOR}

    @staticmethod
    def clock():
        return NOW


def _adapter(operator):
    adapter = object.__new__(hq_sql_adapter.SqlHqAdapter)
    adapter._operator = lambda: operator
    adapter._plane = _Plane()
    return adapter


def test_sql_admit_builds_the_state_with_the_rules_and_signs_with_the_signer():
    key = _key()
    authority = _authority(key)
    operator = _Operator(_state())
    signer = lambda record: "sig"  # noqa: E731
    request = AdmissionRequest(frame="frame2", authority=authority, public_key=key, desired=DESIRED)
    head = _adapter(operator).admit(
        "frame2", request, signer=signer, expected_revision="h", deadline=1.0
    )
    assert head == "head-2"
    state, kwargs = operator.published[0]
    expected = _transition(_state(), key, authority)
    assert state == expected.state
    assert kwargs["authority_signer"] is signer
    assert kwargs["provisioned_route"] == expected.provisioned_route
    assert kwargs["expected_revision"] == "h"
    assert "operator_key" not in kwargs


def test_sql_admit_uses_a_requested_route():
    key = _key()
    operator = _Operator(_state())
    route = ("p", "frame2", "host2", "vm2", 1, "fp")
    request = AdmissionRequest(
        frame="frame2",
        authority=_authority(key),
        public_key=key,
        desired=DESIRED,
        provisioned_route=route,
    )
    _adapter(operator).admit("frame2", request, signer=str, expected_revision="h", deadline=1.0)
    assert operator.published[0][1]["provisioned_route"] == route


def test_sql_admit_open_passes_the_trusted_fleet_guard(monkeypatch):
    from beadhive import hq_authority_enforce

    monkeypatch.setattr(hq_authority_enforce, "fleet_trusted", lambda: True)
    key = _key()
    operator = _Operator(_state())
    request = AdmissionRequest(
        frame="frame2",
        authority=_authority(key, expires=None),
        public_key=key,
        desired=UNDECLARED,
        admitted=True,
    )
    _adapter(operator).admit("frame2", request, signer=str, expected_revision="h", deadline=1.0)
    record = operator.published[0][0]["frames"]["frame2"]["active"]
    assert record["desired"] == DESIRED


def test_sql_admit_open_in_an_untrusted_fleet_is_refused(monkeypatch):
    from beadhive import hq_authority_enforce

    monkeypatch.setattr(hq_authority_enforce, "fleet_trusted", lambda: False)
    key = _key()
    operator = _Operator(_state())
    request = AdmissionRequest(
        frame="frame2", authority=_authority(key), public_key=key, desired={}, admitted=True
    )
    with pytest.raises(HqRefused):
        _adapter(operator).admit("frame2", request, signer=str, expected_revision="h", deadline=1.0)
    assert operator.published == []


def test_sql_admit_maps_a_rule_refusal_to_its_stable_code():
    key = _key()
    operator = _Operator(_state(), head="moved")
    request = AdmissionRequest(
        frame="frame2", authority=_authority(key), public_key=key, desired={}
    )
    with pytest.raises(HqRefused) as excinfo:
        _adapter(operator).admit("frame2", request, signer=str, expected_revision="h", deadline=1.0)
    assert excinfo.value.code == IDENTITY_OR_CAS_INVALID
    assert operator.published == []


@pytest.mark.parametrize(
    ("frame", "request_of"),
    [
        ("frame2", lambda a, k: {"state": {}}),
        ("frame9", lambda a, k: AdmissionRequest("frame2", a, k, {})),
        ("frame9", lambda a, k: AdmissionRequest("frame9", a, k, {})),
    ],
)
def test_sql_admit_refuses_an_untyped_or_mismatched_request(frame, request_of):
    key = _key()
    operator = _Operator(_state())
    with pytest.raises(HqRefused):
        _adapter(operator).admit(
            frame, request_of(_authority(key), key), signer=str, expected_revision="h", deadline=1.0
        )
    assert operator.published == []


# ---- HqServiceAdapter.admit (hq-service v0.1.0 body: store/admission.go) -------------------


def test_api_admit_sends_the_intent_and_never_signs():
    import json

    import httpx

    from beadhive.hq_service_adapter import HqServiceAdapter

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"revision": "r8", "frame_id": "frame2"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://hq")
    adapter = HqServiceAdapter(client)
    key = _key()
    authority = _authority(key)
    request = AdmissionRequest(
        frame="frame2",
        authority=authority,
        public_key=key,
        desired=DESIRED,
        provisioned_route=("p", "frame2", "host2", "vm2", 1, "fp"),
    )

    def signer(record):
        raise AssertionError("the api route never signs")

    head = adapter.admit("frame2", request, signer=signer, expected_revision="r7", deadline=1e12)
    assert head == "r8"
    (sent,) = calls
    assert (sent.method, sent.url.path) == ("POST", "/hq/lifecycle/v0/admissions/frame2")
    # A candidate admission names nothing but the head: the service promotes the committed
    # candidate and reads the desired policy and release from its own state.
    assert json.loads(sent.content) == {"kind": "candidate", "expected_revision": "r7"}
