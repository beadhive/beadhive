"""Receiver-free hive-lease acceptance in signed mode (bh-qv8ig, 0.22.8 bridge).

Real Ed25519 frame signatures, the real request verifier, the real resolver and — for the
two-adopter race — real ``refs/bh/epoch`` CAS against scratch bare Git remotes. Only the SQL
connection is replaced (the composite returns gathered evidence). Every test runs with
BH_HOME/HOME/Git config sandboxed for its whole duration; nothing here can reach a live host
lease, HQ or hive remote.
"""

from __future__ import annotations

import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import host_adopt, host_fence
from beadhive.host_fence import EpochFence
from beadhive.host_lease import HostLeaseRejected
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_control_plane import ControlPlaneError, HqLeaseUnknown, SqlControlPlane
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_signed_hive_lease import (
    SignedHiveLeaseConflict,
    proposal_revision,
    resolve,
)
from beadhive.hq_sql_runtime import (
    HIVE_LEASE_ENV,
    LIVENESS_ENV,
    HiveLeaseEvidence,
    PrincipalBinding,
    SqlRuntimeError,
    hive_lease_mode,
    newest_signed_heartbeat,
)
from beadhive.hq_sql_signatures import canonical, fingerprint, sign_heartbeat, sign_hive_request

NOW = float(int(time.time()))
RELEASE = {"id": "fixture", "digest": "sha256:" + "1" * 64}
REV_OLD = "a" * 32  # authority revision whose signed policy lacks the hive (agent-hitch today)
REV_NEW = "b" * 32  # after the operator adds the hive's frame_policy and renews authority
POLICY = {"config_revision": "desired-1", "requires": {"arch": "x86_64-linux"}}


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Whole-test sandbox: never undone, so no step can reach the operator's real state."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("[user]\n\tname = t\n\temail = t@example.invalid\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bh-home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv(LIVENESS_ENV, raising=False)
    monkeypatch.delenv(HIVE_LEASE_ENV, raising=False)
    monkeypatch.delenv("BH_HQ_AUTHORITY_ENFORCE", raising=False)


def _keypair(path):
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


def _frame(tmp_path, name="a", host="host-a", *, beadyard=None):
    key = tmp_path / f"{name}.key"
    public = _keypair(key)
    signer = fingerprint(public)
    principal = f"frame_{name}"
    route = PrincipalBinding(
        principal, f"frame-{name}", host, f"vm-{name}", 1, f"hq_live_inbox_{principal}_1", signer
    )
    authority = {
        "frame_id": f"frame-{name}",
        "holder_identity": host,
        "instance_ref": f"vm-{name}",
        "key_fingerprint": signer,
        "epoch": 1,
        "audience": "fixture-fleet",
        "config_revision": "desired-1",
        "candidate_expires_at": None,
    }
    if beadyard is not None:
        authority["beadyard_id"] = beadyard
    record = {
        "authority": authority,
        "public_key": public,
        "state": "active",
        "cordoned": False,
        "desired": {
            "declared": True,
            "release": RELEASE,
            "profile": "fixture",
            "caps": {"arch": "x86_64-linux", "max_sessions": 4},
        },
    }

    def propose(
        epoch,
        *,
        prefix="ah",
        operation="adopt",
        revision=REV_NEW,
        adopted=NOW - 60,
        tenure=7200,
        request_id=None,
        **overrides,
    ):
        request_id = request_id or f"00000000-0000-4000-8000-{epoch:04d}{len(operation):08d}"
        lease = {
            "host_id": "" if operation == "release" else host,
            "label": "" if operation == "release" else name,
            "epoch": epoch,
            "adopted_at": now_stamp(adopted),
            "expires_at": now_stamp(adopted + tenure),
        }
        request = {
            "domain": "beadhive/sql-hive-lease/v2" if beadyard else "beadhive/sql-hive-lease/v1",
            "request_id": request_id,
            "principal": principal,
            "frame_id": f"frame-{name}",
            "holder_identity": host,
            "instance_ref": f"vm-{name}",
            "epoch": 1,
            "key_fingerprint": signer,
            "audience": "fixture-fleet",
            "config_revision": "desired-1",
            "authority_revision": revision,
            "prefix": prefix,
            "expected_revision": "",
            "operation": operation,
            "force": False,
            "lease": lease,
            **overrides,
        }
        if beadyard is not None:
            request["beadyard_id"] = beadyard
        envelope = sign_hive_request(request, signing_key=str(key))
        body = canonical(envelope)
        import hashlib

        return (request_id, body, hashlib.sha256(body).hexdigest()), request

    def beat(renew_at):
        lease = HeartbeatLease(
            audience="fixture-fleet",
            frame_id=f"frame-{name}",
            holderIdentity=host,
            instance_ref=f"vm-{name}",
            key_id=signer,
            epoch=1,
            config_revision="desired-1",
            seq=1,
            renewTime=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(renew_at)),
            state_seen="active",
            release=RELEASE,
            report_digest="sha256:" + "2" * 64,
            conformance={"profile": "fixture", "status": "conformant", "checks": []},
        )
        return canonical(sign_heartbeat(lease, signing_key=str(key)))

    return SimpleNamespace(
        route=route, record=record, propose=propose, beat=beat, host=host, key=key, name=name
    )


