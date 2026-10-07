"""Trusted authority mode verification (bh-mk97e).

A trusted frame skips the operator signature, authority expiry and config-head binding, and
accepts the unsigned-record marker; it still honours the content (admission state, cordon,
drain/retire, desired release, caps, beadyard identity), the identity comparisons and the replay
floor. The same scenarios on a signed frame fail closed exactly as in 0.23.1.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import test_emergency_sql_receiver as receiver_hq
import test_hq_authority_backend as git_hq
import test_hq_sql_signed_liveness as sql_hq
from beadhive import frame_eligibility as policy
from beadhive import gitref, hosts, hq_authority_enforce, hq_control_plane
from beadhive import hq_authority_guard as guard
from beadhive.host_heartbeat_core import HeartbeatLease, VerifiedObservation
from beadhive.hq_control_plane import ControlPlaneError
from beadhive.hq_sql_runtime import SqlRuntimeAuthority, SqlRuntimeError
from beadhive.hq_sql_signatures import SqlSignatureError, canonical, sign_authority

# Shared fixtures: the real SSH-signed Git HQ and the SQL frame/route world.
_prepared_hqs, backend, frame = git_hq._prepared_hqs, git_hq.backend, sql_hq.frame
apply, world = git_hq.apply, receiver_hq.world
_eligibility_plane, _hive_lease, _lease_plane = (
    sql_hq._eligibility_plane,
    sql_hq._hive_lease,
    sql_hq._lease_plane,
)

MODE_ENV = hq_authority_enforce.MODE_ENV
NOW = float(int(time.time()))
HEAD_CONFIG = "a" * 32


@pytest.fixture(autouse=True)
def _signed_baseline(monkeypatch):
    for name in (MODE_ENV, hq_authority_enforce.ENFORCE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hq_authority_enforce, "_banner_emitted", False)
    hq_authority_enforce.reset_cache()
    yield
    hq_authority_enforce.reset_cache()


def _mode(monkeypatch, mode):
    monkeypatch.setenv(MODE_ENV, mode)


# ---- the resolver surface -------------------------------------------------------------------


def test_unsigned_marker_shape():
    marker = hq_authority_enforce.UNSIGNED_SIGNATURE
    assert hq_authority_enforce.unsigned_signature(marker)
    assert hq_authority_enforce.unsigned_signature(marker.encode())
    assert hq_authority_enforce.unsigned_signature(memoryview(marker.encode()))
    assert not hq_authority_enforce.unsigned_signature("QUJD")  # base64 never contains ':'
    assert hq_authority_enforce.unsigned_commit("tree x\nauthor a\ncommitter c")
    assert not hq_authority_enforce.unsigned_commit("tree x\ngpgsig -----BEGIN SSH SIGNATURE-----")
    message = hq_authority_enforce.unsigned_rejection("HQ authority")
    assert "UNSIGNED" in message and "hq.authority_mode: trusted" in message
    assert "--operator-key" in message


def test_trusted_waives_no_content_but_deprecated_enforce_false_does(monkeypatch):
    assert hq_authority_enforce.enforced() and not hq_authority_enforce.content_waived()
    _mode(monkeypatch, "trusted")
    assert hq_authority_enforce.trusted() and not hq_authority_enforce.content_waived()
    monkeypatch.delenv(MODE_ENV)
    monkeypatch.setenv(hq_authority_enforce.ENFORCE_ENV, "false")
    assert hq_authority_enforce.trusted() and hq_authority_enforce.content_waived()


def test_configured_mode_is_memoized_on_settings_file_mtime(monkeypatch, tmp_path):
    settings = tmp_path / "operator.yaml"
    settings.write_text("hq:\n  authority_mode: signed\n")
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", str(settings))
    assert hq_authority_enforce.mode() == "signed"
    stamp = settings.stat().st_mtime_ns
    settings.write_text("hq:\n  authority_mode: trusted\n")
    os.utime(settings, ns=(stamp, stamp))
    assert hq_authority_enforce.mode() == "signed"  # same key: the per-process read is reused
    os.utime(settings, ns=(stamp + 10**9, stamp + 10**9))
    assert hq_authority_enforce.mode() == "trusted"  # mtime moved: re-read
    settings.write_text("hq:\n  authority_mode: signed\n")
    os.utime(settings, ns=(stamp + 10**9, stamp + 10**9))
    hq_authority_enforce.reset_cache()
    assert hq_authority_enforce.mode() == "signed"


def test_snapshot_validity_ignores_expiry_only_when_trusted(monkeypatch):
    assert hq_authority_enforce.snapshot_validity(NOW, NOW - 1, None) == NOW - 1
    assert hq_authority_enforce.snapshot_validity(NOW, NOW + 9, NOW + 5) == NOW + 5
    _mode(monkeypatch, "trusted")
    assert hq_authority_enforce.snapshot_validity(NOW, NOW - 1, None) > NOW
    # The candidate window is operator content and still bounds a pending incarnation.
    assert hq_authority_enforce.snapshot_validity(NOW, NOW - 1, NOW + 5) == NOW + 5


# ---- SQL authority row: signature, expiry, replay floor -------------------------------------


def _key(path):
    private = Ed25519PrivateKey.generate()
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    return (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


class _Cursor:
    def __init__(self, rows):
        self.rows, self.result = rows, None

    def execute(self, sql, params=None):
        if "FROM hq_authority AS OF" in sql:
            self.result = [self.rows[params[0]]]
        elif "HAS_ANCESTOR" in sql:
            self.result = [(1,)]
        else:  # pragma: no cover - a new query would need a fixture answer
            raise AssertionError(sql)

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0]


@pytest.fixture
def sql(tmp_path):
    operator, other = tmp_path / "operator", tmp_path / "other"
    public = _key(operator)
    _key(other)
    settings = {
        "runtime_backend_identity": "backend",
        "runtime_generation": "gen",
        "runtime_floor_path": str(tmp_path / "floor" / "runtime.json"),
        "runtime_initial_revision": "h0",
        "runtime_operator_public_key": public,
    }

    def row(revision, *, expires_at=NOW + 3600, signer=operator, signature=None):
        state = {
            "domain": guard.DOMAIN,
            "generation": "gen",
            "revision": revision,
            "issued_at": NOW - 100,
            "expires_at": expires_at,
            "frames": {},
        }
        policies = {}
        signed = {
            "backend_identity": "backend",
            "generation": "gen",
            "revision": revision,
            "config_backend": "config",
            "config_generation": "cgen",
            "config_head": HEAD_CONFIG,
            "state": state,
            "hive_policies": policies,
        }
        encoded, policy_encoded = canonical(state), canonical(policies)
        if signature is None:
            signature = sign_authority(signed, signing_key=str(signer))
        return (
            1,
            1,
            "backend",
            "gen",
            revision,
            "config",
            "cgen",
            HEAD_CONFIG,
            encoded,
            hashlib.sha256(encoded).hexdigest(),
            policy_encoded,
            hashlib.sha256(policy_encoded).hexdigest(),
            signature.encode() if isinstance(signature, str) else signature,
        )

    rows = {
        "valid": row(2),
        "expired": row(2, expires_at=NOW - 10),
        "bad-signature": row(2, signer=other),
        "unsigned": row(2, signature=hq_authority_enforce.UNSIGNED_SIGNATURE),
        "newer": row(5),
        "older": row(4),
    }
    authority = SqlRuntimeAuthority(settings, broker=object(), clock=lambda: NOW)
    return SimpleNamespace(authority=authority, cursor=_Cursor(rows))


def test_signed_sql_authority_fails_closed_as_before(sql):
    state, crossref, _ = sql.authority.verified_state_at(sql.cursor, "valid")
    assert state["revision"] == 2 and crossref[2] == HEAD_CONFIG
    with pytest.raises(SqlRuntimeError, match="expired or inconsistent"):
        sql.authority.verified_state_at(sql.cursor, "expired")
    with pytest.raises(SqlSignatureError, match="signature invalid"):
        sql.authority.verified_state_at(sql.cursor, "bad-signature")
    with pytest.raises(SqlRuntimeError, match="UNSIGNED.*hq.authority_mode: trusted"):
        sql.authority.verified_state_at(sql.cursor, "unsigned")


@pytest.mark.parametrize("head", ["valid", "expired", "bad-signature", "unsigned"])
def test_trusted_sql_authority_accepts_expired_bad_and_absent_signature(sql, monkeypatch, head):
    _mode(monkeypatch, "trusted")
    state, _, _ = sql.authority.verified_state_at(sql.cursor, head)
    assert state["revision"] == 2


@pytest.mark.parametrize("mode", ["signed", "trusted"])
def test_revision_rollback_is_refused_in_both_modes(sql, monkeypatch, mode):
    _mode(monkeypatch, mode)
    sql.authority.verified_state_at(sql.cursor, "newer")
    with pytest.raises(SqlRuntimeError, match="rollback"):
        sql.authority.verified_state_at(sql.cursor, "older")


@pytest.mark.parametrize("mode", ["signed", "trusted"])
def test_config_head_binding_only_in_signed(sql, monkeypatch, mode):
    _mode(monkeypatch, mode)
    moved = SimpleNamespace(backend_identity="config", generation="cgen", commit_revision="b" * 32)
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: moved)
    crossref = ("config", "cgen", HEAD_CONFIG)
    if mode == "signed":
        with pytest.raises(SqlRuntimeError, match="not bound"):
            sql.authority.bound_config_at(sql.cursor, crossref)
    else:
        assert sql.authority.bound_config_at(sql.cursor, crossref) == (moved, "b" * 32)


# ---- SQL plane reads ------------------------------------------------------------------------


def test_trusted_eligibility_read_tolerates_a_frame_relevant_config_change(frame, monkeypatch):
    unbound = {"bh": {"config_revision": "other-revision", "valid_until": NOW + 3600}}
    plane, manifest, calls = _eligibility_plane(frame, frame.record, unbound)
    with pytest.raises(ControlPlaneError, match="differs from current grant"):
        plane.read_eligibility(manifest)
    _mode(monkeypatch, "trusted")
    head, desired, observation = plane.read_eligibility(manifest)
    assert head == "head" and desired["state"] == "active" and observation.verified
    assert [call["enforce"] for call in calls] == [True, False]
    # Identity is not authority: a foreign incarnation is still refused.
    with pytest.raises(ControlPlaneError, match="differs from current grant"):
        plane.read_eligibility(SimpleNamespace(**{**vars(manifest), "instance_ref": "vm-2"}))
    with pytest.raises(ControlPlaneError, match="differs from current grant"):
        plane.read_eligibility(SimpleNamespace(**{**vars(manifest), "host_id": "host-2"}))


def _expired_policy_plane(frame, lease):
    plane = _lease_plane(frame, {}, lease)
    composite = plane._runtime_authority().read_frame_composite()
    expired = {"bh": {"config_revision": "desired-1", "valid_until": NOW - 1}}
    composite = (*composite[:6], expired, *composite[7:])
    plane._runtime_authority = lambda: SimpleNamespace(
        read_frame_composite=lambda prefix=None, **_: composite
    )
    return plane


def test_trusted_hive_lease_tolerates_policy_expiry_but_honours_cordon(frame, monkeypatch):
    lease = _hive_lease(expires=NOW + 3600)
    plane = _expired_policy_plane(frame, lease)
    assert plane.read_hive_lease_record("bh", holder_identity="host-1") == ("revision-1", None)
    _mode(monkeypatch, "trusted")
    assert plane.read_hive_lease_record("bh", holder_identity="host-1") == ("revision-1", lease)
    cordoned = SimpleNamespace(**{**vars(frame), "record": {**frame.record, "cordoned": True}})
    held = _lease_plane(cordoned, {}, lease)
    assert held.read_hive_lease_record("bh", holder_identity="host-1") == ("revision-1", None)
    parked = SimpleNamespace(**{**vars(frame), "record": {**frame.record, "state": "parked"}})
    held = _lease_plane(parked, {}, lease)
    assert held.read_hive_lease_record("bh", holder_identity="host-1") == ("revision-1", None)
    foreign = _lease_plane(frame, {}, _hive_lease("host-2", expires=NOW + 3600))
    assert foreign.read_hive_lease_record("bh", holder_identity="host-1")[1] is None


# ---- HQ receiver: hive-lease acceptance --------------------------------------------------------


@pytest.mark.parametrize("mode", ["signed", "trusted"])
@pytest.mark.parametrize("case", ["config-head", "policy-expired"])
def test_receiver_binding_and_expiry_only_in_signed(world, monkeypatch, mode, case):
    from beadhive import hq_sql_receiver

    _mode(monkeypatch, mode)
    if case == "config-head":  # a frame-relevant config edit: the projection no longer matches
        monkeypatch.setattr(hq_sql_receiver, "project_hive_policies", lambda *a, **k: {})
    else:
        policies = world.receiver.authority.verified_state_at()[2]
        policies["bh"]["valid_until"] = NOW - 1
    if mode == "signed":
        with pytest.raises(hq_sql_receiver.ReceiverError, match="canonical catalog|expired"):
            world.receiver.accept_hive_lease("frame", "request")
        assert not world.committed
    else:
        assert world.receiver.accept_hive_lease("frame", "request") and world.committed


@pytest.mark.parametrize("mode", ["signed", "trusted"])
@pytest.mark.parametrize("case", ["cordoned", "parked", "scope"])
def test_receiver_honours_cordon_state_and_hive_scope(world, monkeypatch, mode, case):
    from beadhive import hq_sql_receiver

    _mode(monkeypatch, mode)
    if case == "scope":
        world.request["prefix"] = "other"
    elif case == "cordoned":
        world.record["cordoned"] = True
    else:
        world.record["state"] = "parked"
    with pytest.raises(hq_sql_receiver.ReceiverError):
        world.receiver.accept_hive_lease("frame", "request")
    assert not world.committed


# ---- eligibility honours the authority's content in both modes -------------------------------

RELEASE = {"id": "release", "digest": "sha256:" + "1" * 64}
CAPS = dict(
    isolation="kvm", trust_zone="self-hosted", arch="x86_64", harnesses=["codex"], max_sessions=2
)
DESIRED = dict(
    declared=True,
    state="active",
    cordoned=False,
    release=RELEASE,
    caps=CAPS,
    profile="profile",
    authority={"holder_identity": "host", "instance_ref": "vm", "beadyard_id": None},
)


def _frame():
    return hosts.HostManifest(
        host_id="host",
        frame_id="frame",
        state="active",
        release=RELEASE,
        capabilities=CAPS,
        instance_ref="vm",
        label="fixture",
        os="linux",
        arch="x86_64",
        role="executor",
        identity={"kind": "none"},
    )


def _observation():
    lease = HeartbeatLease(
        frame_id="frame",
        holderIdentity="host",
        instance_ref="vm",
        key_id="key",
        epoch=9,
        audience="fleet",
        config_revision="config",
        seq=1,
        renewTime=datetime.now(UTC).isoformat(),
        release=RELEASE,
        state_seen="active",
        conformance={
            "profile": "profile",
            "status": "conformant",
            "checks": [{"id": "required", "status": "pass"}],
        },
        report_digest="sha256:" + "2" * 64,
    )
    return VerifiedObservation("fresh", True, True, 1.0, lease)


@pytest.mark.parametrize("mode", ["signed", "trusted"])
@pytest.mark.parametrize(
    "change,predicate",
    [
        ({}, None),
        ({"cordoned": True}, "not_cordoned"),
        ({"state": "draining"}, "admitted_active"),
        ({"state": "parked"}, "admitted_active"),
        ({"state": "retired"}, "admitted_active"),
        ({"release": {"id": "other", "digest": "sha256:" + "3" * 64}}, "release_matches"),
        ({"caps": {**CAPS, "max_sessions": 9}}, "capabilities_match_admission"),
        ({"profile": "other-profile"}, "conformance_pass"),
        (
            {"authority": {**DESIRED["authority"], "beadyard_id": "foreign"}},
            "beadyard_binding",
        ),
        (
            {"authority": {**DESIRED["authority"], "holder_identity": "other"}},
            "current_frame_incarnation",
        ),
    ],
)
def test_eligibility_honours_authority_content(monkeypatch, tmp_path, mode, change, predicate):
    _mode(monkeypatch, mode)
    desired = {**DESIRED, **change}
    plane = SimpleNamespace(
        read_eligibility=lambda manifest, now=None: ("head", desired, _observation())
    )
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda *a, **k: plane)
    facts = policy.load_facts(_frame(), hq_dir=tmp_path, cfg={})
    assert facts.available and not facts.authority_waived
    decision = policy.eligible(_frame(), {}, facts)
    assert decision.waived == () and not decision.enforcement_disabled
    if predicate is None:
        assert decision.allowed
    else:
        assert not decision.allowed and predicate in decision.reason.split(", ")


# ---- Git control plane: real SSH-signed HQ ----------------------------------------------------


def _remote_git(b, *args, data=None):
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=trusted",
            "-c",
            "user.email=trusted@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=b["remote"],
        input=data,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _unsigned_revision(b):
    """Publish the next authority revision as an UNSIGNED Git record (the bh-mk97e shape),
    bypassing the server hook the way a key-less trusted publisher's HQ would accept it."""
    sha, state, _ = b["plane"]._read()
    state = {**state, "revision": state["revision"] + 1, "issued_at": time.time()}
    blob = _remote_git(b, "hash-object", "-w", "--stdin", data=gitref.encode(state))
    tree = _remote_git(b, "mktree", data=f"100644 blob {blob}\tauthority.json\n")
    message = (
        f"HQ authority revision {state['revision']}\n\n{hq_authority_enforce.UNSIGNED_TRAILER}\n"
    )
    commit = _remote_git(b, "commit-tree", tree, "-p", sha, data=message)
    _remote_git(b, "update-ref", guard.HEAD, commit)
    _remote_git(b, "update-ref", f"{guard.WITNESS}{state['revision']:020d}", commit)
    return commit


