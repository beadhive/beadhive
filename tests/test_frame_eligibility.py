"""Frame predicate boundaries and action fencing; no production stores or subprocess mocks."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer import Exit
from typer.testing import CliRunner

from beadhive import config, guard, host, host_adopt, host_lease, hosts
from beadhive import frame_eligibility as policy
from beadhive.host_heartbeat_core import HeartbeatLease, VerifiedObservation
from beadhive.hq_framelease_contracts import DOMAIN_V2


@pytest.fixture
def candidate():
    release = {"id": "release", "digest": "sha256:" + "1" * 64}
    caps = dict(
        isolation="kvm",
        trust_zone="self-hosted",
        arch="x86_64",
        harnesses=["codex"],
        max_sessions=2,
    )
    frame = hosts.HostManifest(
        host_id="host",
        frame_id="frame",
        state="active",
        release=release,
        capabilities=caps,
        instance_ref="vm",
        label="fixture",
        os="linux",
        arch="x86_64",
        role="executor",
        identity={"kind": "none"},
    )
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
        release=release,
        state_seen="active",
        conformance={
            "profile": "profile",
            "status": "conformant",
            "checks": [{"id": "required", "status": "pass"}],
        },
        report_digest="sha256:" + "2" * 64,
    )
    beat = VerifiedObservation("fresh", True, True, 1.0, lease)
    desired = dict(
        declared=True,
        state="active",
        cordoned=False,
        release=release,
        caps=caps,
        profile="profile",
        authority={"holder_identity": "host", "instance_ref": "vm"},
    )
    return frame, policy.EligibilityFacts(beat, desired)


def test_enrolled_frame_requires_host_local_beadyard_pin(candidate, tmp_path, monkeypatch):
    frame, facts = candidate
    owner = str(uuid4())
    frame = frame.model_copy(update={"beadyard_id": owner})
    lease = HeartbeatLease.model_validate(
        {
            **facts.observation.lease.model_dump(mode="json", exclude_none=True),
            "domain": DOMAIN_V2,
            "beadyard_id": owner,
        }
    )
    facts = replace(
        facts,
        observation=replace(facts.observation, lease=lease),
        desired={
            **facts.desired,
            "authority": {**facts.desired["authority"], "beadyard_id": owner},
        },
    )
    monkeypatch.setattr(config, "hq_dir", lambda: tmp_path)
    monkeypatch.setattr(hosts, "load", lambda _root, _host: frame)
    monkeypatch.setattr(policy, "load_facts", lambda *_args, **_kwargs: facts)
    bootstrap = {"host": {"frame_id": "frame"}, "hq": {"beadyard_id": owner}}
    monkeypatch.setattr(config, "load_host", lambda: bootstrap)
    assert policy.decision_for("host").allowed
    bootstrap["hq"]["beadyard_id"] = str(uuid4())
    assert policy.decision_for("host").reason == "beadyard_binding"
    del bootstrap["hq"]["beadyard_id"]
    assert policy.decision_for("host").reason == "beadyard_binding"


@pytest.mark.parametrize(
    "predicate",
    [
        "authority_available",
        "admitted_active",
        "not_cordoned",
        "authenticated_fresh_heartbeat",
        "release_matches",
        "conformance_pass",
        "capabilities_match_admission",
        "hive_requirements",
        "executor_role",
        "dispatch_enabled",
    ],
)
def test_each_predicate_explains_failure(candidate, predicate):
    frame, facts = candidate
    assert policy.eligible(frame, {}, facts).allowed
    hive = {}
    if predicate == "authority_available":
        facts = replace(facts, available=False)
    elif predicate == "admitted_active":
        facts = replace(facts, desired={**facts.desired, "state": "parked"})
    elif predicate == "not_cordoned":
        facts = replace(facts, desired={**facts.desired, "cordoned": True})
    elif predicate == "authenticated_fresh_heartbeat":
        facts = replace(facts, observation=replace(facts.observation, verified=False))
    elif predicate == "release_matches":
        frame = frame.model_copy(update={"release": None})
    elif predicate == "conformance_pass":
        facts = replace(facts, desired={**facts.desired, "profile": "wrong"})
    elif predicate == "capabilities_match_admission":
        facts = replace(facts, desired={**facts.desired, "caps": {}})
    elif predicate == "hive_requirements":
        hive = {"requires": {"isolation": "container"}}
    elif predicate == "executor_role":
        frame = frame.model_copy(update={"role": "viewer"})
    else:
        facts = replace(facts, dispatch_enabled=False)
    decision = policy.eligible(frame, hive, facts)
    assert not decision.allowed and predicate in decision.reason


@pytest.mark.parametrize(
    "age,allowed",
    [
        (299.999, True),
        (300, False),
        (301, False),
        (-1, False),
        (float("nan"), False),
        (float("inf"), False),
        (True, False),
    ],
)
def test_exclusive_ttl_boundary(candidate, age, allowed):
    frame, facts = candidate
    facts = replace(facts, observation=replace(facts.observation, age_seconds=age))
    assert policy.eligible(frame, {}, facts).allowed is allowed


@pytest.mark.parametrize(
    "requires", [None, [], {"harnesses": [[]]}, {"unknown": "anything"}, {"isolation": "kvm"}]
)
def test_container_never_satisfies_kvm_or_malformed_requirements(candidate, requires):
    frame, facts = candidate
    caps = {**facts.desired["caps"], "isolation": "container"}
    frame = frame.model_copy(update={"capabilities": hosts.FrameCapabilities(**caps)})
    facts = replace(facts, desired={**facts.desired, "caps": caps})
    assert not policy.eligible(frame, {"requires": requires}, facts).allowed


def test_sql_and_bound_missing_manifest_never_allow_legacy(tmp_path, monkeypatch):
    for binding in [{"authority_anchor": "protected"}]:
        monkeypatch.setattr(config, "load_host", lambda binding=binding: {"hq": binding})
        assert not policy.decision_for("host", hq_dir=tmp_path).allowed
    monkeypatch.setattr(config, "load_host", lambda: {"hq": {"mode": "dolt-server"}})
    assert not policy.decision_for("host", hq_dir=tmp_path).allowed
    monkeypatch.setattr(config, "load_host", lambda: {"host": {"frame_id": "frame"}})
    assert not policy.decision_for("host", hq_dir=tmp_path).allowed


def test_missing_host_config_preserves_raw_legacy_recovery_but_denies_frames(
    candidate, tmp_path, monkeypatch
):
    from beadhive import hq_control_plane

    def absent():
        raise FileNotFoundError("host config absent")

    def unavailable(_root):
        raise ValueError("no protected binding")

    monkeypatch.setattr(config, "load_host", absent)
    assert policy.require_eligible("legacy", {"prefix": "bh"}, hq_dir=tmp_path) is None
    frame, _facts = candidate
    hosts.save(tmp_path, frame)
    monkeypatch.setattr(hq_control_plane, "control_plane", unavailable)
    decision = policy.decision_for(frame.host_id, hq_dir=tmp_path)
    assert not decision.allowed
    assert dict(decision.predicates)["authority_available"] is False
    with pytest.raises(policy.EligibilityError, match="authority_available"):
        policy.require_eligible(frame.host_id, hq_dir=tmp_path)


@pytest.mark.parametrize("error", [ValueError("invalid-config"), PermissionError("unreadable")])
def test_existing_invalid_or_unreadable_config_never_selects_legacy(error, tmp_path, monkeypatch):
    def rejected():
        raise error

    monkeypatch.setattr(config, "load_host", rejected)
    with pytest.raises(type(error)):
        policy.require_eligible("legacy", hq_dir=tmp_path)


def test_fleet_only_hive_requirements_and_unresolved_catalog(candidate, tmp_path, monkeypatch):
    from beadhive import registry

    frame, facts = candidate
    hosts.save(tmp_path, frame)
    monkeypatch.setattr(config, "hq_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "load_host", lambda: {})
    monkeypatch.setattr(host, "host_id", lambda: "host")
    monkeypatch.setattr(policy, "load_facts", lambda *a, **kw: facts)
    monkeypatch.setattr(config, "load", lambda: {"fleet_only": True})
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(
        registry,
        "entry_for_dir",
        lambda cfg, directory: (
            {"prefix": "bh", "requires": {"isolation": "container"}}
            if cfg.get("fleet_only")
            else None
        ),
    )
    with pytest.raises(policy.EligibilityError, match="hive_requirements"):
        policy.require_local("bh")
    monkeypatch.setattr(registry, "entry_for_dir", lambda *a: None)
    with pytest.raises(policy.EligibilityError, match="hive_catalog_available"):
        policy.require_local("missing")


@pytest.mark.parametrize("operation", ["adopt", "renew", "two_phase", "claim", "dispatch"])
def test_intake_refuses_before_action(operation, monkeypatch, tmp_path):
    from beadhive import localloop

    def deny(*a, **kw):
        raise policy.EligibilityError("frame ineligible: conformance_pass")

    monkeypatch.setattr(policy, "require_eligible", deny)
    monkeypatch.setattr(policy, "require_local", deny)
    if operation == "dispatch":
        keeper = localloop.EligibilityLeaseKeeper(localloop.NullLeaseKeeper(), "bh", {}, tmp_path)
        status = keeper.renew(active=True)
        assert not status.held and "conformance_pass" in status.detail
        return
    with pytest.raises((policy.EligibilityError, Exit)):
        if operation == "adopt":
            host_lease.adopt("missing", "bh", host_id="host", label="host", cwd=tmp_path)
        elif operation == "renew":
            host_lease.renew("missing", "bh", host_id="host", cwd=tmp_path)
        elif operation == "two_phase":
            host_adopt.adopt(
                prefix="bh",
                hive_remote="missing",
                hq_remote="missing",
                hive_cwd=tmp_path,
                hq_cwd=tmp_path,
                host_id="host",
                label="host",
            )
        else:
            guard.guard_primary("bh", cfg={})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "state,ready,expected",
    [
        ("quarantined", True, True),
        ("retired", True, True),
        ("parked", True, False),
        ("active", True, False),
        ("retired", False, False),
    ],
)
def test_protected_eviction_states(candidate, monkeypatch, tmp_path, state, ready, expected):
    from beadhive import hq_control_plane

    frame, facts = candidate
    hosts.save(tmp_path, frame)
    record = {"authority": {"holder_identity": "host"}, "state": state}
    plane = SimpleNamespace(
        load_host_manifest=lambda host_id: hosts.load(tmp_path, host_id),
        eligibility_authority_status=lambda: {
            "authority_ready": ready,
            "state": {
                "expires_at": 9999999999,
                "frames": {"frame": {"active": record, "candidate": None, "retired": []}},
            },
        },
    )
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda *a: plane)
    monkeypatch.setattr(policy, "load_facts", lambda *a, **kw: facts)
    assert policy.evictable("host", hq_dir=tmp_path) is expected


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), True, 0, -1])
def test_invalid_eviction_threshold_denies(tmp_path, threshold):
    assert not policy.evictable("host", hq_dir=tmp_path, evict_after_s=threshold)


def test_cli_uses_same_explanations(candidate, tmp_path, monkeypatch):
    from beadhive.cli import app

    frame, facts = candidate
    hosts.save(tmp_path, frame)
    monkeypatch.setattr(config, "load", lambda: {})
    monkeypatch.setattr(config, "load_host", lambda: {})
    monkeypatch.setattr(config, "hq_dir", lambda: tmp_path)
    monkeypatch.setattr(
        policy, "load_facts", lambda *a, **kw: replace(facts, dispatch_enabled=False)
    )
    for suffix in [[], ["--json"]]:
        result = CliRunner().invoke(app, ["host", "eligible", "host", *suffix])
        assert result.exit_code == 1 and "dispatch_enabled" in result.output


@pytest.mark.parametrize(
    "required,allowed",
    [(1, True), (2, True), (3, False), (True, False), (0, False), (-1, False), (1.0, False)],
)
def test_capacity_requirements_are_positive_integers(candidate, required, allowed):
    frame, facts = candidate
    assert (
        policy.eligible(frame, {"requires": {"max_sessions": required}}, facts).allowed is allowed
    )


def test_zero_capacity_cannot_dispatch(candidate):
    frame, facts = candidate
    caps = {**facts.desired["caps"], "max_sessions": 0}
    frame = frame.model_copy(update={"capabilities": hosts.FrameCapabilities(**caps)})
    facts = replace(facts, desired={**facts.desired, "caps": caps})
    assert "available_capacity" in policy.eligible(frame, {}, facts).reason


@pytest.mark.parametrize("operation", ["two_phase", "adopt", "renew"])
def test_revocation_during_remote_reads_refuses_write(operation, monkeypatch, tmp_path):
    from beadhive import gitref, host_fence

    (tmp_path / ".git").mkdir()
    revoked = False
    writes = []

    def check(*a, **kw):
        if revoked:
            raise policy.EligibilityError("revoked")
        return policy.EligibilityDecision((("active", True),))

    def read(*a, **kw):
        nonlocal revoked
        revoked = True
        lease = host_lease.HostLease(
            "host", "host", 4, host_lease.now_stamp(), host_lease.now_stamp(9999999999)
        )
        return ("old", lease) if operation != "two_phase" else lease

    monkeypatch.setattr(policy, "require_eligible", check)
    monkeypatch.setattr(gitref, "cas", lambda *a, **kw: writes.append("lease"))
    monkeypatch.setattr(host_fence, "install_fence", lambda *a, **kw: writes.append("fence"))
    if operation == "two_phase":
        monkeypatch.setattr(host_fence, "read_fence", lambda *a, **kw: ("", None))
        monkeypatch.setattr(host_lease, "read", read)
    else:
        monkeypatch.setattr(host_lease, "_read", read)
    with pytest.raises(policy.EligibilityError, match="revoked"):
        if operation == "two_phase":
            host_adopt.adopt(
                prefix="bh",
                hive_remote="remote",
                hq_remote="remote",
                hive_cwd=tmp_path,
                hq_cwd=tmp_path,
                host_id="host",
                label="host",
            )
        elif operation == "adopt":
            host_lease.adopt("remote", "bh", host_id="host", label="host", cwd=tmp_path)
        else:
            host_lease.renew("remote", "bh", host_id="host", cwd=tmp_path)
    assert writes == []


def test_claim_session_checks_after_candidate_read(monkeypatch, tmp_path):
    writes = []
    session = SimpleNamespace(claim_issue=lambda *a, **kw: writes.append("claim"))
    guarded = policy.GuardedClaimSession(session, tmp_path)
    monkeypatch.setattr(
        policy,
        "require_intake",
        lambda **kw: (_ for _ in ()).throw(policy.EligibilityError("revoked")),
    )
    with pytest.raises(policy.EligibilityError, match="revoked"):
        guarded.claim_issue("candidate")
    assert writes == []


def test_unverified_operator_anchor_cannot_select_legacy_fallback(tmp_path, monkeypatch):
    import json

    anchor = tmp_path / "operator.json"
    anchor.write_text(json.dumps({"role": "operator"}))
    monkeypatch.setattr(
        config,
        "load_host",
        lambda: {"hq": {"mode": "dolt-server", "authority_anchor": str(anchor)}},
    )
    assert not policy.decision_for("legacy", hq_dir=tmp_path).allowed
    anchor.write_text(json.dumps({"role": "frame"}))
    assert not policy.decision_for("enrolled", hq_dir=tmp_path).allowed


def test_old_local_incarnation_cannot_accept_new_incarnation_heartbeat(candidate):
    frame, facts = candidate
    frame = frame.model_copy(update={"instance_ref": "previous-instance"})
    assert "current_frame_incarnation" in policy.eligible(frame, {}, facts).reason


@pytest.mark.parametrize("phase", ["two_phase", "cas"])
def test_incumbent_recovery_rechecked_before_automatic_takeover(phase, tmp_path, monkeypatch):
    from beadhive import gitref, host_fence

    (tmp_path / ".git").mkdir()
    live = host_lease.HostLease(
        "incumbent", "incumbent", 3, host_lease.now_stamp(), host_lease.now_stamp(9999999999)
    )
    checks = iter([True, False])
    writes = []
    monkeypatch.setattr(policy, "require_eligible", lambda *a, **kw: None)
    monkeypatch.setattr(policy, "evictable", lambda *a, **kw: next(checks))
    monkeypatch.setattr(gitref, "cas", lambda *a, **kw: writes.append("lease"))
    monkeypatch.setattr(host_fence, "install_fence", lambda *a, **kw: writes.append("fence"))
    if phase == "two_phase":
        monkeypatch.setattr(host_fence, "read_fence", lambda *a, **kw: ("", None))
        monkeypatch.setattr(host_lease, "read", lambda *a, **kw: live)
        with pytest.raises(host_lease.HostLeaseRejected, match="authority changed"):
            host_adopt.adopt(
                prefix="bh",
                hive_remote="r",
                hq_remote="r",
                hive_cwd=tmp_path,
                hq_cwd=tmp_path,
                host_id="new",
                label="new",
            )
    else:
        monkeypatch.setattr(host_lease, "_read", lambda *a, **kw: ("old", live))
        with pytest.raises(host_lease.HostLeaseRejected, match="no longer evictable"):
            host_lease.adopt("r", "bh", host_id="new", label="new", cwd=tmp_path)
    assert writes == []


@pytest.mark.parametrize("held", [True, False])
def test_dispatch_adapter_uses_explicit_legacy_primary_reader(held, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "require_local", lambda *a, **kw: None)
    lease = SimpleNamespace(held_by=lambda identity: held and identity == "legacy")
    calls = []

    def reader(hive, **kwargs):
        calls.append((hive, kwargs))
        return "bh", "legacy", lease

    decision = policy.local_intake_decision(
        "beadhive", cfg={}, hive_dir=tmp_path, legacy_primary=reader
    )
    assert decision.allowed is held
    assert calls == [("beadhive", {"cfg": {}, "hive_dir": tmp_path})]


def test_dispatch_adapter_refuses_missing_legacy_primary_reader(monkeypatch):
    monkeypatch.setattr(policy, "require_local", lambda *a, **kw: None)
    decision = policy.local_intake_decision("legacy", cfg={})
    assert not decision.allowed
    assert decision.reason == "legacy_primary_reader_available"


@pytest.mark.parametrize("reason", ["quarantined", "authority_unavailable", "revoked"])
def test_bd_passthrough_refuses_ineligible_frame_even_without_cached_lease(
    reason, tmp_path, monkeypatch
):
    monkeypatch.setattr(guard, "primary_state", lambda **kw: None)
    calls = []

    def refuse(**kwargs):
        calls.append(kwargs)
        raise policy.EligibilityError(f"frame ineligible: {reason}")

    monkeypatch.setattr(policy, "require_intake", refuse)
    assert reason in guard.bd_write_refusal(
        ["update", "bh-test", "--claim"], tmp_path, cfg={"hq": {}}
    )
    assert calls == [{"cfg": {"hq": {}}, "hive_dir": tmp_path}]
    calls.clear()
    assert guard.bd_write_refusal(["show", "bh-test"], tmp_path, cfg={"hq": {}}) == ""
    assert calls == []


def test_bd_passthrough_uses_authenticated_signed_holder_without_cached_lease(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(guard, "primary_state", lambda **kw: None)
    monkeypatch.setattr(policy, "require_intake", lambda **kw: policy.EligibilityDecision(()))
    lease = SimpleNamespace(held_by=lambda identity: identity == "frame-holder")
    monkeypatch.setattr(policy, "authoritative_primary", lambda **kw: ("bh", "frame-holder", lease))
    assert guard.bd_write_refusal(["update", "bh-test", "--claim"], tmp_path, cfg={"hq": {}}) == ""
    monkeypatch.setattr(policy, "authoritative_primary", lambda **kw: None)
    assert "current_hive_lease_holder" in guard.bd_write_refusal(
        ["update", "bh-test", "--claim"], tmp_path, cfg={"hq": {}}
    )