def _evidence(*rows, policies_at=None, registration=None):
    if policies_at is None:
        policies_at = {REV_NEW: {"ah": POLICY}, REV_OLD: {"bh": POLICY}}
    return HiveLeaseEvidence(tuple(rows), policies_at, registration)


def _resolve(frame, fence, *rows, receiver=None, slot="active", **evidence):
    return resolve(
        prefix="ah",
        route=frame.route,
        slot=slot,
        record=frame.record,
        fence=fence,
        evidence=_evidence(*rows, **evidence),
        receiver=receiver,
    )


# ---- resolver: what counts ---------------------------------------------------------------


def test_adopt_proposal_at_the_fence_epoch_is_the_lease(tmp_path):
    a = _frame(tmp_path)
    row, request = a.propose(22)
    result = _resolve(a, EpochFence(22, "host-a"), row)
    assert result.source == "proposal"
    assert result.lease.held_by("host-a") and result.lease.epoch == 22
    assert result.lease.advisory_expiry
    # The same digest the receiver would record as the protected lease revision.
    assert result.revision == proposal_revision(request)


def test_stranded_agent_hitch_is_recoverable_without_replay(tmp_path):
    """Criterion 8 / 4: fence at 21, no lease; proposals 18-21 were signed when the signed
    hive policy did not cover the prefix. They never count — not even after policy is added —
    and a fresh unforced adopt at the next epoch is recognised."""
    a = _frame(tmp_path)
    stranded = [a.propose(epoch, revision=REV_OLD)[0] for epoch in (18, 19, 20, 21)]
    fence = EpochFence(21, "host-a")
    assert _resolve(a, fence, *stranded).lease is None
    with_policy = {REV_OLD: {"bh": POLICY}, REV_NEW: {"ah": POLICY}}
    assert _resolve(a, fence, *stranded, policies_at=with_policy).lease is None
    # Recovery: the two-phase adopt derives max(fence, lease) + 1 and installs it first.
    epoch = host_adopt._next_epoch(fence, None)
    assert epoch == 22
    fresh, _ = a.propose(epoch, revision=REV_NEW)
    result = _resolve(a, EpochFence(epoch, "host-a"), *stranded, fresh, policies_at=with_policy)
    assert result.source == "proposal" and result.lease.epoch == 22
    assert result.lease.held_by("host-a")


def test_superseded_epoch_is_never_the_lease(tmp_path):
    a = _frame(tmp_path)
    old, _ = a.propose(21)
    assert _resolve(a, EpochFence(22, "host-a"), old).lease is None


