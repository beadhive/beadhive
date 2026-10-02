"""Production port/CLI against real SSH-signed commits and enforced bare HQ."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config, host, hosts
from beadhive import host_heartbeat as hb
from beadhive import hq_authority_guard as guard
from beadhive.cli import app
from beadhive.hq_control_plane import (
    ControlPlaneError,
    GitControlPlane,
    bind_broker,
    candidate_ref,
    fingerprint,
    install_guard,
    registration_ref,
)


def frame_process(b, code, *args):
    from beadhive import hq_frame_sandbox

    return subprocess.run(
        [
            sys.executable,
            hq_frame_sandbox.__file__,
            "--server-root",
            str(b["remote"]),
            "--broker-dir",
            str(b["broker_dir"]),
            "--frame-home",
            str(b["frame_home"]),
            "--deny-file",
            str(b["operator"]),
            "--writable-path",
            str(b["repo"]),
            "--frame-env-file",
            str(b["frame_env"]),
            "--",
            sys.executable,
            "-c",
            code,
            str(b["repo"]),
            str(b["runtime"]),
            *map(str, args),
        ],
        cwd=b["repo"],
        capture_output=True,
        text=True,
        timeout=40,
    )


def publish_result(b, lease):
    code = """
import sys, os
from pathlib import Path
from beadhive.host_heartbeat import HeartbeatLease
from beadhive.hq_control_plane import GitControlPlane
lease = HeartbeatLease.model_validate_json(sys.argv[3])
plane = GitControlPlane(Path(sys.argv[1]),
                       authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
print(plane.heartbeat(lease, signing_key=sys.argv[2]))
"""
    return frame_process(b, code, lease.model_dump_json(exclude_none=True))


def publish(b, lease):
    result = publish_result(b, lease)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def git(repo, *args, data=None, check=True):
    result = subprocess.run(
        ["git", "-c", "protocol.ext.allow=always", *args],
        cwd=repo,
        input=data,
        capture_output=True,
        text=True,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip() if check else result


def key(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return path.with_suffix(".pub").read_text().strip()


@pytest.fixture
def backend(tmp_path, monkeypatch, request):
    remote, repo = tmp_path / "hq.git", tmp_path / "client-one"
    repo.mkdir()
    git(repo, "init", "--bare", "-q", "-b", "main", str(remote))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "unchanged").write_text("main history stays unchanged")
    git(repo, "add", "unchanged")
    git(repo, "commit", "-qm", "initial")
    git(repo, "push", "-q", "origin", "main")
    op_repo = tmp_path / "operator-client"
    git(repo, "clone", "-q", str(remote), str(op_repo))
    git(op_repo, "config", "user.name", "Operator")
    git(op_repo, "config", "user.email", "operator@example.invalid")
    operator, runtime = tmp_path / "operator", tmp_path / "runtime"
    operator_public, runtime_public = key(operator), key(runtime)
    digest = install_guard(
        op_repo,
        operator_public,
        "recovery-generation-one",
        confirm_server_custody=True,
        hive_policies={
            prefix: {
                "config_revision": "config-one",
                "config_head": "",
                "valid_until": time.time() + 3600,
                "requires": {"isolation": "kvm"},
                "evict_after_s": 1 if hasattr(request, "param") else 900,
            }
            for prefix in getattr(request, "param", ("bh",))
        },
    )
    broker_dir = tmp_path / "broker"
    broker_dir.mkdir()
    broker_dir.chmod(0o755)
    endpoint = broker_dir / "git.sock"
    log = (tmp_path / "broker.log").open("w")
    process = subprocess.Popen(
        [sys.executable, str(remote / "bh-git-broker.py"), "serve", str(remote), str(endpoint)],
        stdout=log,
        stderr=log,
    )

    def cleanup():
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        log.close()

    request.addfinalizer(cleanup)
    for _ in range(100):
        if endpoint.exists() or process.poll() is not None:
            break
        time.sleep(0.01)
    assert endpoint.exists(), "Git broker failed to start"
    frame_anchor = bind_broker(repo, remote, endpoint, digest, role="frame")
    op_anchor = bind_broker(op_repo, remote, endpoint, digest, role="operator")
    frame_home = tmp_path / "frame-home"
    frame_home.mkdir()
    frame_env = tmp_path / "frame-env.json"
    frame_env.write_text(
        json.dumps(
            {
                "PYTHONPATH": str(Path(hb.__file__).parents[1]),
                "FRAME_AUTHORITY_ANCHOR": str(frame_anchor),
            }
        )
    )
    monkeypatch.setattr(config, "load", lambda: {"hq": {"authority_anchor": str(op_anchor)}})
    monkeypatch.setattr(config, "load_host", lambda: {"hq": {"authority_anchor": str(op_anchor)}})
    monkeypatch.setattr(config, "hq_dir", lambda: op_repo)
    monkeypatch.setattr(config, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(host, "host_id", lambda: "host-one")
    monkeypatch.setattr(host, "signing_key", lambda: str(runtime))
    release = {"id": "release-one", "digest": "sha256:" + "1" * 64}
    caps = {
        "isolation": "kvm",
        "trust_zone": "self-hosted",
        "arch": "x86_64",
        "harnesses": ["codex"],
        "max_sessions": 2,
    }
    manifest = hosts.HostManifest(
        host_id="host-one",
        frame_id="frame-one",
        instance_ref="vm-one",
        release=release,
        capabilities=caps,
        os="linux",
        arch="x86_64",
        label="one",
        role="executor",
        identity={"kind": "none"},
    )
    hosts.save(repo, manifest)
    authority = hb.ObservationAuthority(
        "frame-one",
        "host-one",
        "vm-one",
        fingerprint(runtime_public),
        1,
        "fleet-one",
        "config-one",
        time.time() + 3600,
    )
    plane = GitControlPlane(op_repo, authority_anchor=op_anchor)
    desired = {"declared": True, "release": release, "caps": caps, "profile": "fleet-profile"}
    git(repo, "remote", "set-url", "origin", plane._remote(plane._policy()))
    plane.grant(authority, runtime_public, desired, expected="", operator_key=str(operator))
    plane.publish_registration_evidence(manifest, signing_key=str(runtime))

    def lease(sequence=1, **changes):
        data = dict(
            frame_id="frame-one",
            holderIdentity="host-one",
            instance_ref="vm-one",
            key_id=authority.key_fingerprint,
            epoch=1,
            audience="fleet-one",
            config_revision="config-one",
            seq=sequence,
            renewTime=datetime.now(UTC).isoformat(),
            release=release,
            state_seen="pending",
            conformance={
                "profile": "fleet-profile",
                "status": "conformant",
                "checks": [{"id": "required", "status": "pass"}],
            },
            report_digest="sha256:" + "2" * 64,
        )
        return hb.HeartbeatLease(**{**data, **changes})

    def accept(sequence=1, **changes):
        beat = publish(result, lease(sequence, **changes))
        expected = plane._read()[0]
        plane.accept_observation("frame-one", expected=expected, operator_key=str(operator))
        return beat

    result = dict(
        repo=repo,
        op_repo=op_repo,
        frame_home=frame_home,
        frame_env=frame_env,
        frame_anchor=frame_anchor,
        op_anchor=op_anchor,
        remote=remote,
        plane=plane,
        operator=operator,
        runtime=runtime,
        operator_public=operator_public,
        public=runtime_public,
        authority=authority,
        manifest=manifest,
        desired=desired,
        lease=lease,
        accept=accept,
        digest=digest,
        tmp=tmp_path,
        broker_dir=broker_dir,
        endpoint=endpoint,
    )
    yield result


def apply(b, verb, **changes):
    plan = b["plane"].lifecycle(verb, "frame-one")
    options = dict(
        expected=plan["revision"],
        expected_host_id=plan["host_id"],
        expected_release=plan["release"],
        operator_key=str(b["operator"]),
        confirm=True,
    )
    return b["plane"].lifecycle(verb, "frame-one", "apply", **{**options, **changes})


def test_real_cli_repeated_beats_observation_and_admission(backend):
    from harness.hq_membership_conformance import assert_common_membership_reads

    b = backend
    main = git(b["remote"], "rev-parse", "main")
    for sequence in range(1, 4):
        path = b["tmp"] / "lease.json"
        path.write_text(b["lease"](sequence).model_dump_json(exclude_none=True))
        code = """
import sys, os
from pathlib import Path
from beadhive import config, host
from beadhive.cli import app
from typer.testing import CliRunner
anchor = os.environ["FRAME_AUTHORITY_ANCHOR"]
os.environ["BH_SKIP_SETUP_CHECK"] = "1"
config.load = lambda: {"schema_version": 1, "hq": {"authority_anchor": anchor}}
config.load_host = lambda: {"schema_version": 1, "hq": {"authority_anchor": anchor}}
config.hq_dir = lambda: Path(sys.argv[1])
host.host_id = lambda: "host-one"
host.signing_key = lambda: sys.argv[2]
result = CliRunner().invoke(app, ["host", "heartbeat", sys.argv[3], "--json"])
print(result.output)
raise SystemExit(result.exit_code)
"""
        result = frame_process(b, code, path)
        assert result.returncode == 0, result.stderr + result.stdout
        sha = json.loads(result.stdout.strip().splitlines()[-1])["sha"]
        assert not git(b["repo"], "show", "-s", "--format=%P", sha)
        result = CliRunner().invoke(
            app,
            [
                "hq",
                "authority",
                "observe",
                "--frame",
                "frame-one",
                "--expected-revision",
                b["plane"]._read()[0],
                "--operator-key",
                str(b["operator"]),
                "--confirm",
            ],
        )
        assert result.exit_code == 0, result.output
    assert git(b["remote"], "rev-parse", "main") == main
    candidate = hb.load_trusted_authority(b["op_repo"], "frame-one")
    assert (
        candidate.sequence == 3
        and not candidate.eligible
        and candidate.lifecycle_state == "pending"
    )
    assert hb.observe(b["op_repo"], b["manifest"]).fresh
    plan = b["plane"].lifecycle("admit", "frame-one")
    result = CliRunner().invoke(
        app,
        [
            "host",
            "admit",
            "apply",
            "frame-one",
            "--expected-revision",
            plan["revision"],
            "--expected-host-id",
            "host-one",
            "--expected-release",
            plan["release"],
            "--operator-key",
            str(b["operator"]),
            "--confirm",
        ],
    )
    assert result.exit_code == 0, result.output
    active = hb.load_trusted_authority(b["op_repo"], "frame-one")
    assert (
        active.eligible
        and active.lifecycle_state == "active"
        and active.authority.candidate_expires_at is None
    )
    assert_common_membership_reads(b["plane"], b["manifest"], sha)
    before = b["plane"]._read()[0]
    assert apply(b, "admit")["state"] == "active"
    assert b["plane"]._read()[0] == before


def test_restore_client_and_sqlite_preserves_receipt_floor(backend):
    b = backend
    first = b["accept"](1)
    accepted = b["plane"].watch_state("frame-one")
    b["accept"](2)
    second = b["tmp"] / "client-two"
    git(b["repo"], "clone", "-q", str(b["remote"]), str(second))
    op_anchor = bind_broker(second, b["remote"], b["endpoint"], b["digest"], role="operator")
    plane = GitControlPlane(second, authority_anchor=op_anchor)
    snapshot = plane.watch_state("frame-one")
    assert snapshot.sequence == 2 and snapshot.first_seen >= accepted.first_seen
    git(b["repo"], "push", "--force", "origin", f"{first}:{candidate_ref('frame-one')}")
    restored = hb.observe(
        second,
        b["manifest"],
        observer_dir=b["tmp"] / "empty-observer",
        trusted_authority_lookup=lambda *_: plane.watch_state("frame-one"),
    )
    assert restored.status == "replay" and not restored.fresh
    anchor = bind_broker(second, b["remote"], b["endpoint"], b["digest"], role="frame")
    env = b["tmp"] / "second-env.json"
    env.write_text(
        json.dumps(
            {"PYTHONPATH": str(Path(hb.__file__).parents[1]), "FRAME_AUTHORITY_ANCHOR": str(anchor)}
        )
    )
    result = publish_result({**b, "repo": second, "frame_env": env}, b["lease"](2))
    assert result.returncode != 0 and "accepted sequence" in result.stderr


def test_receipt_poll_is_idempotent_and_does_not_refresh(backend):
    b = backend
    b["accept"](1)
    before = b["plane"]._read()[0]
    snapshot = b["plane"].watch_state("frame-one")
    assert (
        b["plane"].accept_observation("frame-one", expected=before, operator_key=str(b["operator"]))
        == before
    )
    assert b["plane"].watch_state("frame-one").first_seen == snapshot.first_seen


def test_candidate_cannot_write_authority_or_receipts(backend):
    b = backend
    expected, state, _ = b["plane"]._read()
    with pytest.raises(ControlPlaneError):
        b["plane"]._write(state, expected, str(b["runtime"]))
    assert b["plane"]._read()[0] == expected
    # The frame bypasses the port and directly asks the protected broker to write authority.
    code = r"""
import json, os, sys
from pathlib import Path
from beadhive.hq_control_plane import GitControlPlane, _git
from beadhive import hq_authority_guard as guard
plane = GitControlPlane(Path(sys.argv[1]), authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
expected, state, policy = plane._read()
state["revision"] += 1
blob = _git(plane.hq_dir, "hash-object", "-w", "--stdin", data=json.dumps(state))
tree = _git(plane.hq_dir, "mktree", data=f"100644 blob {blob}\tauthority.json\n")
sha = _git(plane.hq_dir, "-c", "gpg.format=ssh", "-c", f"user.signingkey={sys.argv[2]}",
           "commit-tree", "-S", tree, "-p", expected, data="unauthorized receipt\n")
_git(plane.hq_dir, "-c", "protocol.ext.allow=always", "push", "--atomic", plane._remote(policy),
     f"{sha}:{guard.HEAD}", f"{sha}:{guard.WITNESS}{state['revision']:020d}")
"""
    result = frame_process(b, code)
    assert result.returncode != 0 and "pre-receive hook declined" in result.stderr
    assert b["plane"]._read()[0] == expected


def test_signed_old_authority_rollback_rejected_at_receive(backend):
    b = backend
    old = b["plane"]._read()[0]
    b["accept"](1)
    current = b["plane"]._read()[0]
    result = git(b["repo"], "push", "--force", "origin", f"{old}:{guard.HEAD}", check=False)
    assert result.returncode != 0
    assert b["plane"]._read()[0] == current
    # Simulate an operator restore of only the head; retained remote witnesses detect it.
    git(b["remote"], "update-ref", guard.HEAD, old)
    with pytest.raises(ControlPlaneError, match="rollback"):
        b["plane"].watch_state("frame-one")


@pytest.mark.parametrize(
    "path", ["hooks/pre-receive", "bh-authority-guard.py", guard.POLICY, "bh-authority-operators"]
)
def test_changed_server_enforcement_fails_closed(backend, path):
    b = backend
    target = b["remote"] / path
    target.write_text(target.read_text() + "\n# changed\n")
    with pytest.raises(ControlPlaneError):
        b["plane"].watch_state("frame-one")


def test_missing_policy_binding_is_not_ready(backend):
    b = backend
    b["plane"].authority_anchor = None
    with pytest.raises(ControlPlaneError, match="provisioned"):
        b["plane"].watch_state("frame-one")
    assert GitControlPlane(b["repo"]).authority_anchor is None


@pytest.mark.parametrize(
    "field,value", [("epoch", 999999), ("instance_ref", "other-vm"), ("key_id", "SHA256:unknown")]
)
def test_ungranted_predecessor_cannot_poison_epoch(backend, field, value):
    b = backend
    ungranted = b["lease"](100, **{field: value})
    payload = ungranted.model_dump_json(exclude_none=True)
    blob = git(b["repo"], "hash-object", "-w", "--stdin", data=payload)
    tree = git(b["repo"], "mktree", data=f"100644 blob {blob}\theartbeat.json\n")
    sha = git(
        b["repo"],
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={b['runtime']}",
        "commit-tree",
        "-S",
        tree,
        data="poison\n",
    )
    git(b["repo"], "push", "origin", f"{sha}:{candidate_ref('frame-one')}")
    b["accept"](1)
    assert b["plane"].watch_state("frame-one").sequence == 1


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"declared": False}, "declared"),
        ({"caps": {}}, "capability"),
        ({"profile": "different"}, "conformant"),
    ],
)
def test_admission_enforces_declared_policy(backend, change, reason):
    b = backend
    sha, state, _ = b["plane"]._read()
    state["frames"]["frame-one"]["candidate"]["desired"].update(change)
    b["plane"]._write(state, sha, str(b["operator"]))
    for seq in range(1, 4):
        b["accept"](seq)
    with pytest.raises(ControlPlaneError, match=reason):
        apply(b, "admit")


def test_admission_requires_three_beats_and_exact_expected_values(backend):
    b = backend
    b["accept"](1)
    with pytest.raises(ControlPlaneError, match="three"):
        apply(b, "admit")
    for seq in (2, 3):
        b["accept"](seq)
    with pytest.raises(ControlPlaneError, match="expected"):
        apply(b, "admit", expected_host_id="wrong-host")
    with pytest.raises(ControlPlaneError, match="confirm"):
        apply(b, "admit", confirm=False)


def test_lifecycle_cordon_drain_park_resume_quarantine_retire(backend):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    apply(b, "cordon")
    assert not b["plane"].watch_state("frame-one").eligible
    assert apply(b, "resume")["state"] == "active"
    with pytest.raises(ControlPlaneError, match="illegal"):
        apply(b, "park")
    apply(b, "drain", deadline=time.time() + 600)
    b["accept"](4, state_seen="drained")
    assert apply(b, "park")["state"] == "parked"
    assert apply(b, "resume")["state"] == "active"
    assert apply(b, "quarantine")["state"] == "quarantined"
    with pytest.raises(ControlPlaneError, match="illegal"):
        apply(b, "resume")
    assert apply(b, "retire")["state"] == "retired"
    assert b["plane"].watch_state("frame-one") is None
    result = publish_result(b, b["lease"](5))
    assert result.returncode != 0 and "operator-granted" in result.stderr


def test_first_observer_rejects_expired_and_future_sender(backend):
    b = backend
    for seq, offset in ((1, -1000), (2, 1000)):
        renew = datetime.fromtimestamp(time.time() + offset, UTC).isoformat()
        publish(b, b["lease"](seq, renewTime=renew))
        with pytest.raises(ControlPlaneError, match="expired or future"):
            b["plane"].accept_observation(
                "frame-one", expected=b["plane"]._read()[0], operator_key=str(b["operator"])
            )
    assert b["plane"].watch_state("frame-one").sequence == 0


def test_frame_process_cannot_write_server_or_read_operator_key(backend):
    b = backend
    code = """
import errno, os, sys
from pathlib import Path
root, operator = Path(sys.argv[3]), Path(sys.argv[4])
for path in (root / "config", root / "bh-authority-policy.json", root / "hooks/pre-receive",
             root / "refs/heads/bh-authority", root / "new-receipt"):
    try:
        with path.open("w") as stream:
            stream.write("unauthorized")
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM), (path, exc)
    else:
        raise AssertionError("frame wrote server authority file")
assert operator.read_bytes() == b"", "operator private key leaked into frame process"
caps = Path("/proc/self/status").read_text()
assert "CapEff:\\t0000000000000000" in caps
assert not os.access(root, os.W_OK)
print("custody-enforced")
"""
    result = frame_process(b, code, b["remote"], b["operator"])
    assert result.returncode == 0, result.stderr
    assert "custody-enforced" in result.stdout


def test_writable_server_frame_publication_denied(backend):
    b = backend
    with pytest.raises(ControlPlaneError, match="read-only mount custody|filesystem write access"):
        GitControlPlane(b["repo"], authority_anchor=b["frame_anchor"]).heartbeat(
            b["lease"](), signing_key=str(b["runtime"])
        )


@pytest.mark.parametrize(
    "broker_request", [b"../other.git\n", b"git-receive-pack /other/path\n", b"sh -c id\n"]
)
def test_broker_rejects_path_and_command_escape(backend, broker_request):
    b = backend
    before = b["plane"]._read()[0]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(b["endpoint"]))
        connection.sendall(broker_request)
        assert connection.recv(64) == b""
    assert b["plane"]._read()[0] == before


def test_supersede_retires_previous_signer_atomically(backend):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    previous_key = b["authority"].key_fingerprint
    new_key = b["tmp"] / "replacement-runtime"
    public = key(new_key)
    authority = replace(
        b["authority"],
        holder_identity="host-two",
        instance_ref="vm-two",
        key_fingerprint=fingerprint(public),
        epoch=2,
        candidate_expires_at=time.time() + 3600,
    )
    b["plane"].grant(
        authority,
        public,
        b["desired"],
        expected=b["plane"]._read()[0],
        operator_key=str(b["operator"]),
    )
    manifest = b["manifest"].model_copy(update={"host_id": "host-two", "instance_ref": "vm-two"})
    b["plane"].publish_registration_evidence(manifest, signing_key=str(new_key))
    active = b["plane"].watch_state("frame-one")
    assert active.eligible and active.authority.key_fingerprint == previous_key
    assert len(active.alternatives) == 1 and not active.alternatives[0].eligible
    b["accept"](4, state_seen="active")
    assert b["plane"].watch_state("frame-one").sequence == 4
    new_client = {**b, "runtime": new_key}
    for seq in (1, 2, 3):
        record = b["lease"](
            seq,
            holderIdentity="host-two",
            instance_ref="vm-two",
            key_id=authority.key_fingerprint,
            epoch=2,
        )
        publish(new_client, record)
        b["plane"].accept_observation(
            "frame-one",
            expected=b["plane"]._read()[0],
            operator_key=str(b["operator"]),
            holder_identity="host-two",
        )
    with pytest.raises(ControlPlaneError, match="supersede"):
        apply(b, "admit")
    apply(b, "admit", supersede=True)
    assert b["plane"].watch_state("frame-one").authority.key_fingerprint != previous_key
    result = publish_result(b, b["lease"](5))
    assert result.returncode != 0 and "operator-granted" in result.stderr
    _, state, _ = b["plane"]._read()
    trust = b["plane"]._trust(state)
    assert b["public"] not in trust.read_text()


def test_operator_renewal_after_expiry_preserves_authoritative_receipts(backend):
    b = backend
    b["accept"](1)
    before, state, _ = b["plane"]._read()
    frames = json.loads(json.dumps(state["frames"]))
    expired_head = b["plane"]._write(state, before, str(b["operator"]), duration=1)
    time.sleep(1.1)
    with pytest.raises(ControlPlaneError, match="expired"):
        b["plane"].watch_state("frame-one")
    renewed = b["plane"].renew(expected=expired_head, operator_key=str(b["operator"]))
    _, restored, _ = b["plane"]._read()
    assert renewed != expired_head and restored["frames"] == frames
    assert b["plane"].watch_state("frame-one").sequence == 1
    replay = publish_result(b, b["lease"](1))
    assert replay.returncode != 0 and "accepted sequence" in replay.stderr


def test_active_key_and_retired_key_cannot_be_reenrolled(backend):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    replacement = replace(
        b["authority"], holder_identity="host-two", instance_ref="vm-two", epoch=2
    )
    for retired in (False, True):
        if retired:
            apply(b, "retire")
        with pytest.raises(ControlPlaneError, match="key"):
            b["plane"].grant(
                replacement,
                b["public"],
                b["desired"],
                expected=b["plane"]._read()[0],
                operator_key=str(b["operator"]),
            )


def test_frame_environment_and_host_proc_aliases_excluded(backend):
    b = backend
    code = """
import os, sys
from pathlib import Path
assert "SSH_AUTH_SOCK" not in os.environ
assert "OPERATOR_SECRET" not in os.environ
operator_pid = sys.argv[3]
assert not Path("/proc", operator_pid, "root").exists()
assert not Path("/proc", operator_pid, "fd").exists()
assert "NoNewPrivs:\\t1" in Path("/proc/self/status").read_text()
print("environment-and-proc-protected")
"""
    old = os.environ.get("OPERATOR_SECRET")
    os.environ["OPERATOR_SECRET"] = "operator-only"
    try:
        result = frame_process(b, code, os.getpid())
    finally:
        if old is None:
            del os.environ["OPERATOR_SECRET"]
        else:
            os.environ["OPERATOR_SECRET"] = old
    assert result.returncode == 0, result.stderr


def test_mutable_client_origin_and_config_cannot_replace_protected_authority(backend):
    b = backend
    expected = b["plane"]._read()[0]
    git(b["repo"], "remote", "set-url", "origin", "file:///nonexistent-attacker.git")
    git(b["repo"], "config", "bh.authorityPolicyDigest", "attacker")
    # The protected anchor selects broker/policy/generation independently of checkout configuration.
    publish(b, b["lease"](1))
    assert b["plane"]._read()[0] == expected
    b["plane"].accept_observation("frame-one", expected=expected, operator_key=str(b["operator"]))
    assert b["plane"].watch_state("frame-one").sequence == 1


def test_actual_console_reads_real_config_identity_and_protected_anchor(backend):
    b = backend
    (b["frame_home"] / "config.yaml").write_text(
        "schema_version: 1\nhq:\n  mode: git\n  authority_anchor: " + str(b["frame_anchor"]) + "\n"
    )
    (b["frame_home"] / "host.yaml").write_text(
        "host_id: host-one\nlabel: runtime\nsigning_key: " + str(b["runtime"]) + "\n"
    )
    environment = json.loads(b["frame_env"].read_text())
    environment.update(BH_HOME=str(b["frame_home"]), BH_HQ=str(b["repo"]))
    b["frame_env"].write_text(json.dumps(environment))
    lease_file = b["frame_home"] / "lease.json"
    lease_file.write_text(b["lease"]().model_dump_json(exclude_none=True))
    console = Path(sys.executable).parent / "bh"
    code = """
import os, sys
# Supported debug setup override only; no product configuration/identity/provider substitutions.
os.environ["BH_SKIP_SETUP_CHECK"] = "1"
os.execv(sys.argv[3], [sys.argv[3], "host", "heartbeat", sys.argv[4], "--json"])
"""
    result = frame_process(b, code, console, lease_file)
    assert result.returncode == 0, result.stderr + result.stdout
    row = json.loads(result.stdout.strip().splitlines()[-1])
    assert row["ref"] == candidate_ref("frame-one")
    b["plane"].accept_observation(
        "frame-one", expected=b["plane"]._read()[0], operator_key=str(b["operator"])
    )
    assert b["plane"].watch_state("frame-one").sha == row["sha"]


@pytest.mark.parametrize("kind", ["url-rewrite", "environment"])
def test_authority_transport_rejects_git_input_injection(backend, monkeypatch, kind):
    b = backend
    if kind == "url-rewrite":
        git(b["op_repo"], "config", "url.file:///attacker.git.insteadOf", "ext::")
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.file:///attacker.git.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "ext::")
    with pytest.raises(ControlPlaneError, match="rewrite|injection"):
        b["plane"].watch_state("frame-one")


def test_future_checkout_anchor_is_provisioned_without_client_mutation(backend):
    b = backend
    future = b["tmp"] / "future-client"
    anchor = bind_broker(future, b["remote"], b["endpoint"], b["digest"])
    assert anchor.is_file() and not future.exists()
    assert json.loads(anchor.read_text())["role"] == "frame"


def test_expired_candidate_cleanup_preserves_active_authority_and_carrier(backend):
    b = backend
    for sequence in (1, 2, 3):
        b["accept"](sequence)
    apply(b, "admit")
    active = b["plane"].watch_state("frame-one")
    replacement_key = b["tmp"] / "cleanup-key"
    public = key(replacement_key)
    authority = replace(
        b["authority"],
        holder_identity="host-two",
        instance_ref="vm-two",
        key_fingerprint=fingerprint(public),
        epoch=2,
    )
    b["plane"].grant(
        authority,
        public,
        b["desired"],
        expected=b["plane"]._read()[0],
        operator_key=str(b["operator"]),
    )
    lease = b["lease"](
        1,
        holderIdentity="host-two",
        instance_ref="vm-two",
        epoch=2,
        key_id=authority.key_fingerprint,
    )
    publish({**b, "runtime": replacement_key}, lease)
    sha, state, _ = b["plane"]._read()
    state["frames"]["frame-one"]["candidate"]["authority"]["candidate_expires_at"] = time.time() - 1
    b["plane"]._write(state, sha, str(b["operator"]))
    plan = b["plane"].lifecycle("retire", "frame-one", expected_host_id="host-two")
    b["plane"].lifecycle(
        "retire",
        "frame-one",
        "apply",
        expected=plan["revision"],
        expected_host_id="host-two",
        expected_release=plan["release"],
        confirm=True,
        operator_key=str(b["operator"]),
    )
    restored = b["plane"].watch_state("frame-one")
    assert restored.eligible and restored.authority == active.authority
    assert restored.sha == active.sha and restored.sequence == active.sequence
    assert git(b["remote"], "rev-parse", hb.ref_name("frame-one")) == active.sha
    assert git(
        b["remote"], "rev-parse", "--verify", candidate_ref("frame-one"), check=False
    ).returncode


def raw_frame_push(b, refspec):
    code = """
import os, subprocess, sys
from pathlib import Path
from beadhive.hq_control_plane import GitControlPlane
plane = GitControlPlane(Path(sys.argv[1]), authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
policy = plane._policy()
result = subprocess.run([policy["executables"]["git"]["path"], "-c", "protocol.ext.allow=always",
                         "push", "--force", plane._remote(policy), sys.argv[3]],
                        cwd=plane.hq_dir, capture_output=True, text=True)
print(result.stdout)
print(result.stderr, file=sys.stderr)
raise SystemExit(result.returncode)
"""
    return frame_process(b, code, refspec)


def carrier_commit(b, *, signed=True, lease=None):
    payload = (lease or b["lease"]()).model_dump_json(exclude_none=True)
    blob = git(b["repo"], "hash-object", "-w", "--stdin", data=payload)
    tree = git(b["repo"], "mktree", data=f"100644 blob {blob}\theartbeat.json\n")
    signing = ["-S"] if signed else []
    return git(
        b["repo"],
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={b['runtime']}",
        "commit-tree",
        *signing,
        tree,
        data="raw frame carrier\n",
    )


@pytest.mark.parametrize(
    "attack", ["unsigned", "delete", "unknown-ref", "cross-frame", "wrong-epoch"]
)
def test_raw_frame_receive_requires_own_authorized_carrier(backend, attack):
    b = backend
    b["accept"](1)
    reference = candidate_ref("frame-one")
    before = git(b["remote"], "rev-parse", reference)
    lease = b["lease"](2, epoch=999) if attack == "wrong-epoch" else b["lease"](2)
    sha = carrier_commit(b, signed=attack != "unsigned", lease=lease)
    if attack == "delete":
        refspec = ":" + reference
    else:
        destination = {
            "unknown-ref": "refs/heads/escape",
            "cross-frame": candidate_ref("victim"),
        }.get(attack, reference)
        refspec = sha + ":" + destination
    result = raw_frame_push(b, refspec)
    assert result.returncode != 0 and "pre-receive hook declined" in result.stderr
    assert git(b["remote"], "rev-parse", reference) == before


def test_raw_retired_key_cannot_publish_after_operator_retire(backend):
    b = backend
    b["accept"](1)
    apply(b, "retire")
    sha = carrier_commit(b, lease=b["lease"](2))
    for reference in (candidate_ref("frame-one"), hb.ref_name("frame-one"), "refs/heads/escape"):
        result = raw_frame_push(b, sha + ":" + reference)
        assert result.returncode != 0 and "pre-receive hook declined" in result.stderr


def test_signed_own_manifest_publication_and_pinned_runtime_inventory(backend):
    b = backend
    for name in ("gpg.format", "user.signingkey", "commit.gpgsign", "gpg.ssh.program"):
        assert git(b["repo"], "config", "--get", name, check=False).returncode == 1
    code = """
import os, sys
from pathlib import Path
from beadhive import host
from beadhive.hq_control_plane import GitControlPlane
host.signing_key = lambda: sys.argv[2]
plane = GitControlPlane(Path(sys.argv[1]), authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
print(plane.publish_registration("host-one"))
"""
    result = frame_process(b, code)
    assert result.returncode == 0, result.stderr
    assert "True" in result.stdout
    remote_manifest = git(b["remote"], "show", "main:hosts/host-one.yaml")
    assert remote_manifest == hosts.manifest_path(b["repo"], "host-one").read_text().strip()
    assert b["plane"].watch_state("frame-one") is not None
    b["accept"](1)
    b["accept"](2)
    assert not list((b["remote"] / "bh-guard-libs").rglob("*.pyc"))
    assert b["plane"].watch_state("frame-one").sequence == 2


@pytest.mark.parametrize(
    "attack",
    [
        "wrong-host",
        "extra-path",
        "wrong-binding",
        "unsigned",
        "delete",
        "invalid-schema",
        "duplicate-yaml",
        "custom-tag",
        "retired",
    ],
)
def test_raw_main_receive_requires_exact_signed_own_manifest(backend, attack):
    b = backend
    before = git(b["remote"], "rev-parse", "main")
    path = hosts.manifest_path(b["repo"], "host-one")
    if attack == "wrong-host":
        path = b["repo"] / "hosts/victim.yaml"
        path.write_text(hosts.manifest_path(b["repo"], "host-one").read_text())
    elif attack == "extra-path":
        (b["repo"] / "unrelated").write_text("unrelated mutation")
        git(b["repo"], "add", "unrelated")
    elif attack == "wrong-binding":
        path.write_text(
            json.dumps(
                b["manifest"]
                .model_copy(update={"instance_ref": "victim-vm"})
                .model_dump(mode="json")
            )
        )
    if attack == "invalid-schema":
        path.write_text(path.read_text().replace("role: executor", "role: illegal"))
    elif attack == "duplicate-yaml":
        path.write_text(path.read_text() + "\nhost_id: host-one\n")
    elif attack == "custom-tag":
        path.write_text(
            path.read_text() + "\ncapacity: !!python/object/apply:os.system ['false']\n"
        )
    elif attack == "retired":
        apply(b, "retire")
    git(b["repo"], "add", str(path))
    tree = git(b["repo"], "write-tree")
    signed = [] if attack == "unsigned" else ["-S"]
    sha = git(
        b["repo"],
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={b['runtime']}",
        "commit-tree",
        *signed,
        tree,
        "-p",
        before,
        data="raw main attack\n",
    )
    result = raw_frame_push(
        b, ":refs/heads/main" if attack == "delete" else sha + ":refs/heads/main"
    )
    assert result.returncode != 0 and "pre-receive hook declined" in result.stderr
    assert git(b["remote"], "rev-parse", "main") == before


def test_candidate_holder_identity_cannot_reuse_incarnation(backend):
    b = backend
    public = key(b["tmp"] / "different-key")
    for retired in (False, True):
        if retired:
            apply(b, "retire")
        authority = replace(
            b["authority"],
            key_fingerprint=fingerprint(public),
            epoch=2,
            instance_ref="different-vm",
        )
        with pytest.raises(ControlPlaneError, match="holder identity"):
            b["plane"].grant(
                authority,
                public,
                b["desired"],
                expected=b["plane"]._read()[0],
                operator_key=str(b["operator"]),
            )


def test_raw_registration_cross_incarnation_denied(backend):
    b = backend
    manifest = b["manifest"].model_copy(update={"instance_ref": "another-incarnation"})
    blob = git(
        b["repo"], "hash-object", "-w", "--stdin", data=manifest.model_dump_json(exclude_none=True)
    )
    tree = git(b["repo"], "mktree", data=f"100644 blob {blob}\tregistration.json\n")
    sha = git(
        b["repo"],
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={b['runtime']}",
        "commit-tree",
        "-S",
        tree,
        data="cross-incarnation registration\n",
    )
    result = raw_frame_push(b, sha + ":" + registration_ref(asdict(b["authority"])))
    assert result.returncode != 0 and "pre-receive hook declined" in result.stderr


def test_first_carrier_between_retire_prepare_and_receive_requires_retry(backend, monkeypatch):
    b = backend
    plane = b["plane"]
    original = plane._write
    sha = carrier_commit(b)

    def interleave(*args, **kwargs):
        # Operator prepared deletion before the first runtime carrier existed.
        result = raw_frame_push(b, sha + ":" + candidate_ref("frame-one"))
        assert result.returncode == 0, result.stderr
        return original(*args, **kwargs)

    monkeypatch.setattr(plane, "_write", interleave)
    with pytest.raises(ControlPlaneError, match="pre-receive hook declined"):
        apply(b, "retire")
    monkeypatch.setattr(plane, "_write", original)
    apply(b, "retire")
    for reference in (candidate_ref("frame-one"), registration_ref(asdict(b["authority"]))):
        assert not git(b["remote"], "for-each-ref", "--format=%(objectname)", reference)
        assert raw_frame_push(b, sha + ":" + reference).returncode != 0


def test_complete_receive_session_serializes_actual_operator_client(backend):
    from concurrent.futures import ThreadPoolExecutor

    from beadhive.hq_git_broker import receive_lock

    b = backend
    # The first client stalls after real receive-pack advertisement. This owns
    # the transaction lock before authorization, throughout Git's input phase.
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    connection.connect(str(b["endpoint"]))
    connection.sendall(b"git-receive-pack\n")
    assert connection.recv(65536)
    with pytest.raises(TimeoutError, match="lock deadline"):
        with receive_lock(b["remote"], timeout=0.1):
            pass
    head = git(b["remote"], "rev-parse", guard.HEAD)
    with ThreadPoolExecutor() as executor:
        entered = __import__("threading").Event()

        def retire():
            entered.set()
            return apply(b, "retire")

        future = executor.submit(retire)
        assert entered.wait(2)
        time.sleep(0.3)
        assert not future.done()
        assert git(b["remote"], "rev-parse", guard.HEAD) == head
        connection.close()
        assert future.result(timeout=20)["state"] == "retired"
    assert git(b["remote"], "rev-parse", guard.HEAD) != head
    assert not git(
        b["remote"],
        "for-each-ref",
        "--format=%(objectname)",
        registration_ref(asdict(b["authority"])),
    )


def test_fetch_config_selects_exact_active_and_candidate_desired_binding(backend):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    public = key(b["tmp"] / "next-config-key")
    authority = replace(
        b["authority"],
        holder_identity="host-next",
        instance_ref="vm-next",
        key_fingerprint=fingerprint(public),
        epoch=2,
    )
    desired = {
        **b["desired"],
        "profile": "new-profile",
        "release": {"id": "next-release", "digest": "sha256:" + "2" * 64},
        "caps": {**b["desired"]["caps"], "max_sessions": 8},
    }
    plane = b["plane"]
    plane.grant(
        authority, public, desired, expected=plane._read()[0], operator_key=str(b["operator"])
    )
    with pytest.raises(ControlPlaneError, match="exact holder identity"):
        plane.fetch_config("frame-one")
    for holder, expected, binding in (
        ("host-one", b["desired"], b["authority"]),
        ("host-next", desired, authority),
    ):
        actual = plane.fetch_config("frame-one", holder_identity=holder)
        assert all(actual[k] == v for k, v in expected.items())
        assert actual["authority"] == asdict(
            replace(binding, candidate_expires_at=None) if holder == "host-one" else binding
        )
    with pytest.raises(ControlPlaneError, match="unknown, expired or retired"):
        plane.fetch_config("frame-one", holder_identity="not-granted")


def test_additive_frame_retire_keeps_legacy_retire_four_parameters(backend):
    import inspect

    from beadhive import host_cli

    assert tuple(inspect.signature(host_cli.retire_cmd).parameters) == (
        "dry_run",
        "backup",
        "confirm",
        "purge",
    )
    b = backend
    result = CliRunner().invoke(app, ["host", "frame-retire", "plan", "frame-one"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    result = CliRunner().invoke(
        app,
        [
            "host",
            "frame-retire",
            "apply",
            "frame-one",
            "--expected-revision",
            plan["revision"],
            "--expected-host-id",
            plan["host_id"],
            "--expected-release",
            plan["release"],
            "--operator-key",
            str(b["operator"]),
            "--confirm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "retired"


def test_committed_fleet_snapshot_cas_and_document_order(backend):
    from beadhive.hq_fleet_config import FleetConfigError
    from beadhive.modules.config.domain.ports import FleetConfigDocument

    b = backend
    plane = b["plane"]
    store = plane.config_store(operator_key=str(b["operator"]))
    documents = (
        FleetConfigDocument("fleet.yaml", "schema_version: 1\nmanaged_repos: []\n"),
        FleetConfigDocument("workspace-prototypes.toml", '[[provider]]\npath="prototypes"\n'),
        FleetConfigDocument("workspace.toml", '[[provider]]\npath="contrib"\n'),
        FleetConfigDocument("allowed_signers", "# public policy\n"),
    )
    initial_main = git(b["remote"], "rev-parse", "main")
    first = store.publish_snapshot(documents, expected_revision="")
    assert first.documents == documents
    assert first.backend_identity.startswith("git:")
    assert first.generation == "recovery-generation-one"
    assert first.fetched_at < first.valid_until
    assert store.load_snapshot().commit_revision == first.commit_revision
    (b["op_repo"] / "fleet.yaml").write_text("dirty local file must never replace committed config")
    assert store.load_snapshot().documents == documents
    second = store.publish_snapshot(documents[:-1], expected_revision=first.commit_revision)
    assert second.commit_revision != first.commit_revision
    with pytest.raises(FleetConfigError, match="expected configuration revision changed"):
        store.publish_snapshot(documents, expected_revision=first.commit_revision)
    with pytest.raises(FleetConfigError, match="no longer current"):
        store.load_snapshot(revision=first.commit_revision)
    assert git(b["remote"], "rev-parse", "main") == initial_main
    assert store.load_snapshot().commit_revision == second.commit_revision


def test_fleet_config_rollback_expiry_and_protected_publication(backend):
    from beadhive.hq_fleet_config import FleetConfigError
    from beadhive.modules.config.domain.ports import FleetConfigDocument

    b = backend
    store = b["plane"].config_store(operator_key=str(b["operator"]))
    docs = (FleetConfigDocument("fleet.yaml", "schema_version: 1\n"),)
    first = store.publish_snapshot(docs, expected_revision="")
    second = store.publish_snapshot(docs, expected_revision=first.commit_revision)
    # Operator-owned remote restore cannot roll back the head while newer witnesses survive.
    git(b["remote"], "update-ref", guard.CONFIG_HEAD, first.commit_revision)
    with pytest.raises(FleetConfigError, match="rollback"):
        store.load_snapshot()
    git(b["remote"], "update-ref", guard.CONFIG_HEAD, second.commit_revision)
    original_clock = b["plane"].clock
    b["plane"].clock = lambda: second.valid_until
    with pytest.raises(FleetConfigError, match="validity expired"):
        store.load_snapshot()
    b["plane"].clock = original_clock
    result = frame_process(
        b,
        """
import os, sys
from pathlib import Path
from beadhive.hq_control_plane import GitControlPlane
from beadhive.modules.config.domain.ports import FleetConfigDocument
plane = GitControlPlane(Path(sys.argv[1]), authority_anchor=os.environ['FRAME_AUTHORITY_ANCHOR'])
plane.config_store(operator_key=sys.argv[2]).publish_snapshot(
    (FleetConfigDocument('fleet.yaml', 'schema_version: 1\\n'),), expected_revision=sys.argv[3])
""",
        second.commit_revision,
    )
    assert result.returncode != 0
    assert "operator custody" in result.stderr
    assert store.load_snapshot().commit_revision == second.commit_revision


def test_attach_fleet_config_reads_only_host_bootstrap(backend, monkeypatch):
    from beadhive.hq_control_plane import attach_fleet_config, control_plane
    from beadhive.modules.config.domain.ports import FleetConfigDocument

    b = backend
    snapshot = (
        b["plane"]
        .config_store(operator_key=str(b["operator"]))
        .publish_snapshot(
            (FleetConfigDocument("fleet.yaml", "schema_version: 1\n"),), expected_revision=""
        )
    )
    monkeypatch.setattr(config, "load", lambda: pytest.fail("recursive effective config load"))
    monkeypatch.setattr(
        config,
        "load_host",
        lambda: {"hq": {"mode": "git", "authority_anchor": str(b["op_anchor"])}},
    )
    _, readback = attach_fleet_config(b["op_repo"])
    assert readback.commit_revision == snapshot.commit_revision
    assert control_plane(b["op_repo"]).authority_anchor == b["op_anchor"]
    for mode in ("dolt-server", "invalid"):
        with pytest.raises(ControlPlaneError, match="unsupported HQ configuration bootstrap mode"):
            attach_fleet_config(b["op_repo"], bootstrap={"mode": mode})


def test_fleet_config_refuses_credential_paths_and_duplicate_documents(backend):
    from beadhive.modules.config.domain.ports import FleetConfigDocument

    b = backend
    store = b["plane"].config_store(operator_key=str(b["operator"]))
    fleet = FleetConfigDocument("fleet.yaml", "schema_version: 1\n")
    for documents in (
        (fleet, FleetConfigDocument("host.yaml", "host-only")),
        (fleet, FleetConfigDocument("../credential", "private")),
        (fleet, fleet),
    ):
        with pytest.raises(ValueError):
            store.publish_snapshot(documents, expected_revision="")
    assert not git(b["remote"], "show-ref", guard.CONFIG_HEAD, check=False).returncode == 0


def test_raw_frame_receive_cannot_publish_config_or_witness(backend):
    from beadhive.modules.config.domain.ports import FleetConfigDocument

    b = backend
    snapshot = (
        b["plane"]
        .config_store(operator_key=str(b["operator"]))
        .publish_snapshot(
            (FleetConfigDocument("fleet.yaml", "schema_version: 1\n"),), expected_revision=""
        )
    )
    # Raw Git cannot bypass adapter-level role checks, even with an operator-signed object.
    code = """
import os, subprocess, sys
from pathlib import Path
from beadhive.hq_control_plane import GitControlPlane
plane = GitControlPlane(Path(sys.argv[1]), authority_anchor=os.environ['FRAME_AUTHORITY_ANCHOR'])
policy = plane._policy()
remote = plane._remote(policy)
git = policy['executables']['git']['path']
subprocess.run([git, '-c', 'protocol.ext.allow=always', 'fetch', remote, sys.argv[3]],
               cwd=plane.hq_dir, check=True)
result = subprocess.run([git, '-c', 'protocol.ext.allow=always', 'push', remote,
                         sys.argv[3] + ':refs/bh/config-witness/00000000000000000002'],
                        cwd=plane.hq_dir, capture_output=True, text=True)
print(result.stderr)
raise SystemExit(result.returncode)
"""
    result = frame_process(b, code, snapshot.commit_revision)
    assert result.returncode != 0
    assert "frame cannot write configuration" in result.stdout, result.stderr + result.stdout
    assert b["plane"].config_store().load_snapshot().commit_revision == snapshot.commit_revision


def test_git_composite_snapshot_refuses_without_joining_independent_reads(tmp_path, monkeypatch):
    plane = GitControlPlane(tmp_path)
    monkeypatch.setattr(plane, "watch_state", lambda *_: pytest.fail("independent authority read"))
    monkeypatch.setattr(plane, "config_store", lambda **_: pytest.fail("independent config read"))
    with pytest.raises(ControlPlaneError, match="atomic config/authority snapshot unsupported"):
        plane.load_config_authority_snapshot("frame-a")


def test_eligibility_bracket_after_new_beat_and_admission(backend):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    publish(b, b["lease"](4, state_seen="active"))
    revision, desired, observation = b["plane"].read_eligibility(b["manifest"])
    assert revision == b["plane"]._read()[0]
    assert desired["state"] == "active"
    assert observation.verified and not observation.fresh and observation.lease.seq == 4
    b["plane"].accept_observation("frame-one", expected=revision, operator_key=str(b["operator"]))
    revision, desired, observation = b["plane"].read_eligibility(b["manifest"])
    assert revision == b["plane"]._read()[0]
    assert observation.verified and observation.fresh and observation.lease.seq == 4


def test_hive_ownership_rejects_unrelated_origin(backend):
    b = backend
    assert b["plane"].read_hive_lease("bh") is None
    git(b["op_repo"], "remote", "set-url", "origin", str(b["tmp"] / "untrusted.git"))
    with pytest.raises(ControlPlaneError, match="origin differs"):
        b["plane"].read_hive_lease("bh")


def test_real_frame_signed_hive_lease_adopt_renew_and_cordoned_release(backend):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    code = """
import sys,os,json
from pathlib import Path
from beadhive import config,host,host_lease
config.load_host = lambda: {"hq": {"authority_anchor": os.environ["FRAME_AUTHORITY_ANCHOR"]}}
config.load = lambda: {"managed_repos": []}
host.host_id = lambda: "host-one"
host.signing_key = lambda: sys.argv[2]
# Canonical fixture catalog is provisioned separately in protected server policy.
from beadhive import registry
registry.resolve_hive = lambda cfg,prefix: {"prefix": "bh", "requires": {"isolation": "kvm"}}
cwd=Path(sys.argv[1])
a=host_lease.adopt("origin","bh",host_id="host-one",label="fixture",cwd=cwd)
r=host_lease.renew("origin","bh",host_id="host-one",cwd=cwd)
assert a.lease.epoch == r.lease.epoch
assert a.sha != r.sha
print(json.dumps({"epoch": r.lease.epoch,"sha": r.sha}))
"""
    result = frame_process(b, code)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["epoch"] == 1
    # Separate bounded operations and accept a real new heartbeat before the loop tick.
    b["accept"](4, state_seen="active")
    keeper_code = (
        code.split("a=host_lease.adopt")[0]
        + """
from beadhive import localloop
config.hq_dir=lambda:cwd
registry.hive_dir_for=lambda cfg,hive:cwd
registry.entry_for_dir=lambda cfg,directory:{"prefix":"bh","requires":{"isolation":"kvm"}}
keeper=localloop.lease_keeper_for("bh",cfg={"host":{"lease":{"ttl":30,"renew_interval":10000}}},hive_dir=cwd)
assert isinstance(keeper.keeper,localloop.HostLeaseKeeper)
status=keeper.renew(active=True)
assert status.held and status.renewed,status
"""
    )
    result = frame_process(b, keeper_code)
    assert result.returncode == 0, result.stderr + result.stdout
    apply(b, "cordon")
    release_code = """
import sys,os
from pathlib import Path
from beadhive import config,host,host_lease
config.load_host=lambda: {"hq":{"authority_anchor":os.environ["FRAME_AUTHORITY_ANCHOR"]}}
host.host_id=lambda:"host-one"
host.signing_key=lambda:sys.argv[2]
out=host_lease.release("origin","bh",host_id="host-one",cwd=Path(sys.argv[1]))
assert out.lease.is_tombstone
"""
    result = frame_process(b, release_code)
    assert result.returncode == 0, result.stderr + result.stdout


def test_frame_hive_lease_guard_rejects_forgery_binding_and_cas_replays(backend, monkeypatch):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    code = """
import sys,os,time
from pathlib import Path
from beadhive import host,host_lease
from beadhive.hq_control_plane import GitControlPlane
host.host_id=lambda:"host-one"
host.signing_key=lambda:sys.argv[2]
plane=GitControlPlane(Path(sys.argv[1]),authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
lease=host_lease.HostLease("host-one","fixture",1,host_lease.now_stamp(),host_lease.now_stamp(time.time()+900))
print(plane.publish_hive_lease("bh",lease,expected="",operation="adopt"))
"""
    result = frame_process(b, code)
    assert result.returncode == 0, result.stderr + result.stdout
    sha = result.stdout.strip()
    base = json.loads(git(b["repo"], "show", f"{sha}:hive-lease.json"))
    import copy

    for failure in ("holder", "incarnation", "authority", "expected", "unsigned", "wrong_signer"):
        data = copy.deepcopy(base)
        data["operation"] = "renew"
        data["expected_lease_sha"] = sha
        if failure == "holder":
            data["lease"]["host_id"] = "host-two"
        elif failure == "incarnation":
            data["authority"]["epoch"] += 1
        elif failure == "authority":
            data["authority_revision"] = "0" * 40
        elif failure == "expected":
            data["expected_lease_sha"] = "f" * 40
        blob = git(b["repo"], "hash-object", "-w", "--stdin", data=json.dumps(data))
        tree = git(b["repo"], "mktree", data=f"100644 blob {blob}\thive-lease.json\n")
        args = [
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={b['operator'] if failure == 'wrong_signer' else b['runtime']}",
            "commit-tree",
        ]
        if failure != "unsigned":
            args.append("-S")
        forged = git(b["repo"], *args, tree, data=f"forged {failure}\n")
        result = raw_frame_push(b, f"{forged}:refs/bh/lease/bh")
        assert result.returncode != 0 and "pre-receive hook declined" in result.stderr, failure
        assert git(b["remote"], "rev-parse", "refs/bh/lease/bh") == sha
    # Read-side intake must honor exactly the same protected policy validity.
    old_clock = b["plane"].clock
    expiry = b["plane"]._policy()["hive_policies"]["bh"]["valid_until"]
    b["plane"].clock = lambda: expiry - 0.001
    assert b["plane"].read_hive_lease("bh", holder_identity="host-one") is not None
    b["plane"].clock = lambda: expiry
    assert b["plane"].read_hive_lease("bh", holder_identity="host-one") is None
    b["plane"].clock = old_clock
    # A config read cannot mask concurrent authority revocation.
    factory = b["plane"].config_store
    store = factory()
    original_read = store._read

    def revoke_during_config():
        result = original_read()
        apply(b, "cordon")
        return result

    store._read = revoke_during_config
    monkeypatch.setattr(b["plane"], "config_store", lambda: store)
    with pytest.raises(ControlPlaneError, match="authority changed"):
        b["plane"].read_hive_lease("bh", holder_identity="host-one")
    monkeypatch.setattr(b["plane"], "config_store", factory)
    # Revoked/retired exact signer cannot resume intake, even with current authority revision.
    apply(b, "retire")
    data = copy.deepcopy(base)
    data["operation"] = "renew"
    data["authority_revision"] = b["plane"]._read()[0]
    data["expected_lease_sha"] = sha
    blob = git(b["repo"], "hash-object", "-w", "--stdin", data=json.dumps(data))
    tree = git(b["repo"], "mktree", data=f"100644 blob {blob}\thive-lease.json\n")
    forged = git(
        b["repo"],
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={b['runtime']}",
        "commit-tree",
        "-S",
        tree,
        data="retired renewal\n",
    )
    assert raw_frame_push(b, f"{forged}:refs/bh/lease/bh").returncode != 0
    assert git(b["remote"], "rev-parse", "refs/bh/lease/bh") == sha


def test_verified_operator_anchor_preserves_config_only_legacy_executor(backend, monkeypatch):
    from beadhive import frame_eligibility

    b = backend
    monkeypatch.setattr(
        config,
        "load_host",
        lambda: {"hq": {"mode": "git", "authority_anchor": str(b["op_anchor"])}},
    )
    assert frame_eligibility.decision_for("legacy-unenrolled", hq_dir=b["op_repo"]) is None

    # An operator anchor proves this host has no frame role; it cannot repair an
    # unsupported SQL config mode without an explicit selected HOST SQL binding.
    monkeypatch.setattr(
        config,
        "load_host",
        lambda: {"hq": {"mode": "dolt-server", "authority_anchor": str(b["op_anchor"])}},
    )
    decision = frame_eligibility.decision_for("legacy-unenrolled", hq_dir=b["op_repo"])
    assert dict(decision.predicates) == {"authority_available": False}


def test_config_head_advance_fences_old_hive_projection(backend):
    from beadhive.modules.config.domain.ports import FleetConfigDocument

    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    code = """
import sys,os,time
from pathlib import Path
from beadhive import host,host_lease
from beadhive.hq_control_plane import GitControlPlane
host.host_id=lambda:"host-one"
host.signing_key=lambda:sys.argv[2]
plane=GitControlPlane(Path(sys.argv[1]),authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
lease=host_lease.HostLease("host-one","fixture",1,host_lease.now_stamp(),host_lease.now_stamp(time.time()+900))
print(plane.publish_hive_lease("bh",lease,expected="",operation="adopt"))
"""
    result = frame_process(b, code)
    assert result.returncode == 0, result.stderr + result.stdout
    sha = result.stdout.strip()
    assert b["plane"].read_hive_lease("bh", holder_identity="host-one") is not None
    b["plane"].config_store(operator_key=str(b["operator"])).publish_snapshot(
        (FleetConfigDocument("fleet.yaml", "schema_version: 1\nmanaged_repos: []\n"),),
        expected_revision="",
    )
    assert b["plane"].read_hive_lease("bh", holder_identity="host-one") is None
    code = code.replace('expected="",operation="adopt"', f'expected="{sha}",operation="renew"')
    result = frame_process(b, code)
    assert result.returncode != 0 and "pre-receive hook declined" in result.stderr
    assert git(b["remote"], "rev-parse", "refs/bh/lease/bh") == sha
    release_code = """
import sys,os
from pathlib import Path
from beadhive import config,host,host_lease
config.load_host=lambda:{"hq":{"authority_anchor":os.environ["FRAME_AUTHORITY_ANCHOR"]}}
host.host_id=lambda:"host-one"
host.signing_key=lambda:sys.argv[2]
out=host_lease.release("origin","bh",host_id="host-one",cwd=Path(sys.argv[1]))
assert out.lease.is_tombstone
"""
    result = frame_process(b, release_code)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("backend", [("bh", "stale", "quarantine", "retire")], indirect=True)
def test_real_foreign_holder_takeover_requires_authenticated_incumbent_evidence(backend):
    b = backend
    for seq in (1, 2, 3):
        b["accept"](seq)
    apply(b, "admit")
    runtime = b["repo"] / "second-runtime"
    public = key(runtime)
    second_authority = replace(
        b["authority"],
        frame_id="frame-two",
        holder_identity="host-two",
        instance_ref="vm-two",
        key_fingerprint=fingerprint(public),
    )
    second_manifest = b["manifest"].model_copy(
        update={"frame_id": "frame-two", "host_id": "host-two", "instance_ref": "vm-two"}
    )
    hosts.save(b["repo"], second_manifest)
    b["plane"].grant(
        second_authority,
        public,
        b["desired"],
        expected=b["plane"]._read()[0],
        operator_key=str(b["operator"]),
    )
    second = {**b, "runtime": runtime, "manifest": second_manifest}
    register = """
import sys,os
from pathlib import Path
from beadhive import hosts
from beadhive.hq_control_plane import GitControlPlane
plane=GitControlPlane(Path(sys.argv[1]),authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
plane.publish_registration_evidence(hosts.load(Path(sys.argv[1]),"host-two"),signing_key=sys.argv[2])
"""
    result = frame_process(second, register)
    assert result.returncode == 0, result.stderr + result.stdout
    for seq in (1, 2, 3):
        lease = b["lease"](seq).model_copy(
            update={
                "frame_id": "frame-two",
                "holderIdentity": "host-two",
                "instance_ref": "vm-two",
                "key_id": second_authority.key_fingerprint,
            }
        )
        publish(second, lease)
        b["plane"].accept_observation(
            "frame-two", expected=b["plane"]._read()[0], operator_key=str(b["operator"])
        )
    plan = b["plane"].lifecycle("admit", "frame-two")
    b["plane"].lifecycle(
        "admit",
        "frame-two",
        "apply",
        expected=plan["revision"],
        expected_host_id=plan["host_id"],
        expected_release=plan["release"],
        operator_key=str(b["operator"]),
        confirm=True,
    )
    # Separate canonical hives retain monotonic lease epochs for each independent case.
    create = """
import sys,os,time,json
from pathlib import Path
from beadhive import host,host_lease
from beadhive.hq_control_plane import GitControlPlane
host.host_id=lambda:"host-one"
host.signing_key=lambda:sys.argv[2]
plane=GitControlPlane(Path(sys.argv[1]),authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
leases={}
for prefix in ("bh","stale","quarantine","retire"):
 lease=host_lease.HostLease("host-one","incumbent",1,host_lease.now_stamp(),host_lease.now_stamp(time.time()+900))
 leases[prefix]=plane.publish_hive_lease(prefix,lease,expected="",operation="adopt")
print(json.dumps(leases))
"""
    result = frame_process(b, create)
    assert result.returncode == 0, result.stderr + result.stdout
    incumbent = json.loads(result.stdout)
    attempt = """
import sys,os,time
from pathlib import Path
from beadhive import host,host_lease
from beadhive.hq_control_plane import GitControlPlane
host.host_id=lambda:"host-two"
host.signing_key=lambda:sys.argv[2]
plane=GitControlPlane(Path(sys.argv[1]),authority_anchor=os.environ["FRAME_AUTHORITY_ANCHOR"])
lease=host_lease.HostLease("host-two","challenger",2,host_lease.now_stamp(),host_lease.now_stamp(time.time()+900))
print(plane.publish_hive_lease(sys.argv[3],lease,expected=sys.argv[4],operation="adopt"))
"""
    result = frame_process(second, attempt, "bh", incumbent["bh"])
    assert result.returncode != 0 and "pre-receive hook declined" in result.stderr
    assert git(b["remote"], "rev-parse", "refs/bh/lease/bh") == incumbent["bh"]
    # Signed real receipt aging, rather than replacing evictable() or server clocks.
    b["accept"](4, state_seen="active", leaseDurationSeconds=6, intervalSeconds=1)
    time.sleep(7)
    for prefix in ("stale", "quarantine", "retire"):
        if prefix == "quarantine":
            apply(b, "quarantine")
        elif prefix == "retire":
            apply(b, "retire")
        result = frame_process(second, attempt, prefix, incumbent[prefix])
        assert result.returncode == 0, result.stderr + result.stdout
        assert git(b["remote"], "rev-parse", f"refs/bh/lease/{prefix}") == result.stdout.strip()
        current = b["plane"].read_hive_lease(prefix, holder_identity="host-two")
        assert current.host_id == "host-two" and current.epoch == 2
