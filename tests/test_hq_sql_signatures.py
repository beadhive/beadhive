"""SQL carriers bind all FrameLease fields to the granted current signer."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive.host_heartbeat_core import HeartbeatLease, ObservationAuthority, _authority_matches
from beadhive.hq_framelease_contracts import DOMAIN_V2
from beadhive.hq_framelease_contracts import HeartbeatLease as NeutralHeartbeatLease
from beadhive.hq_sql_signatures import (
    SqlSignatureError,
    fingerprint,
    sign_heartbeat,
    sign_hive_request,
    sign_registration,
    verify_heartbeat,
    verify_hive_request,
    verify_registration,
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
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
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


def test_bound_v2_framelease_signs_hq_id_and_denies_foreign_authority(tmp_path):
    key, public = _key(tmp_path, "bound-frame")
    beadyard_id = str(uuid4())
    lease = HeartbeatLease.model_validate(
        {
            **_lease(fingerprint(public)).model_dump(mode="json", exclude_none=True),
            "domain": DOMAIN_V2,
            "beadyard_id": beadyard_id,
        }
    )
    envelope = sign_heartbeat(lease, signing_key=str(key))
    assert envelope["apiVersion"] == "frame.beadhive.ai/v1alpha2"
    assert verify_heartbeat(envelope, granted_public_key=public)[0] == lease
    with pytest.raises(SqlSignatureError):
        verify_heartbeat(
            {
                **envelope,
                "spec": {**envelope["spec"], "beadyard_id": str(uuid4())},
            },
            granted_public_key=public,
        )
    authority = ObservationAuthority(
        lease.frame_id,
        lease.holderIdentity,
        lease.instance_ref,
        lease.key_id,
        lease.epoch,
        lease.audience,
        lease.config_revision,
        beadyard_id=str(uuid4()),
    )
    assert not _authority_matches(lease, authority)
    with pytest.raises(ValueError, match="domain and beadyard"):
        HeartbeatLease.model_validate(
            {**lease.model_dump(mode="json"), "domain": "beadhive/frame-heartbeat/v1"}
        )


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
            "host_id": "host-1",
            "label": "fixture",
            "epoch": 1,
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
    bound = {
        **request,
        "domain": "beadhive/sql-hive-lease/v2",
        "beadyard_id": str(uuid4()),
    }
    bound_envelope = sign_hive_request(bound, signing_key=str(key))
    assert verify_hive_request(bound_envelope, granted_public_key=public)[0] == bound
    with pytest.raises(SqlSignatureError):
        verify_hive_request(
            {**bound_envelope, "request": {**bound, "beadyard_id": str(uuid4())}},
            granted_public_key=public,
        )
    with pytest.raises(SqlSignatureError, match="version and beadyard"):
        sign_hive_request({**bound, "domain": request["domain"]}, signing_key=str(key))


def test_sql_registration_v2_signs_beadyard_binding(tmp_path):
    key, public = _key(tmp_path, "registration-key")
    request = {
        "domain": "beadhive/sql-registration/v2",
        "beadyard_id": str(uuid4()),
        "key_fingerprint": fingerprint(public),
        "frame_id": "frame-1",
        "manifest": {"frame_id": "frame-1"},
    }
    signed = sign_registration(request, signing_key=str(key))
    assert verify_registration(signed, granted_public_key=public)[0] == request
    with pytest.raises(SqlSignatureError):
        verify_registration(
            {**signed, "request": {**request, "beadyard_id": str(uuid4())}},
            granted_public_key=public,
        )


def test_sql_principal_preserves_existing_incarnation_across_beadyard_binding():
    from beadhive.hq_sql_operator import SqlRuntimeOperator

    legacy = ObservationAuthority(
        "frame-1", "host-1", "vm-1", "SHA256:granted-key", 7, "fleet", "config"
    )
    original = SqlRuntimeOperator.principal_for(legacy)
    assert original.startswith("frame_")
    assert SqlRuntimeOperator.principal_for(replace(legacy, beadyard_id=str(uuid4()))) == original
    assert (
        SqlRuntimeOperator.principal_for(replace(legacy, instance_ref="another-incarnation"))
        != original
    )