@pytest.mark.parametrize(
    "mutation",
    [
        "tampered",
        "digest",
        "wrong-key",
        "force",
        "foreign-principal",
        "wrong-prefix",
        "unknown-revision",
        "overlong-tenure",
        "not-canonical",
        "oversize",
    ],
)
def test_unqualified_proposals_do_not_count(tmp_path, mutation):
    a = _frame(tmp_path)
    other = _frame(tmp_path, "b", "host-a")  # same holder name, different key
    (rid, body, sha), _ = a.propose(22)
    row = {
        "tampered": lambda: (rid, body.replace(b'"epoch":22', b'"epoch":23'), sha),
        "digest": lambda: (rid, body, "0" * 64),
        "wrong-key": lambda: other.propose(22)[0],
        "force": lambda: a.propose(22, force=True)[0],
        "foreign-principal": lambda: a.propose(22, principal="frame_z")[0],
        "wrong-prefix": lambda: a.propose(22, prefix="bh")[0],
        "unknown-revision": lambda: a.propose(22, revision="c" * 32)[0],
        "overlong-tenure": lambda: a.propose(22, tenure=86401)[0],
        "not-canonical": lambda: (rid, body.replace(b"{", b"{ ", 1), sha),
        "oversize": lambda: (rid, b" " * 70000, sha),
    }[mutation]()
    assert _resolve(a, EpochFence(22, "host-a"), row).lease is None


def test_candidate_slot_and_unregistered_bound_adopt_do_not_count(tmp_path):
    a = _frame(tmp_path)
    row, _ = a.propose(22)
    assert _resolve(a, EpochFence(22, "host-a"), row, slot="candidate").lease is None
    bound = _frame(tmp_path, "c", "host-c", beadyard="c1ed115f-c478-47ae-a4de-ac1fc2e1474d")
    bound_row, _ = bound.propose(22)
    fence = EpochFence(22, "host-c")
    assert _resolve(bound, fence, bound_row).lease is None
    registered = _resolve(bound, fence, bound_row, registration=bound.route.signer_fingerprint)
    assert registered.lease.held_by("host-c")


def test_unmet_hive_requirements_do_not_count(tmp_path):
    a = _frame(tmp_path)
    row, _ = a.propose(22)
    arm = {REV_NEW: {"ah": {**POLICY, "requires": {"arch": "aarch64-linux"}}}}
    assert _resolve(a, EpochFence(22, "host-a"), row, policies_at=arm).lease is None


# ---- resolver: ties and conflicts fail closed --------------------------------------------


def test_two_tenures_at_one_epoch_fail_closed(tmp_path):
    a = _frame(tmp_path)
    first, _ = a.propose(22, adopted=NOW - 60, request_id="00000000-0000-4000-8000-000000000001")
    second, _ = a.propose(22, adopted=NOW - 30, request_id="00000000-0000-4000-8000-000000000002")
    with pytest.raises(SignedHiveLeaseConflict, match="conflicting"):
        _resolve(a, EpochFence(22, "host-a"), first, second)


def test_renewal_of_the_same_tenure_is_not_a_conflict(tmp_path):
    a = _frame(tmp_path)
    adopt, _ = a.propose(22, tenure=3600)
    renew, request = a.propose(22, operation="renew", tenure=7200)
    result = _resolve(a, EpochFence(22, "host-a"), adopt, renew)
    assert result.revision == proposal_revision(request)
    assert result.lease.expires_at == now_stamp(NOW - 60 + 7200)


def test_matching_release_frees_and_mismatched_release_fails_closed(tmp_path):
    a = _frame(tmp_path)
    adopt, _ = a.propose(22)
    release, _ = a.propose(22, operation="release")
    released = _resolve(a, EpochFence(22, "host-a"), adopt, release)
    assert released.lease.is_tombstone and released.lease.epoch == 22
    stray, _ = a.propose(22, operation="release", adopted=NOW - 999)
    with pytest.raises(SignedHiveLeaseConflict, match="release"):
        _resolve(a, EpochFence(22, "host-a"), adopt, stray)


def test_receiver_row_at_fence_epoch_counts_older_row_is_superseded(tmp_path):
    a = _frame(tmp_path)
    row_lease = HostLease("host-a", "a", 221, now_stamp(NOW - 60), now_stamp(NOW - 30))
    kept = _resolve(a, EpochFence(221, "host-a"), receiver=("rev-221", row_lease))
    assert kept.source == "receiver" and kept.revision == "rev-221"
    assert kept.lease.held_by("host-a")  # advisory: the stale expiry is only a hint
    # bh-skls today: protected row at epoch 2, fence at 3 -> the row is superseded.
    stale = HostLease("host-a", "a", 2, now_stamp(NOW - 60), now_stamp(NOW + 60))
    assert _resolve(a, EpochFence(3, "host-a"), receiver=("rev-2", stale)).lease is None


