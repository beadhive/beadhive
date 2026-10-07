"""Operator-signed SQL mutations keep the signed authority life, with a 1 h floor (bh-oywx8)."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import hq_authority_guard as guard
from beadhive.host_heartbeat_core import ObservationAuthority
from beadhive.hq_control_plane import SqlControlPlane
from beadhive.hq_sql_signatures import fingerprint

NOW = 1_000_000.0
WEEK = 7 * 86400
CAPS = {
    "isolation": "container",
    "trust_zone": "self-hosted",
    "arch": "arm64",
    "harnesses": ["codex"],
    "max_sessions": 1,
}
RELEASE = {"id": "r1", "digest": "sha256:" + "1" * 64}


def _public():
    private = Ed25519PrivateKey.generate()
    return (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


def _record(*, state="active", cordoned=False, expiry=None):
    public = _public()
    return {
        "authority": {
            "frame_id": "frame",
            "holder_identity": str(uuid4()),
            "instance_ref": "vm",
            "key_fingerprint": fingerprint(public),
            "epoch": 1,
            "audience": "fleet",
            "config_revision": "cfg",
            "candidate_expires_at": expiry,
        },
        "public_key": public,
        "state": state,
        "desired": {"declared": True, "release": RELEASE, "caps": CAPS, "profile": "p"},
        "cordoned": cordoned,
        "drain_deadline": None,
        "receipt": {
            "sequence": 0,
            "sha": "",
            "first_seen": None,
            "consecutive": 0,
            "lease": None,
            "registration": None,
        },
    }


def _state(expires_at, record=None):
    return {
        "domain": guard.DOMAIN,
        "generation": "g",
        "revision": 4,
        "issued_at": NOW - 100,
        "expires_at": expires_at,
        "frames": {"frame": {"active": record, "candidate": None, "retired": [], "epoch_floor": 1}},
    }


class Operator:
    def __init__(self, state, evidence=None):
        self.state = state
        self.published = []
        self._evidence = evidence or (None, None, [])

    def load(self, **kwargs):
        return "head", copy.deepcopy(self.state), (), {}

    def evidence(self, *args, **kwargs):
        return self._evidence

    def publish(self, state, **kwargs):
        self.published.append(state)
        return "next"

    @staticmethod
    def principal_for(authority):
        return "principal"


def _plane(operator):
    plane = SqlControlPlane.__new__(SqlControlPlane)
    plane.clock = lambda: NOW
    plane._operator = lambda: operator
    plane._operator_deadline = lambda: 10**12
    plane.settings = {"runtime_operator_public_key": _public()}
    return plane


def _lifecycle(operator, verb, **extra):
    plane = _plane(operator)
    return plane.lifecycle(
        verb,
        "frame",
        "apply",
        expected="head",
        expected_host_id=operator.state["frames"]["frame"]["active"]["authority"][
            "holder_identity"
        ],
        expected_release="",
        operator_key="key",
        confirm=True,
        **extra,
    )


def _observe(operator):
    record = operator.state["frames"]["frame"]["active"]
    beat = SimpleNamespace(leaseDurationSeconds=1000, state_seen="drained")
    operator._evidence = ({**record, "state": "draining"}, None, [(1, "d", NOW - 1, beat)])
    _plane(operator).accept_observation("frame", expected="head", operator_key="key")


def _drive(path, expires_at):
    record = _record()
    if path == "resume":
        record["cordoned"] = True
    operator = Operator(_state(expires_at, record))
    if path in ("cordon", "resume"):
        _lifecycle(operator, path)
    elif path == "drain":
        _lifecycle(operator, "drain", deadline=NOW + 600)
    elif path == "observe":
        record["state"] = "draining"
        operator.state = _state(expires_at, record)
        _observe(operator)
    elif path == "grant":
        public = _public()
        authority = ObservationAuthority(
            "frame2", "host2", "vm2", fingerprint(public), 1, "fleet", "cfg", NOW + 600
        )
        _plane(operator).grant(
            authority,
            public,
            {"declared": True, "release": RELEASE, "caps": CAPS, "profile": "p"},
            expected="head",
            operator_key="key",
        )
    assert len(operator.published) == 1
    return operator.state, operator.published[0]


PATHS = ["grant", "observe", "cordon", "resume", "drain"]


@pytest.mark.parametrize("path", PATHS)
def test_long_authority_is_preserved_and_revision_bumps(path):
    before, after = _drive(path, NOW + WEEK)
    assert after["expires_at"] == NOW + WEEK
    assert after["revision"] == before["revision"] + 1
    assert after["issued_at"] == NOW


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("remaining", [1800, -50])
def test_short_or_lapsed_authority_keeps_the_one_hour_floor(path, remaining):
    _before, after = _drive(path, NOW + remaining)
    assert after["expires_at"] == NOW + 3600


def test_helper_never_shortens_and_floors_at_one_hour():
    assert guard.operator_signed_expiry({"expires_at": NOW + WEEK}, NOW) == NOW + WEEK
    assert guard.operator_signed_expiry({"expires_at": NOW + 5}, NOW) == NOW + 3600
