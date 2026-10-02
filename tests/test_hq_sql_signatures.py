"""SQL carriers bind all FrameLease fields to the granted current signer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive.host_heartbeat_core import HeartbeatLease
from beadhive.hq_framelease_contracts import HeartbeatLease as NeutralHeartbeatLease
from beadhive.hq_sql_signatures import (
    SqlSignatureError,
    fingerprint,
    sign_heartbeat,
    sign_hive_request,
    verify_heartbeat,
    verify_hive_request,
)


def _key(tmp_path, name):
    private = Ed25519PrivateKey.generate()
    path = tmp_path / name
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode()
    return path, public


def test_sql_and_git_carriers_share_the_exact_framelease_class():
    assert HeartbeatLease is NeutralHeartbeatLease


def _lease(key_id):
    return HeartbeatLease(
        audience="fixture-fleet",
        frame_id="frame-1",
        holderIdentity="host-1",
        instance_ref="vm-1",
        key_id=key_id,
        epoch=3,
        config_revision="config-1",
        seq=1,
        renewTime=datetime.fromtimestamp(1000, UTC).isoformat(),
        state_seen="pending",
        release={"id": "fixture", "digest": "sha256:" + "1" * 64},
        report_digest="sha256:" + "2" * 64,
        conformance={"profile": "fixture", "status": "unknown", "checks": []},
    )


def test_sql_framelease_signature_binds_complete_envelope_and_current_key(tmp_path):
    key, public = _key(tmp_path, "frame-key")
    envelope = sign_heartbeat(_lease(fingerprint(public)), signing_key=str(key))
    verified, digest = verify_heartbeat(envelope, granted_public_key=public)
    assert verified.frame_id == "frame-1"
    assert digest.startswith("sha256:")

    changed = {**envelope, "metadata": {"name": "frame-2"}}
    with pytest.raises(SqlSignatureError):
        verify_heartbeat(changed, granted_public_key=public)
    forged = {**envelope, "spec": {**envelope["spec"], "config_revision": "config-2"}}
    with pytest.raises(SqlSignatureError):
        verify_heartbeat(forged, granted_public_key=public)
    _, revoked_public = _key(tmp_path, "replacement-key")
    with pytest.raises(SqlSignatureError):
        verify_heartbeat(envelope, granted_public_key=revoked_public)
    with pytest.raises(SqlSignatureError):
        sign_heartbeat(_lease(fingerprint(revoked_public)), signing_key=str(key))


def test_sql_hive_request_signature_binds_original_cas_and_request_id(tmp_path):
    key, public = _key(tmp_path, "frame-key")
    request = {
        "domain": "beadhive/sql-hive-lease/v1",
        "request_id": "00000000-0000-0000-0000-000000000001",
        "principal": "frame_a",
        "frame_id": "frame-1",
        "holder_identity": "host-1",
        "instance_ref": "vm-1",
        "epoch": 1,
        "key_fingerprint": fingerprint(public),
        "audience": "fixture-fleet",
        "config_revision": "config-1",
        "authority_revision": "a" * 32,
        "prefix": "fixture/hive",
        "expected_revision": "",
        "operation": "adopt",
        "force": False,
        "lease": {
            "host_id": "host-1", "label": "fixture", "epoch": 1,
            "adopted_at": "2026-10-02T00:00:00Z",
            "expires_at": "2026-10-02T00:30:00Z",
        },
    }
    envelope = sign_hive_request(request, signing_key=str(key))
    verified, digest = verify_hive_request(envelope, granted_public_key=public)
    assert verified == request
    assert len(digest) == 64
    with pytest.raises(SqlSignatureError):
        verify_hive_request(
            {**envelope, "request": {**request, "expected_revision": "changed"}},
            granted_public_key=public,
        )