def test_receiver_row_contradicting_the_fence_fails_closed(tmp_path):
    a = _frame(tmp_path)
    row_lease = HostLease("host-z", "z", 22, now_stamp(NOW - 60), now_stamp(NOW + 60))
    with pytest.raises(SignedHiveLeaseConflict, match="different host"):
        _resolve(a, EpochFence(22, "host-a"), receiver=("rev", row_lease))


def test_receiver_and_proposal_for_one_tenure_agree(tmp_path):
    a = _frame(tmp_path)
    row, request = a.propose(22)
    accepted = HostLease(**request["lease"])
    result = _resolve(
        a, EpochFence(22, "host-a"), row, receiver=(proposal_revision(request), accepted)
    )
    assert result.revision == proposal_revision(request)


def test_never_fenced_is_no_lease(tmp_path):
    a = _frame(tmp_path)
    row, _ = a.propose(1)
    assert _resolve(a, None, row).lease is None


# ---- two adopters race on a real refs/bh/epoch CAS ---------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_two_adopters_race_and_exactly_one_holds(tmp_path):
    """Criterion 7: no receiver, no global lease CAS. Two frames race the same hive from the
    stranded state; both even publish signed proposals. Exactly one is the holder from every
    reader's point of view, and the loser cannot seize it."""
    remote = tmp_path / "hive.git"
    _git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    clones = {}
    for name in ("a", "b"):
        clone = tmp_path / f"clone-{name}"
        _git("clone", "-q", str(remote), str(clone), cwd=tmp_path)
        clones[name] = clone
    seed = host_fence.install_fence(
        "origin", EpochFence(21, "host-a"), expected="", cwd=clones["a"]
    )
    assert seed
    frames = {"a": _frame(tmp_path, "a", "host-a"), "b": _frame(tmp_path, "b", "host-b")}
    barrier = threading.Barrier(2)
    outcome = {}

    def adopter(name):
        frame = frames[name]
        sha, fence = host_fence.read_fence("origin", cwd=clones[name])
        epoch = host_adopt._next_epoch(fence, None)
        barrier.wait()
        try:
            host_fence.install_fence(
                "origin", EpochFence(epoch, frame.host), expected=sha, cwd=clones[name]
            )
            outcome[name] = ("won", epoch)
        except host_fence.FenceRejected:
            outcome[name] = ("lost", epoch)
        # Adversarial: both publish a signed proposal for the epoch they computed.
        outcome[name + "-row"] = frame.propose(epoch)[0]

    threads = [threading.Thread(target=adopter, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcome[n][0] for n in ("a", "b")) == ["lost", "won"]
    _sha, fence = host_fence.read_fence("origin", cwd=clones["a"])
    assert fence.epoch == 22
    holders = set()
    for name, frame in frames.items():
        result = _resolve(frame, fence, outcome[name + "-row"])
        lease = result.lease
        assert lease is not None and not lease.is_tombstone
        holders.add(lease.host_id)
        assert lease.held_by(frame.host) == (outcome[name][0] == "won")
    assert holders == {fence.host_id}
    # The loser's view names the winner as a live holder (not "free"): an unforced adopt
    # by the loser is refused by the existing foreign-lease check.
    loser = next(n for n in ("a", "b") if outcome[n][0] == "lost")
    view = _resolve(frames[loser], fence, outcome[loser + "-row"]).lease
    assert view.host_id == fence.host_id and not view.is_expired(NOW)


# ---- the default fence reader (no host_fence / registry import, bh-nyuyy.3) ---------------


def _hive_layout(tmp_path, monkeypatch, *, clone=True):
    from beadhive import hq_control_plane

    workspace = tmp_path / "ws"
    remote = tmp_path / "hive-remote.git"
    _git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    checkout = workspace / "github" / "acme" / "hive"
    checkout.parent.mkdir(parents=True)
    if clone:
        _git("clone", "-q", str(remote), str(checkout), cwd=tmp_path)
    entry = {"prefix": "hv", "provider": "github", "org": "acme", "repo": "hive"}
    ports = SimpleNamespace(
        load=lambda: {"managed_repos": [entry]},
        hq_dir=lambda: tmp_path / "hq",
        _workspace_root_for_transition=lambda: workspace,
    )
    monkeypatch.setattr(hq_control_plane, "config", ports)
    return hq_control_plane, checkout


def test_default_fence_reader_matches_host_fence_read_fence(tmp_path, monkeypatch):
    plane_module, checkout = _hive_layout(tmp_path, monkeypatch)
    assert plane_module._registry_fence_reader("hv") is None  # never fenced
    host_fence.install_fence("origin", EpochFence(7, "host-a", 2), expected="", cwd=checkout)
    fence = plane_module._registry_fence_reader("hv")
    assert fence == host_fence.read_fence("origin", cwd=checkout)[1] == EpochFence(7, "host-a", 2)


def test_default_fence_reader_fails_closed_without_a_checkout(tmp_path, monkeypatch):
    plane_module, checkout = _hive_layout(tmp_path, monkeypatch, clone=False)
    with pytest.raises(ControlPlaneError, match="not cloned on this host"):
        plane_module._registry_fence_reader("hv")
    # A directory nested inside some other checkout is still not this hive's clone.
    _git("init", "-q", str(checkout.parent), cwd=tmp_path)
    checkout.mkdir()
    with pytest.raises(ControlPlaneError, match="not cloned on this host"):
        plane_module._registry_fence_reader("hv")
    with pytest.raises(ControlPlaneError, match="not exactly one managed hive"):
        plane_module._registry_fence_reader("other")


def test_managed_hive_dir_matches_registry_layout(tmp_path, monkeypatch):
    from beadhive import hq_control_plane, registry

    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    triplet = {"prefix": "hv", "provider": "github", "org": "acme", "repo": "hive"}
    hq = {"prefix": "hq", "kind": registry.HQ_KIND, "provider": "local", "org": "f", "repo": "hq"}
    for entry in (triplet, hq):
        assert hq_control_plane._managed_hive_dir(entry) == registry.hive_dir(entry)


def test_epoch_fence_contract_is_shared_with_host_fence():
    from beadhive import host_lease_contracts

    assert host_fence.EpochFence is host_lease_contracts.EpochFence
    assert host_fence.EPOCH_REF == host_lease_contracts.EPOCH_REF == "refs/bh/epoch"


# ---- the control plane: readers, mode switch, publish ------------------------------------


def _plane(frame, *, fence, rows=(), settings=None, receiver_row=None, policies=None, **evidence):
    observation = newest_signed_heartbeat(
        [(frame.beat(NOW - 5),)], frame.route, frame.record, now=NOW
    )
    calls = SimpleNamespace(composite=[], fence=0)

    def composite(prefix=None, enforce=True, hive_lease_epoch=None, **_):
        calls.composite.append(hive_lease_epoch)
        result = (
            "head",
            {},
            frame.route,
            "active",
            frame.record,
            SimpleNamespace(beadyard_id=None),
            policies if policies is not None else {"ah": {**POLICY, "valid_until": NOW + 3600}},
            observation,
            receiver_row,
        )
        if hive_lease_epoch is None:
            return result
        return (*result, _evidence(*rows, **evidence))

    def read_fence(_prefix):
        calls.fence += 1
        return fence

    plane = SqlControlPlane(
        settings if settings is not None else {"liveness": "signed"},
        clock=lambda: NOW,
        fence_reader=read_fence,
    )
    plane._runtime_authority = lambda: SimpleNamespace(read_frame_composite=composite)
    return plane, calls


def test_signed_reader_passes_current_hive_lease_holder_from_a_proposal(tmp_path):
    a = _frame(tmp_path)
    row, request = a.propose(22)
    plane, calls = _plane(a, fence=EpochFence(22, "host-a"), rows=[row])
    revision, lease = plane.read_hive_lease_record("ah", holder_identity="host-a")
    assert revision == proposal_revision(request)
    assert lease.held_by("host-a") and lease.epoch == 22
    assert calls.composite == [22]
    assert plane.read_hive_lease_record("ah", holder_identity="host-b")[1] is None


def test_signed_reader_refuses_a_cordoned_or_unpolicied_holder(tmp_path):
    a = _frame(tmp_path)
    row, _ = a.propose(22)
    plane, _ = _plane(a, fence=EpochFence(22, "host-a"), rows=[row], policies={})
    assert plane.read_hive_lease_record("ah", holder_identity="host-a")[1] is None
    a.record["cordoned"] = True
    plane, _ = _plane(a, fence=EpochFence(22, "host-a"), rows=[row])
    assert plane.read_hive_lease_record("ah", holder_identity="host-a")[1] is None


def test_conflict_surfaces_as_a_fail_closed_control_plane_error(tmp_path):
    a = _frame(tmp_path)
    one, _ = a.propose(22, adopted=NOW - 60, request_id="00000000-0000-4000-8000-000000000001")
    two, _ = a.propose(22, adopted=NOW - 30, request_id="00000000-0000-4000-8000-000000000002")
    plane, _ = _plane(a, fence=EpochFence(22, "host-a"), rows=[one, two])
    with pytest.raises(ControlPlaneError, match="fails closed"):
        plane.read_hive_lease_record("ah", holder_identity="host-a")


def test_unreadable_fence_fails_closed(tmp_path):
    a = _frame(tmp_path)

    def broken(_prefix):
        raise OSError("remote unreachable")

    plane, _ = _plane(a, fence=None)
    plane.fence_reader = broken
    with pytest.raises(ControlPlaneError, match="refs/bh/epoch"):
        plane.read_hive_lease_record("ah", holder_identity="host-a")


def test_older_reader_fails_closed_on_a_proposal_only_lease(tmp_path, monkeypatch):
    """Criterion 9. ``BH_HQ_SQL_HIVE_LEASE=receiver`` is exactly the 0.22.7 signed reader
    (protected receiver row only): a lease that exists only as a signed proposal is no
    lease to it, so every write gate refuses rather than grants."""
    a = _frame(tmp_path)
    row, _ = a.propose(22)
    monkeypatch.setenv(HIVE_LEASE_ENV, "receiver")
    plane, calls = _plane(a, fence=EpochFence(22, "host-a"), rows=[row])
    assert plane.read_hive_lease_record("ah", holder_identity="host-a") == ("", None)
    assert plane.read_hive_lease_record("ah") == ("", None)
    assert calls.fence == 0 and calls.composite == [None, None]


def test_receiver_liveness_is_unchanged(tmp_path, monkeypatch):
    a = _frame(tmp_path)
    row, _ = a.propose(22)
    plane, calls = _plane(a, fence=EpochFence(22, "host-a"), rows=[row], settings={})
    assert plane.read_hive_lease_record("ah", holder_identity="host-a") == ("", None)
    assert calls.fence == 0
    monkeypatch.setenv(HIVE_LEASE_ENV, "proposal")  # needs signed liveness to mean anything
    assert hive_lease_mode({}) == "receiver"


@pytest.mark.parametrize("value", ["", "Proposal", "signed", "off"])
def test_invalid_hive_lease_override_fails_loudly(monkeypatch, value):
    monkeypatch.setenv(HIVE_LEASE_ENV, value)
    with pytest.raises(SqlRuntimeError, match=HIVE_LEASE_ENV):
        hive_lease_mode({"liveness": "signed"})
    with pytest.raises(ControlPlaneError, match=HIVE_LEASE_ENV):
        SqlControlPlane({"liveness": "signed"})


def _publishing_plane(frame, monkeypatch, *, fence, rows):
    plane, _ = _plane(frame, fence=fence, rows=rows)
    plane.settings["runtime"] = {"operation_timeout": 15}
    monkeypatch.setattr("beadhive.host.signing_key", lambda: str(frame.key))
    monkeypatch.setattr(
        plane,
        "propose_hive_lease",
        lambda *a, **k: ("00000000-0000-4000-8000-000000000022", "sha", frame.route, "aud"),
    )
    return plane


def test_signed_publish_needs_no_receiver_result(tmp_path, monkeypatch):
    a = _frame(tmp_path)
    row, request = a.propose(22)
    plane = _publishing_plane(a, monkeypatch, fence=EpochFence(22, "host-a"), rows=[row])
    original = plane._runtime_authority

    def runtime():
        handle = original()
        handle.read_public_result = lambda *a, **k: pytest.fail("polled the receiver")
        return handle

    plane._runtime_authority = runtime
    lease = HostLease(**request["lease"])
    assert plane.publish_hive_lease(
        "ah", lease, expected="", operation="adopt"
    ) == proposal_revision(request)


def test_signed_publish_that_is_not_recognised_says_so(tmp_path, monkeypatch):
    a = _frame(tmp_path)
    row, request = a.propose(22, revision=REV_OLD)  # no signed policy for ah at that revision
    plane = _publishing_plane(a, monkeypatch, fence=EpochFence(22, "host-a"), rows=[row])
    with pytest.raises(ControlPlaneError, match="not the lease at the current epoch fence") as exc:
        plane.publish_hive_lease(
            "ah", HostLease(**request["lease"]), expected="", operation="adopt"
        )
    assert not isinstance(exc.value, HqLeaseUnknown)


# ---- the diagnosed cause: refuse before the fence moves ---------------------------------


def test_require_hive_policy_names_the_placement_dependency(tmp_path):
    a = _frame(tmp_path)
    plane, _ = _plane(a, fence=None, policies={"bh": {**POLICY, "valid_until": NOW + 3600}})
    with pytest.raises(ControlPlaneError, match="PLACEMENT: hive ah has no operator-signed"):
        plane.require_hive_policy("ah")
    assert plane.require_hive_policy("bh")["config_revision"] == "desired-1"


def test_adopt_without_hive_policy_never_moves_the_fence(tmp_path, monkeypatch):
    a = _frame(tmp_path)
    plane, _ = _plane(a, fence=None, policies={})
    hive = tmp_path / "hive"
    (hive / ".git").mkdir(parents=True)
    monkeypatch.setattr("beadhive.frame_eligibility.require_eligible", lambda *a, **k: None)
    monkeypatch.setattr(
        host_fence, "read_fence", lambda *a, **k: ("sha21", EpochFence(21, "host-a"))
    )
    monkeypatch.setattr("beadhive.host_lease.read", lambda *a, **k: None)
    monkeypatch.setattr("beadhive.host_lease._frame_plane", lambda cwd: plane)
    monkeypatch.setattr(
        host_fence, "install_fence", lambda *a, **k: pytest.fail("fence moved without policy")
    )
    with pytest.raises(HostLeaseRejected, match="before the fence moved"):
        host_adopt.adopt(
            prefix="ah",
            hive_remote="origin",
            hq_remote="origin",
            hive_cwd=hive,
            hq_cwd=tmp_path,
            host_id="host-a",
            label="a",
        )


# ---- the composite gathers evidence in its one transaction ------------------------------


class _Cursor:
    def __init__(self, world):
        self.world, self.rows = world, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        world = self.world
        world.statements.append((sql, params))
        if sql.startswith("SELECT CURRENT_USER"):
            self.rows = [("frame_a@localhost", "rt", "main", "2.3.5")]
        elif sql.startswith("SELECT DOLT_HASHOF"):
            self.rows = [(world.head,)]
        elif sql.startswith("START TRANSACTION"):
            self.rows = []
        elif "FROM hq_principal_registry" in sql:
            self.rows = [tuple(vars(world.route).values())]
        elif "kind='heartbeat'" in sql:
            self.rows = []
        elif "kind='hive_lease'" in sql:
            self.rows = list(world.inbox)
        elif sql.startswith("SELECT HAS_ANCESTOR"):
            self.rows = [(1 if params[1] in world.ancestors else 0,)]
        elif "JOIN hq_live_results" in sql:
            assert "kind='registration'" in sql and params[-1] == world.route.signer_fingerprint
            self.rows = [(world.route.signer_fingerprint,)] if world.registered else []
        elif "FROM hq_live_hive_leases" in sql:
            self.rows = []
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def _composite(tmp_path, monkeypatch, inbox, *, registered=True):
    from beadhive import hq_sql_runtime as runtime

    a = _frame(tmp_path)
    head = "h" * 32
    world = SimpleNamespace(
        route=a.route,
        head=head,
        inbox=inbox(a),
        ancestors={REV_NEW, REV_OLD},
        registered=registered,
        statements=[],
    )
    state = {"frames": {"frame-a": {"active": a.record, "candidate": None}}, "expires_at": 1e12}
    signed = {REV_NEW: {"ah": POLICY}, REV_OLD: {"bh": POLICY}, head: {"ah": POLICY}}
    settings = {
        "runtime": {"user": "frame_a", "database": "rt"},
        "liveness": "signed",
        "runtime_backend_identity": "backend",
        "runtime_generation": "generation",
    }
    authority = runtime.SqlRuntimeAuthority(settings, broker=object(), clock=lambda: NOW)

    class _Connection:
        def cursor(self):
            return _Cursor(world)

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        authority, "_open", lambda deadline=None: (_Connection(), time.monotonic() + 30)
    )
    monkeypatch.setattr(
        authority, "verified_state_at", lambda *a, **k: (state, ("b", "g", "c"), signed[head])
    )
    monkeypatch.setattr(
        authority,
        "_signed_authority_at",
        lambda cursor, revision: ("backend", "generation", 1, (), {}, signed[revision]),
    )
    monkeypatch.setattr(authority, "load_config_at", lambda *a, **k: "snapshot")
    monkeypatch.setattr(runtime, "project_hive_policies", lambda *a, **k: signed[head])
    for fence in (
        "fresh_config_head_fence",
        "fresh_runtime_head_fence",
        "fresh_hive_lease_fence",
    ):
        monkeypatch.setattr(authority, fence, lambda *a, **k: None)
    return a, authority, world


