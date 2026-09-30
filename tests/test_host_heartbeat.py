"""U2 real SSH signatures and Git refs; never contact operator HQ."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from beadhive import host_heartbeat as hb
from beadhive import hosts


def git(repo, *args, data=None):
    result = subprocess.run(["git", *args], cwd=repo, input=data, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "hq"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Frame test")
    git(repo, "config", "user.email", "frame@example.invalid")
    git(repo, "remote", "add", "origin", str(remote))
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    public = key.with_suffix(".pub").read_text()
    (repo / "allowed_signers").write_text("frame@example.invalid " + public)
    fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", str(key.with_suffix(".pub"))],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[1]
    manifest = hosts.HostManifest(
        host_id="host-1",
        frame_id="frame-1",
        label="test",
        instance_ref="vm-1",
        os="linux",
        arch="x86_64",
        role="executor",
        identity={"kind": "none"},
    )
    authority = hb.ObservationAuthority(
        "frame-1", "host-1", "vm-1", fingerprint, 2, "fleet-test", "rev-1"
    )
    directory = repo / "heartbeat-authorities"
    directory.mkdir()
    (directory / "frame-1.json").write_text(json.dumps(authority.__dict__))

    def lease(at=1000, seq=1, **changes):
        data = dict(
            audience="fleet-test",
            frame_id="frame-1",
            holderIdentity="host-1",
            instance_ref="vm-1",
            key_id=fingerprint,
            epoch=2,
            config_revision="rev-1",
            seq=seq,
            renewTime=datetime.fromtimestamp(at, UTC).isoformat(),
            state_seen="active",
            release={"id": "test", "digest": "sha256:" + "1" * 64},
            report_digest="sha256:" + "2" * 64,
            conformance={"profile": "test", "status": "unknown", "checks": []},
        )
        return hb.HeartbeatLease(**{**data, **changes})

    observer_clock = [1000]
    receipts = {}

    def trusted_provider(hq_dir, identity):
        # Contract fixture for a separate authority/observer service, not local SQLite.
        auth = hb.load_authority(hq_dir, identity)
        if auth is None:
            return None
        previous = receipts.get(identity)
        if previous is None or previous.authority != auth:
            previous = hb.AuthoritySnapshot(auth, 0, 1000000, 0, "", None)
        sha = git(remote, "rev-parse", hb.ref_name(identity))
        record = hb.HeartbeatLease.model_validate_json(git(repo, "show", f"{sha}:heartbeat.json"))
        try:
            hb.framelease_envelope(repo, sha, hb._trust_path(repo, auth))
        except hb.HeartbeatError:
            return previous
        if hb._authority_matches(record, auth) and record.seq > previous.sequence:
            first_seen = min(observer_clock[0], record.observed_at)
            previous = hb.AuthoritySnapshot(auth, first_seen, 1000000, record.seq, sha, first_seen)
        receipts[identity] = previous
        return previous

    monkeypatch.setattr(hb, "load_trusted_authority", trusted_provider)

    def observe(at=1000, **kwargs):
        observer_clock[0] = at
        return hb.observe(repo, manifest, now=at, observer_dir=tmp_path / "observer", **kwargs)

    return repo, key, remote, lease, observe, authority, manifest


def test_signed_parentless_refs_never_change_main(fleet):
    repo, key, remote, lease, observe, *_ = fleet
    git(repo, "commit", "--allow-empty", "-m", "main baseline")
    git(repo, "push", "origin", "HEAD:main")
    original = git(remote, "rev-parse", "main")
    first = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    assert observe().fresh
    second = hb.publish(repo, lease(1010, 2), signing_key=str(key), now=1010)
    assert first != second
    assert git(remote, "rev-list", "--count", hb.ref_name("frame-1")) == "1"
    assert git(remote, "rev-parse", "main") == original
    assert observe(1010).status == "verified"
    with pytest.raises(hb.HeartbeatError, match="sequence"):
        hb.publish(repo, lease(1010, 2), signing_key=str(key), now=1010)


def test_repeat_poll_does_not_refresh_age_and_restart_retains_high_water(fleet):
    repo, key, _, lease, observe, *_ = fleet
    first = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    assert observe().age_seconds == 0
    assert observe(1299).fresh
    assert observe(1300).status == "stale"
    second = hb.publish(repo, lease(1310, 2), signing_key=str(key), now=1000)
    assert observe(1310).fresh
    git(repo, "push", "--force", "origin", f"{first}:{hb.ref_name('frame-1')}")
    assert observe(1311).status == "replay"
    git(repo, "push", "--force", "origin", f"{second}:{hb.ref_name('frame-1')}")
    assert observe(1312).fresh


@pytest.mark.parametrize(
    "delta,expected",
    [(-31, "clock-skew"), (-30, "verified"), (30, "verified"), (31, "verified"), (300, "stale")],
)
def test_clock_bounds_and_stale(fleet, delta, expected):
    repo, key, _, lease, observe, *_ = fleet
    hb.publish(repo, lease(1000 - delta), signing_key=str(key), now=1000)
    assert observe().status == expected


@pytest.mark.parametrize(
    "changes",
    [dict(leaseDurationSeconds=179), dict(leaseDurationSeconds=901), dict(intervalSeconds=0)],
)
def test_ttl_rejects_out_of_bounds(fleet, changes):
    with pytest.raises(ValueError):
        fleet[3](**changes)


def raw_commit(repo, lease, *, key=None, parent=None):
    blob = git(repo, "hash-object", "-w", "--stdin", data=lease.model_dump_json(exclude_none=True))
    tree = git(repo, "mktree", data=f"100644 blob {blob}\theartbeat.json\n")
    args = ["commit-tree", tree]
    if key:
        args = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", *args, "-S"]
    if parent:
        args += ["-p", parent]
    sha = git(repo, *args, data="test beat\n")
    git(repo, "push", "--force", "origin", f"{sha}:{hb.ref_name('frame-1')}")
    return sha


def test_unsigned_and_unknown_key(fleet, tmp_path):
    repo, _, _, lease, observe, *_ = fleet
    raw_commit(repo, lease())
    assert observe().status == "unsigned"
    unknown = tmp_path / "unknown"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(unknown)], check=True)
    raw_commit(repo, lease(), key=unknown)
    assert observe().status == "unknown-key"


@pytest.mark.parametrize(
    "changes",
    [
        dict(epoch=1),
        dict(instance_ref="cloned-vm"),
        dict(audience="other-fleet"),
        dict(holderIdentity="other-host"),
        dict(config_revision="old-rev"),
        dict(key_id="wrong-key"),
    ],
)
def test_binding_mismatch(fleet, changes):
    repo, key, _, lease, observe, *_ = fleet
    hb.publish(repo, lease(**changes), signing_key=str(key), now=1000)
    assert observe().status == "identity-mismatch"


def test_candidate_is_separately_scoped_and_never_admitted(fleet):
    repo, key, _, lease, observe, authority, _ = fleet
    (repo / "heartbeat-authorities" / "frame-1.json").unlink()
    directory = repo / "heartbeat-candidates"
    directory.mkdir()
    candidate = replace(authority, candidate_expires_at=1100)
    (directory / "frame-1.json").write_text(json.dumps(candidate.__dict__))
    (repo / "candidate_signers").write_text((repo / "allowed_signers").read_text())
    (repo / "allowed_signers").unlink()
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    result = observe()
    assert result.verified and result.fresh and result.candidate
    assert observe(1100).status == "candidate-expired"


def test_bad_authority_fails_closed(fleet):
    repo, key, _, lease, observe, *_ = fleet
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    (repo / "heartbeat-authorities" / "frame-1.json").write_text('{"epoch": "bad"}')
    assert not observe().fresh


def test_clock_rollback_fails_closed(fleet):
    repo, key, _, lease, observe, *_ = fleet
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    assert observe().fresh
    assert observe(999).status == "replay"


def test_list_invalid_beat_never_uses_manifest_mtime(fleet, monkeypatch):
    from beadhive import host_cli

    repo, key, _, lease, _, _, manifest = fleet
    hosts.save(repo, manifest)
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    monkeypatch.setattr(host_cli.time, "time", lambda: 1000)
    monkeypatch.setattr(hb.config, "home", lambda: repo.parent / "bh-home")
    result = host_cli.list_payload(repo, {})[0]
    assert result["liveness_source"] == "signed-heartbeat"
    assert result["heartbeat_status"] == "verified"
    assert result["heartbeat_age"] == 0
    raw_commit(repo, lease(1001, 2))
    result = host_cli.list_payload(repo, {})[0]
    assert result["stale"] == "stale"
    assert result["heartbeat_status"] == "unsigned"
    assert result["last_seen"] == ""


def test_legacy_fallback_is_labeled(tmp_path):
    from beadhive import host_cli

    manifest = hosts.HostManifest(
        host_id="legacy",
        label="legacy",
        os="linux",
        arch="x86_64",
        role="viewer",
        identity={"kind": "none"},
    )
    hosts.save(tmp_path, manifest)
    row = host_cli.list_payload(tmp_path, {})[0]
    assert row["liveness_source"] == "legacy-mtime"
    assert row["last_seen"]
    assert row["heartbeat_status"] == "absent"


def test_same_sequence_with_changed_signed_bytes_is_replay(fleet):
    repo, key, _, lease, observe, *_ = fleet
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    assert observe().fresh
    raw_commit(repo, lease(1001), key=key)
    assert observe(1001).status == "replay"


def test_parented_commit_is_rejected(fleet):
    repo, key, _, lease, observe, *_ = fleet
    parent = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    raw_commit(repo, lease(1001, 2), key=key, parent=parent)
    assert observe(1001).status == "invalid"


def test_delayed_first_poll_retains_sender_ttl_bound(fleet):
    repo, key, _, lease, observe, *_ = fleet
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    delayed = observe(1060)
    assert delayed.fresh and delayed.age_seconds == 60
    assert observe(1299).fresh
    assert observe(1300).status == "stale"


def test_revoked_key_replacement_requires_current_authority(fleet, tmp_path):
    repo, key, remote, lease, observe, authority, manifest = fleet
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    assert observe().fresh
    replacement = tmp_path / "replacement-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(replacement)], check=True
    )
    fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", str(replacement.with_suffix(".pub"))],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[1]
    (repo / "allowed_signers").write_text(
        "frame@example.invalid " + replacement.with_suffix(".pub").read_text()
    )
    new_authority = replace(
        authority,
        holder_identity="host-new",
        instance_ref="vm-new",
        key_fingerprint=fingerprint,
        epoch=3,
    )
    (repo / "heartbeat-authorities" / "frame-1.json").write_text(json.dumps(new_authority.__dict__))
    new_lease = lease(holderIdentity="host-new", instance_ref="vm-new", key_id=fingerprint, epoch=3)
    with pytest.raises(hb.HeartbeatError):
        hb.publish(repo, new_lease, signing_key=str(key), now=1000)
    hb.publish(repo, new_lease, signing_key=str(replacement), now=1000)
    new_manifest = manifest.model_copy(update={"host_id": "host-new", "instance_ref": "vm-new"})
    result = hb.observe(repo, new_manifest, now=1000, observer_dir=tmp_path / "observer")
    assert result.fresh
    assert git(remote, "rev-list", "--count", hb.ref_name("frame-1")) == "1"


def test_unknown_key_cannot_overwrite_without_matching_authority(fleet, tmp_path):
    repo, key, remote, lease, _, _, _ = fleet
    unknown = tmp_path / "unknown-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(unknown)], check=True)
    original = raw_commit(repo, lease(), key=unknown)
    with pytest.raises(hb.HeartbeatError, match="current.*authority"):
        hb.publish(
            repo,
            lease(seq=2, epoch=3, holderIdentity="forged-host"),
            signing_key=str(key),
            now=1000,
        )
    assert git(remote, "rev-parse", hb.ref_name("frame-1")) == original
    (repo / "heartbeat-authorities" / "frame-1.json").unlink()
    with pytest.raises(hb.HeartbeatError, match="current.*authority"):
        hb.publish(repo, lease(seq=2, epoch=3), signing_key=str(unknown), now=1000)
    assert git(remote, "rev-parse", hb.ref_name("frame-1")) == original


def test_identity_change_requires_epoch_advance_even_if_authority_changes(fleet, tmp_path):
    repo, key, _, lease, observe, authority, manifest = fleet
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    assert observe().fresh
    changed = lease(1001, 2, holderIdentity="host-new", instance_ref="vm-new")
    with pytest.raises(hb.HeartbeatError, match="current authority"):
        hb.publish(repo, changed, signing_key=str(key), now=1000)
    new_authority = replace(authority, holder_identity="host-new", instance_ref="vm-new")
    path = repo / "heartbeat-authorities" / "frame-1.json"
    path.write_text(json.dumps(new_authority.__dict__))
    new_manifest = manifest.model_copy(update={"host_id": "host-new", "instance_ref": "vm-new"})
    raw_commit(repo, changed, key=key)
    result = hb.observe(repo, new_manifest, now=1001, observer_dir=tmp_path / "observer")
    assert result.status == "replay"
    path.write_text(json.dumps(replace(new_authority, epoch=3).__dict__))
    raw_commit(
        repo, lease(1002, 1, epoch=3, holderIdentity="host-new", instance_ref="vm-new"), key=key
    )
    assert hb.observe(repo, new_manifest, now=1002, observer_dir=tmp_path / "observer").fresh


def test_unknown_key_cannot_replace_trusted_beat(fleet, tmp_path):
    repo, key, remote, lease, _, _, _ = fleet
    original = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    unknown = tmp_path / "unapproved-replacement"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(unknown)], check=True)
    with pytest.raises(hb.HeartbeatError):
        hb.publish(repo, lease(1001, 2), signing_key=str(unknown), now=1000)
    assert git(remote, "rev-parse", hb.ref_name("frame-1")) == original


def test_local_inventory_is_not_current_authority(fleet, monkeypatch):
    repo, key, _, lease, observe, *_ = fleet
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    monkeypatch.setattr(hb, "load_trusted_authority", lambda *_: None)
    result = observe()
    assert result.status == "authority-unavailable"
    assert result.verified and not result.fresh


def test_deleted_observer_state_cannot_replay_below_authoritative_floor(fleet, tmp_path):
    repo, key, _, lease, observe, *_ = fleet
    old = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    assert observe().fresh
    hb.publish(repo, lease(1010, 2), signing_key=str(key), now=1010)
    assert observe(1010).fresh
    for store in (tmp_path / "observer").glob("*.sqlite3"):
        store.unlink()
    git(repo, "push", "--force", "origin", f"{old}:{hb.ref_name('frame-1')}")
    assert observe(1011).status == "replay"


def test_restore_uses_trusted_first_observation_not_new_local_time(fleet, tmp_path):
    repo, key, _, lease, observe, authority, _ = fleet
    sha = hb.publish(repo, lease(1030), signing_key=str(key), now=1000)
    receipt = hb.AuthoritySnapshot(authority, 1000, 2000, 1, sha, 1000)
    assert observe(trusted_authority_lookup=lambda *_: receipt).fresh
    for store in (tmp_path / "observer").glob("*.sqlite3"):
        store.unlink()
    assert observe(1300, trusted_authority_lookup=lambda *_: receipt).status == "stale"


def test_expired_authority_snapshot_cannot_make_evidence_fresh(fleet):
    repo, key, _, lease, observe, authority, _ = fleet
    sha = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    receipt = hb.AuthoritySnapshot(authority, 990, 1000, 1, sha, 990)
    result = observe(trusted_authority_lookup=lambda *_: receipt)
    assert result.status == "authority-unavailable" and not result.fresh


def test_new_bytes_require_trusted_first_observation_receipt(fleet):
    repo, key, _, lease, observe, authority, _ = fleet
    sha = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    receipt = hb.AuthoritySnapshot(authority, 1000, 2000, 1, sha, 1000)
    hb.publish(repo, lease(1010, 2), signing_key=str(key), now=1010)
    result = observe(1010, trusted_authority_lookup=lambda *_: receipt)
    assert result.status == "authority-unavailable" and not result.fresh


def test_framelease_git_adapter_round_trip(fleet):
    repo, key, _, lease, *_ = fleet
    sha = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    envelope = hb.framelease_envelope(repo, sha, repo / "allowed_signers")
    assert envelope["spec"]["signature"]["scope"] == "git-commit"
    assert envelope["spec"]["signature"]["signedObject"] == sha
    assert hb.verify_framelease_envelope(repo, envelope, repo / "allowed_signers") == lease()


@pytest.mark.parametrize(
    "field,value",
    [
        ("holderIdentity", "host-forged"),
        ("frame_id", "other-frame"),
        ("epoch", 100),
        ("seq", 100),
        ("leaseDurationSeconds", 900),
        ("renewTime", "2030-01-01T00:00:00Z"),
    ],
)
def test_framelease_adapter_rejects_unsigned_changes(fleet, field, value):
    repo, key, _, lease, *_ = fleet
    sha = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    envelope = hb.framelease_envelope(repo, sha, repo / "allowed_signers")
    envelope["spec"][field] = value
    with pytest.raises(hb.HeartbeatError, match="authenticated Git payload"):
        hb.verify_framelease_envelope(repo, envelope, repo / "allowed_signers")


def test_canonical_frame_schema_pin_and_authoritative_fixture():
    import hashlib
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker

    root = Path(hb.__file__).parent / "schemas/frame/v1alpha1"
    schema_bytes = (root / "framelease.schema.json").read_bytes()
    pin = json.loads((root / "provenance.json").read_text())
    assert hashlib.sha256(schema_bytes).hexdigest() == pin["sha256"]
    fixture_bytes = (root / "framelease-positive.json").read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == pin["fixture_sha256"]
    fixture = json.loads(fixture_bytes)
    Draft202012Validator(json.loads(schema_bytes), format_checker=FormatChecker()).validate(fixture)
    spec = {k: v for k, v in fixture["spec"].items() if k != "signature"}
    assert hb.HeartbeatLease.model_validate(spec).frame_id == fixture["metadata"]["name"]


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("metadata", "name", "other-frame"),
        ("signature", "keyId", "other-key"),
        ("signature", "algorithm", "ed25519"),
        ("signature", "scope", "payload"),
        ("signature", "value", "ZmFrZQ=="),
    ],
)
def test_framelease_adapter_rejects_provenance_changes(fleet, target, field, value):
    repo, key, _, lease, *_ = fleet
    sha = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    envelope = hb.framelease_envelope(repo, sha, repo / "allowed_signers")
    section = envelope["metadata"] if target == "metadata" else envelope["spec"]["signature"]
    section[field] = value
    with pytest.raises(hb.HeartbeatError, match="authenticated Git payload"):
        hb.verify_framelease_envelope(repo, envelope, repo / "allowed_signers")


def test_signed_frame_roster_does_not_read_manifest_mtime(fleet, monkeypatch):
    from beadhive import host_cli

    repo, key, _, lease, _, _, manifest = fleet
    hosts.save(repo, manifest)
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    monkeypatch.setattr(host_cli.time, "time", lambda: 1000)
    monkeypatch.setattr(hb.config, "home", lambda: repo.parent / "bh-home")

    def no_mtime(_):
        raise AssertionError("signed frame row read legacy mtime")

    monkeypatch.setattr(host_cli, "_last_seen", no_mtime)
    assert host_cli.list_payload(repo, {})[0]["last_seen"] == lease().renewTime


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": -1},
        {"sha": "not-a-sha"},
        {"checked_at": float("nan")},
        {"first_seen": 1001},
        {"valid_until": 1000},
    ],
)
def test_authority_receipt_rejects_invalid_reconstruction(fleet, changes):
    _, _, _, _, _, authority, _ = fleet
    fields = dict(
        authority=authority,
        checked_at=1000,
        valid_until=2000,
        sequence=1,
        sha="a" * 40,
        first_seen=1000,
    )
    with pytest.raises(hb.HeartbeatError):
        hb.AuthoritySnapshot(**(fields | changes))


def test_unknown_key_high_epoch_does_not_poison_authority(fleet, tmp_path):
    repo, key, remote, lease, _, authority, _ = fleet
    unknown = tmp_path / "poison-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(unknown)], check=True)
    accepted = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    raw_commit(repo, lease(epoch=999999, seq=999999), key=unknown)
    receipt = hb.AuthoritySnapshot(authority, 1000, 2000, 1, accepted, 1000)
    current = hb.publish(
        repo,
        lease(seq=2),
        signing_key=str(key),
        now=1000,
        trusted_authority_lookup=lambda *_: receipt,
    )
    assert git(remote, "rev-parse", hb.ref_name("frame-1")) == current
    assert json.loads(git(repo, "show", f"{current}:heartbeat.json"))["epoch"] == authority.epoch


def test_untrusted_replacement_requires_current_trusted_authority(fleet, tmp_path):
    repo, key, remote, lease, _, _, _ = fleet
    unknown = tmp_path / "poison-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(unknown)], check=True)
    poison = raw_commit(repo, lease(epoch=999999), key=unknown)
    with pytest.raises(hb.HeartbeatError, match="current trusted authority unavailable"):
        hb.publish(
            repo, lease(), signing_key=str(key), now=1000, trusted_authority_lookup=lambda *_: None
        )
    assert git(remote, "rev-parse", hb.ref_name("frame-1")) == poison


@pytest.mark.parametrize(
    "now,status,age", [(1060, "authority-unavailable", 60), (1300, "stale", 300)]
)
def test_default_cli_diagnostic_age_without_authority(fleet, monkeypatch, now, status, age):
    from typer.testing import CliRunner

    from beadhive import host_cli

    repo, key, _, lease, _, _, manifest = fleet
    hosts.save(repo, manifest)
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    # Exercise the real default unavailable production binding, not a receipt fixture.
    monkeypatch.setattr(hb, "load_trusted_authority", lambda *_: None)
    monkeypatch.setattr(host_cli.time, "time", lambda: now)
    monkeypatch.setattr(host_cli.config, "hq_dir", lambda: repo)
    monkeypatch.setattr(host_cli.config, "load", lambda: {})
    row = host_cli.list_payload(repo, {})[0]
    assert row["heartbeat_status"] == status
    assert row["heartbeat_verified"] is True
    assert row["heartbeat_age"] == age
    assert row["heartbeat_age_basis"] == "sender-diagnostic"
    assert row["stale"] == "stale"
    result = CliRunner().invoke(host_cli.app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["heartbeat_age_basis"] == "sender-diagnostic"
    table = CliRunner().invoke(host_cli.app, ["list"])
    assert table.exit_code == 0, table.output
    assert "AGE_BASIS" in table.output and "sender-diagnostic" in table.output


@pytest.mark.parametrize(
    "changes",
    [
        {"epoch": 999999, "seq": 999999},
        {"holderIdentity": "ungranted-host", "instance_ref": "ungranted-vm", "seq": 999999},
    ],
)
def test_known_signer_ungranted_metadata_cannot_advance_authority(fleet, changes):
    repo, key, remote, lease, _, authority, _ = fleet
    accepted = hb.publish(repo, lease(), signing_key=str(key), now=1000)
    raw_commit(repo, lease(**changes), key=key)
    receipt = hb.AuthoritySnapshot(authority, 1000, 2000, 1, accepted, 1000)
    current = hb.publish(
        repo,
        lease(seq=2),
        signing_key=str(key),
        now=1000,
        trusted_authority_lookup=lambda *_: receipt,
    )
    assert git(remote, "rev-parse", hb.ref_name("frame-1")) == current
    assert json.loads(git(repo, "show", f"{current}:heartbeat.json"))["epoch"] == authority.epoch


def test_new_authoritative_epoch_without_receipt_does_not_grant_freshness(fleet):
    repo, key, _, lease, observe, authority, _ = fleet
    hb.publish(repo, lease(), signing_key=str(key), now=1000)
    snapshot = hb.AuthoritySnapshot(authority, 1000, 2000, 0, "", None)
    result = observe(trusted_authority_lookup=lambda *_: snapshot)
    assert result.status == "authority-unavailable" and not result.fresh
    assert result.age_basis == "sender-diagnostic"