def test_git_trusted_frame_reads_expired_authority_signed_fails_closed(backend, monkeypatch):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    expired, state, _ = b["plane"]._read()
    # Two hours on: past the granted authority's expiry, without racing a 1 s readback.
    b["plane"].clock = lambda: time.time() + 7200
    assert b["plane"].clock() >= state["expires_at"]
    with pytest.raises(ControlPlaneError, match="expired"):
        b["plane"].read_eligibility(b["manifest"])
    _mode(monkeypatch, "trusted")
    revision, desired, _observation = b["plane"].read_eligibility(b["manifest"])
    assert revision == expired and desired["state"] == "active" and not desired["cordoned"]
    assert b["plane"].eligibility_authority_status()["revision"] == expired


def test_git_unsigned_record_trusted_accepts_signed_rejects_actionably(backend, monkeypatch):
    b = backend
    unsigned = _unsigned_revision(b)
    with pytest.raises(ControlPlaneError, match="UNSIGNED.*hq.authority_mode: trusted"):
        b["plane"]._read()
    _mode(monkeypatch, "trusted")
    sha, state, _ = b["plane"]._read()
    assert sha == unsigned and state["frames"]["frame-one"]["candidate"] is not None


@pytest.mark.parametrize("mode", ["signed", "trusted"])
def test_git_rollback_and_cordon_are_honoured_in_both_modes(backend, monkeypatch, mode):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    apply(b, "cordon")
    _mode(monkeypatch, mode)
    _revision, desired, _observation = b["plane"].read_eligibility(b["manifest"])
    assert desired["cordoned"] is True
    current, _, _ = b["plane"]._read()
    parent = _remote_git(b, "rev-parse", f"{current}^")
    _remote_git(b, "update-ref", guard.HEAD, parent)
    with pytest.raises(ControlPlaneError, match="rollback"):
        b["plane"]._read()