def test_composite_returns_prefiltered_evidence_with_signed_history(tmp_path, monkeypatch):
    def inbox(a):
        return [
            a.propose(22, revision=REV_NEW)[0],
            a.propose(21, revision=REV_OLD)[0],  # wrong epoch: filtered
            a.propose(22, prefix="bh", revision=REV_OLD)[0],  # wrong prefix: filtered
            ("00000000-0000-4000-8000-00000000dead", b"{not json", "x"),
        ]

    a, authority, world = _composite(tmp_path, monkeypatch, inbox)
    result = authority.read_frame_composite(prefix="ah", hive_lease_epoch=22)
    assert len(result) == 10
    evidence = result[9]
    assert [row[0] for row in evidence.rows] == [world.inbox[0][0]]
    assert evidence.policies_at == {REV_NEW: {"ah": POLICY}}
    assert evidence.registration_signer == a.route.signer_fingerprint
    # Without an epoch the composite keeps its historic 9-tuple and reads no proposals.
    world.statements.clear()
    assert len(authority.read_frame_composite(prefix="ah")) == 9
    assert not any("kind='hive_lease'" in sql for sql, _ in world.statements)


def test_signed_policies_at_requires_history_and_the_pinned_backend(tmp_path, monkeypatch):
    a, authority, world = _composite(tmp_path, monkeypatch, lambda a: [])
    cursor = _Cursor(world)
    assert authority.signed_policies_at(cursor, world.head, REV_NEW) == {"ah": POLICY}
    assert authority.signed_policies_at(cursor, world.head, "not-a-revision") is None
    world.ancestors = set()
    assert authority.signed_policies_at(cursor, world.head, REV_NEW) is None  # forked/unknown
    world.ancestors = {REV_NEW}
    monkeypatch.setattr(
        authority,
        "_signed_authority_at",
        lambda cursor, revision: ("other-backend", "generation", 1, (), {}, {"ah": POLICY}),
    )
    assert authority.signed_policies_at(cursor, world.head, REV_NEW) is None


def test_too_many_proposal_revisions_fail_closed(tmp_path, monkeypatch):
    from beadhive import hq_sql_runtime as runtime

    monkeypatch.setattr(runtime, "MAX_PROPOSAL_REVISIONS", 1)

    def inbox(a):
        return [
            a.propose(22, revision=REV_NEW, request_id="00000000-0000-4000-8000-000000000001")[0],
            a.propose(22, revision=REV_OLD, request_id="00000000-0000-4000-8000-000000000002")[0],
        ]

    _a, authority, _world = _composite(tmp_path, monkeypatch, inbox)
    with pytest.raises(SqlRuntimeError, match="too many"):
        authority.read_frame_composite(prefix="ah", hive_lease_epoch=22)
