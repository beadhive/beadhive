"""The capability model: one capability per write verb, predicates per capability (bh-b3ebg).

Trust-profiles ADR section 1 (docs/design/hq-trust-profiles-and-capabilities-adr.md). Pins the
registry to the ADR's table, then runs every write capability against a fixture fleet of frames
that each fail one class of predicate: a frame that fails only execution predicates (heartbeat,
release, conformance, capacity, dispatch) still authors and claims, while execute and publish
refuse; a frame that fails identity, admission, cordon or role refuses everything.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from typer import Exit
from typer.testing import CliRunner

from beadhive import capabilities, config, guard, host, hosts, hq_authority_expiry, registry
from beadhive import frame_eligibility as policy
from beadhive.host_heartbeat_core import HeartbeatLease, VerifiedObservation

# ---- the registry is the ADR table ----------------------------------------------------------


def test_the_registry_is_the_adr_section_1_table():
    table = {name: (cap.verbs, cap.predicates) for name, cap in capabilities.REGISTRY.items()}
    identity = ("current_frame_incarnation", "beadyard_binding")
    author = (*identity, "admitted_active", "not_cordoned")
    claim = (*author, "writer_reachable")
    execute = (
        *claim,
        "authenticated_fresh_heartbeat",
        "release_matches",
        "conformance_pass",
        "capabilities_match_admission",
        "hive_requirements",
        "available_capacity",
        "dispatch_enabled",
    )
    assert table == {
        "author": (
            (
                "bd create",
                "bd update",
                "bd comment",
                "bd dep",
                "bd label",
                "bd close",
                "plan file",
                "plan approve",
            ),
            author,
        ),
        "claim": (("work claim", "work resume", "work abandon", "work assign"), claim),
        "execute": (("work check", "dispatch", "validation runs", "work next"), execute),
        "publish": (
            ("work submit", "work merge", "work finish", "push_state"),
            (*execute, "bh_writer"),
        ),
        "place": (
            ("host lease adopt", "host lease release", "failover"),
            ("admitted_active", "placement_rights"),
        ),
        "admin": (
            ("hq authority", "fleet-config publish", "admission of other frames"),
            ("hq_write_access",),
        ),
    }


def test_every_predicate_is_a_frame_predicate_or_decided_by_a_named_layer():
    known = set(capabilities.FRAME_PREDICATES) | set(capabilities.LAYER_PREDICATES)
    for cap in capabilities.REGISTRY.values():
        assert set(cap.predicates) <= known, cap.name
    assert set(capabilities.IMPLIED) <= set(capabilities.FRAME_PREDICATES)


def test_author_consults_identity_admission_and_cordon_only():
    assert capabilities.frame_predicates("author") == (
        "authority_available",
        "admitted_active",
        "not_cordoned",
        "reviewed_admission_or_emergency",
        "current_frame_incarnation",
        "beadyard_binding",
        "executor_role",
    )
    assert capabilities.frame_predicates("claim") == capabilities.frame_predicates("author")
    assert capabilities.layer_predicates("claim") == ("writer_reachable",)


@pytest.mark.parametrize("name", ["execute", "publish"])
def test_execute_and_publish_consult_every_frame_predicate(name):
    assert capabilities.frame_predicates(name) == capabilities.FRAME_PREDICATES


def test_publish_names_the_in_data_writer_fence():
    assert capabilities.layer_predicates("publish") == ("writer_reachable", "bh_writer")


def test_an_unknown_capability_is_refused_by_name():
    with pytest.raises(capabilities.UnknownCapability, match="known: author"):
        capabilities.frame_predicates("write")


# ---- a fixture fleet ------------------------------------------------------------------------

RELEASE = {"id": "release", "digest": "sha256:" + "1" * 64}
CAPS = dict(
    isolation="kvm", trust_zone="self-hosted", arch="x86_64", harnesses=["codex"], max_sessions=2
)


def _healthy():
    frame = hosts.HostManifest(
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
    desired = dict(
        declared=True,
        state="active",
        cordoned=False,
        release=RELEASE,
        caps=CAPS,
        profile="profile",
        authority={"holder_identity": "host", "instance_ref": "vm"},
    )
    beat = VerifiedObservation("fresh", True, True, 1.0, lease)
    return frame, policy.EligibilityFacts(beat, desired)


def _desired(facts, **changes):
    return replace(facts, desired={**facts.desired, **changes})


def _stale(frame, facts):
    return frame, replace(facts, observation=replace(facts.observation, verified=False))


def _release_drift(frame, facts):
    return frame, _desired(facts, release={"id": "other", "digest": "sha256:" + "3" * 64})


def _nonconformant(frame, facts):
    return frame, _desired(facts, profile="other-profile")


def _no_capacity(frame, facts):
    caps = {**CAPS, "max_sessions": 0}
    frame = frame.model_copy(update={"capabilities": hosts.FrameCapabilities(**caps)})
    return frame, _desired(facts, caps=caps)


def _dispatch_disabled(frame, facts):
    return frame, replace(facts, dispatch_enabled=False)


def _execution_down(frame, facts):
    for fail in (_stale, _release_drift, _nonconformant, _no_capacity, _dispatch_disabled):
        frame, facts = fail(frame, facts)
    return frame, facts


def _parked(frame, facts):
    return frame, _desired(facts, state="parked")


def _cordoned(frame, facts):
    return frame, _desired(facts, cordoned=True)


def _foreign_incarnation(frame, facts):
    return frame, _desired(facts, authority={"holder_identity": "host", "instance_ref": "vm-2"})


def _viewer(frame, facts):
    return frame.model_copy(update={"role": "viewer"}), facts


#: member -> (the mutation of a healthy frame it applies, the capabilities it may exercise)
FLEET = {
    "healthy": (lambda f, x: (f, x), {"author", "claim", "execute", "publish"}),
    "stale-heartbeat": (_stale, {"author", "claim"}),
    "release-drift": (_release_drift, {"author", "claim"}),
    "nonconformant": (_nonconformant, {"author", "claim"}),
    "no-capacity": (_no_capacity, {"author", "claim"}),
    "dispatch-disabled": (_dispatch_disabled, {"author", "claim"}),
    "execution-down": (_execution_down, {"author", "claim"}),
    "parked": (_parked, set()),
    "cordoned": (_cordoned, set()),
    "foreign-incarnation": (_foreign_incarnation, set()),
    "viewer": (_viewer, set()),
}


def _member(name):
    mutate, allowed = FLEET[name]
    frame, facts = mutate(*_healthy())
    return frame, facts, allowed


@pytest.mark.parametrize("member", sorted(FLEET))
@pytest.mark.parametrize("capability", capabilities.WRITE_CAPABILITIES)
def test_fleet_decision_per_capability(member, capability):
    frame, facts, allowed = _member(member)
    decision = policy.eligible(frame, {}, facts).for_capability(capability)
    assert decision.allowed is (capability in allowed), decision.reason
    consulted = {name for name, _ in decision.predicates}
    assert consulted == set(capabilities.frame_predicates(capability))


def test_execution_down_frame_names_each_failed_execution_predicate():
    frame, facts, _allowed = _member("execution-down")
    reason = policy.eligible(frame, {}, facts).for_capability("execute").reason
    for name in (
        "authenticated_fresh_heartbeat",
        "release_matches",
        "conformance_pass",
        "available_capacity",
        "dispatch_enabled",
    ):
        assert name in reason


def test_full_intake_is_unchanged_without_a_capability():
    frame, facts, _allowed = _member("stale-heartbeat")
    decision = policy.eligible(frame, {}, facts)
    assert decision.for_capability(None) is decision
    assert not decision.allowed


@pytest.mark.parametrize(
    "predicates",
    [
        (("authority_available", False),),  # the read itself failed: nothing evaluated
        (("beadyard_binding", False),),  # a foreign HQ pin short-circuits
    ],
)
@pytest.mark.parametrize("capability", sorted(capabilities.REGISTRY))
def test_a_short_circuit_refusal_is_never_narrowed_into_an_allow(predicates, capability):
    decision = policy.EligibilityDecision(predicates)
    assert not decision.for_capability(capability).allowed


def test_narrowing_keeps_predicates_it_does_not_know():
    frame, facts, _allowed = _member("healthy")
    full = policy.eligible(frame, {}, facts)
    decision = policy.EligibilityDecision((*full.predicates, ("some_adapter_check", False)))
    assert decision.for_capability("author").reason == "some_adapter_check"


# ---- the guard: one capability per verb, through every layer --------------------------------


@pytest.fixture
def fleet_host(monkeypatch, tmp_path):
    """Route the real guard layers to one fleet member holding the hive lease."""
    state = SimpleNamespace(held=True)

    def use(name, *, held=True):
        frame, facts, _allowed = _member(name)
        state.held = held
        monkeypatch.setattr(
            policy, "decision_for", lambda _id, hive=None, **_kw: policy.eligible(frame, {}, facts)
        )

    lease = SimpleNamespace(held_by=lambda identity: state.held and identity == "host", epoch=3)
    monkeypatch.setattr(guard, "forward_marker", lambda *a, **kw: None)
    monkeypatch.setattr(guard, "writer_state", lambda *a, **kw: None)
    monkeypatch.setattr(guard, "_renew_liveness_hint", lambda *a, **kw: None)
    monkeypatch.setattr(hq_authority_expiry, "warn_if_expiring", lambda *a, **kw: None)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    monkeypatch.setattr(registry, "hive_dir_for", lambda *_a, **_kw: tmp_path)
    entry = {"prefix": "bh", "path": str(tmp_path)}
    monkeypatch.setattr(registry, "entry_for_dir", lambda *_a, **_kw: entry)
    monkeypatch.setattr(policy, "authoritative_primary", lambda *a, **kw: ("bh", "host", lease))
    return use


@pytest.mark.parametrize("member", sorted(FLEET))
@pytest.mark.parametrize("capability", capabilities.WRITE_CAPABILITIES)
def test_guard_capability_on_the_fixture_fleet(fleet_host, member, capability, capsys):
    fleet_host(member)
    _mutate, allowed = FLEET[member]
    if capability in allowed:
        guard.guard_capability(capability, "bh", cfg={}, verb="fixture")
        assert guard.require_capability(capability, "bh", cfg={}) is not None
        return
    with pytest.raises(Exit):
        guard.guard_capability(capability, "bh", cfg={}, verb="fixture")
    assert "ineligible" in capsys.readouterr().err
    with pytest.raises(policy.EligibilityError):
        guard.require_capability(capability, "bh", cfg={})


def test_guard_primary_is_the_publish_check(fleet_host):
    fleet_host("stale-heartbeat")
    guard.guard_capability("author", "bh", cfg={})
    with pytest.raises(Exit):
        guard.guard_primary("bh", cfg={})


@pytest.mark.parametrize("capability", capabilities.WRITE_CAPABILITIES)
def test_the_fenced_write_policy_still_needs_the_lease_for_every_capability(
    fleet_host, capability, capsys
):
    fleet_host("healthy", held=False)
    with pytest.raises(Exit):
        guard.guard_capability(capability, "bh", cfg={})
    assert "current_hive_lease_holder" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["admin", "write", ""])
def test_guard_capability_refuses_a_non_write_capability(name):
    with pytest.raises(capabilities.UnknownCapability):
        guard.guard_capability(name, "bh", cfg={})
    with pytest.raises(capabilities.UnknownCapability):
        guard.require_capability(name, "bh", cfg={})


def test_place_is_the_self_placement_check_never_a_bead_write(monkeypatch):
    """``place`` (bh-vl0r3): guard_capability delegates it to the registered self-placement
    check; require_capability (the in-process bead-write recheck) still refuses it."""
    seen = []
    monkeypatch.setattr(
        guard, "_PLACE_GUARD", lambda hive, **kw: seen.append((hive, kw["verb"])) or "decision"
    )
    assert guard.guard_capability("place", "bh", cfg={}, verb="host lease adopt") == "decision"
    assert seen == [("bh", "host lease adopt")]
    with pytest.raises(capabilities.UnknownCapability):
        guard.require_capability("place", "bh", cfg={})


@pytest.mark.parametrize(
    ("args", "capability"),
    [
        (["create", "--title", "x"], "author"),
        (["update", "bh-1", "--status", "blocked"], "author"),
        (["comments", "add", "bh-1", "note"], "author"),
        (["dep", "add", "bh-1", "bh-2"], "author"),
        (["label", "add", "bh-1", "x"], "author"),
        (["close", "bh-1"], "author"),
        (["update", "bh-1", "--claim"], "claim"),
        (["claim", "bh-1"], "claim"),
        (["dolt", "push"], "publish"),
        (["dolt", "sync"], "publish"),
    ],
)
def test_each_passthrough_write_names_one_capability(args, capability):
    assert guard.bd_capability(args) == capability


def test_a_passthrough_author_write_passes_on_a_stale_frame(fleet_host, tmp_path):
    fleet_host("execution-down")
    cfg = {"hq": {}}
    assert guard.bd_write_refusal(["update", "bh-1", "--status", "x"], tmp_path, cfg=cfg) == ""
    refusal = guard.bd_write_refusal(["dolt", "push"], tmp_path, cfg=cfg)
    assert "authenticated_fresh_heartbeat" in refusal


# ---- bh host eligible reports the decision per capability -----------------------------------


def test_host_eligible_reports_each_capability(tmp_path, monkeypatch):
    from beadhive.cli import app

    frame, facts, _allowed = _member("execution-down")
    hosts.save(tmp_path, frame)
    monkeypatch.setattr(config, "load", lambda: {})
    monkeypatch.setattr(config, "load_host", lambda: {})
    monkeypatch.setattr(config, "hq_dir", lambda: tmp_path)
    monkeypatch.setattr(policy, "load_facts", lambda *a, **kw: facts)
    result = CliRunner().invoke(app, ["host", "eligible", "host", "--json"])
    assert result.exit_code == 1  # the all-up bit still evaluates every predicate
    payload = json.loads(result.output)
    report = payload["capabilities"]
    assert list(report) == list(capabilities.REGISTRY)
    assert report["author"]["allowed"] and report["claim"]["allowed"]
    assert not report["execute"]["allowed"] and not report["publish"]["allowed"]
    assert set(report["author"]["predicates"]) == set(capabilities.frame_predicates("author"))
    assert "dispatch_enabled" in report["publish"]["reason"]
    assert set(report["publish"]["deferred"]) == {"writer_reachable", "bh_writer"}
    assert "deferred" not in report["author"]

    table = CliRunner().invoke(app, ["host", "eligible", "host"])
    assert "CAPABILITY" in table.output and "author" in table.output


def test_host_eligible_hive_lease_applies_to_every_write_capability(tmp_path, monkeypatch):
    from beadhive import host_cli

    frame, facts, _allowed = _member("healthy")
    decision = policy.eligible(frame, {}, facts)
    report = host_cli.capability_decisions(decision, held=False)
    for name in capabilities.WRITE_CAPABILITIES:
        assert not report[name]["allowed"]
        assert report[name]["reason"] == "current_hive_lease_holder"
    assert report["place"]["allowed"]
    assert "current_hive_lease_holder" not in report["place"]["predicates"]
    legacy = host_cli.capability_decisions(None, held=True)
    assert all(row["allowed"] for row in legacy.values())
