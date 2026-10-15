"""SQL admission accepts OpenSSH Ed25519 keys whose trailing comment has spaces (bh-k1jwq)."""

from __future__ import annotations

import copy
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import hq_authority_guard as guard
from beadhive.host_heartbeat_core import ObservationAuthority
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_sql_signatures import SqlSignatureError, fingerprint, public_key_problem

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
COMMENT = "Jane Doe's MacBook Pro (operator key)"


def _bare():
    return (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


@pytest.mark.parametrize(
    "suffix", ["", " host", " " + COMMENT, "\t" + COMMENT, "  two  spaces\t and tab  "]
)
def test_fingerprint_ignores_any_trailing_comment(suffix):
    bare = _bare()
    assert fingerprint(bare + suffix) == fingerprint(bare)
    assert public_key_problem(bare + suffix) is None


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen unavailable")
def test_sql_fingerprint_matches_ssh_keygen_for_a_multiword_comment():
    """The Git plane fingerprints with ssh-keygen; both planes must agree on the same key."""
    line = _bare() + " " + COMMENT
    assert fingerprint(line) == guard.fingerprint(line)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        (None, "not text"),
        ("ssh-rsa AAAAB3NzaC1yc2E " + COMMENT, "key type is not ssh-ed25519"),
        ("ssh-ed25519", "key data missing"),
        ("ssh-ed25519 !!!notbase64!!! " + COMMENT, "not a valid base64"),
        ('from="10.0.0.1" ssh-ed25519 AAAA ' + COMMENT, "key type is not ssh-ed25519"),
    ],
)
def test_invalid_keys_name_the_defect_without_echoing_key_or_comment(text, reason):
    assert reason in public_key_problem(text)
    with pytest.raises(SqlSignatureError) as excinfo:
        fingerprint(text)
    message = str(excinfo.value)
    assert reason in message
    assert "Jane" not in message and "AAAA" not in message


def test_multi_line_key_text_is_refused():
    bare = _bare()
    assert "single OpenSSH public-key line" in public_key_problem(bare + "\n" + _bare())


class _Operator:
    def __init__(self, state):
        self.state = state
        self.loads = 0
        self.published = []

    def load(self, **kwargs):
        self.loads += 1
        return "head", copy.deepcopy(self.state), (), {}

    def publish(self, state, **kwargs):
        self.published.append(state)
        return "next"


def _state():
    return {
        "domain": guard.DOMAIN,
        "generation": "g",
        "revision": 4,
        "issued_at": NOW - 100,
        "expires_at": NOW + 7200,
        "frames": {},
    }


def _plane(operator, operator_public):
    plane = SqlControlPlane.__new__(SqlControlPlane)
    plane.clock = lambda: NOW
    plane._operator = lambda: operator
    plane._operator_deadline = lambda: 10**12
    plane.settings = {"runtime_operator_public_key": operator_public}
    return plane


def _grant(operator, *, candidate, operator_public):
    authority = ObservationAuthority(
        "frame2", "host2", "vm2", "SHA256:x", 1, "fleet", "cfg", NOW + 600
    )
    if public_key_problem(candidate) is None:
        authority = ObservationAuthority(
            "frame2", "host2", "vm2", fingerprint(candidate), 1, "fleet", "cfg", NOW + 600
        )
    return _plane(operator, operator_public).grant(
        authority, candidate, DESIRED, expected="head", operator_key="key"
    )


def test_grant_admits_operator_and_candidate_keys_with_multiword_comments():
    operator = _Operator(_state())
    candidate = _bare() + " " + "frame two candidate (mac mini)"
    assert _grant(operator, candidate=candidate, operator_public=_bare() + " " + COMMENT) == "next"
    (published,) = operator.published
    record = published["frames"]["frame2"]["candidate"]
    assert record["public_key"] == candidate  # comment preserved verbatim
    assert record["authority"]["key_fingerprint"] == fingerprint(" ".join(candidate.split()[:2]))
    guard.validate_state(published)  # the guard's ssh-keygen fingerprint agrees


@pytest.mark.parametrize("which", ["operator", "candidate"])
def test_grant_names_the_invalid_key_before_any_read_or_mutation(which):
    operator = _Operator(_state())
    bad = "ssh-rsa AAAAB3NzaC1yc2E " + COMMENT
    candidate = bad if which == "candidate" else _bare() + " " + COMMENT
    operator_public = bad if which == "operator" else _bare() + " " + COMMENT
    with pytest.raises(ControlPlaneError) as excinfo:
        _grant(operator, candidate=candidate, operator_public=operator_public)
    message = str(excinfo.value)
    assert f"{which} public key" in message
    assert "key type is not ssh-ed25519" in message
    assert "unavailable" not in message
    assert "Jane" not in message and "AAAA" not in message
    assert operator.loads == 0 and operator.published == []


def test_keygen_comment_round_trip(tmp_path):
    """A key written by ssh-keygen -C with spaces fingerprints identically to its bare form."""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen unavailable")
    path = tmp_path / "id"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", COMMENT, "-f", str(path)],
        check=True,
    )
    line = (tmp_path / "id.pub").read_text()
    assert len(line.split()) > 3
    assert fingerprint(line) == fingerprint(" ".join(line.split()[:2]))
