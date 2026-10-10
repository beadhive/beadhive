"""Composable host roles: host.roles as a set of capability bundles (bh-t1xwp).

Trust-profiles ADR section 1 (docs/design/hq-trust-profiles-and-capabilities-adr.md): the
effective capability set is the union of the role bundles, then the trust profile's ceiling;
tenure follows the highest-tenure role; the admission grant's capability list is projected from
the role set and ``capabilities_match_admission`` compares it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from beadhive import capabilities, failover_policy, host_lease, host_roles, hosts
from beadhive import frame_eligibility as policy
from beadhive.host_heartbeat_core import HeartbeatLease, VerifiedObservation
from beadhive.hq_manifest_guard import validate as guard_validate

CAPS = dict(
    isolation="workstation",
    trust_zone="self-hosted",
    arch="x86_64",
    harnesses=["claude"],
    max_sessions=2,
)
RELEASE = {"id": "release", "digest": "sha256:" + "1" * 64}


def _manifest(**overrides) -> hosts.HostManifest:
    fields = {
        "host_id": "host",
        "label": "workstation",
        "os": "linux",
        "arch": "x86_64",
        "identity": {"kind": "none"},
    }
    fields.update(overrides)
    return hosts.HostManifest(**fields)


# ---- the bundles are the ADR table ------------------------------------------------------------


def test_the_role_bundles_are_the_adr_section_1_table():
    assert host_roles.ROLE_BUNDLES == {
        "viewer": frozenset(),
        "planner": frozenset({"author"}),
        "operator": frozenset({"author", "admin"}),
        "transient": frozenset({"author", "claim", "execute", "publish"}),
        "executor": frozenset({"author", "claim", "execute", "publish", "place"}),
        "director": frozenset({"place", "admin"}),
    }
    assert set(hosts.ROLE_NAMES) == set(host_roles.ROLES)
    for bundle in host_roles.ROLE_BUNDLES.values():
        assert bundle <= set(capabilities.REGISTRY)


@pytest.mark.parametrize(
    "roles,expected",
    [
        (["planner"], {"author"}),
        (["operator", "executor"], {"author", "admin", "claim", "execute", "publish", "place"}),
        (["viewer", "executor"], set(host_roles.ROLE_BUNDLES["executor"])),
        (["planner", "executor"], set(host_roles.ROLE_BUNDLES["executor"])),
        (["viewer"], set()),
    ],
)
def test_the_effective_set_is_the_union_of_the_bundles(roles, expected):
    assert host_roles.effective_capabilities(roles) == expected
    assert _manifest(role=host_roles.tenure_role(roles), roles=roles).capability_set() == expected


def test_viewer_adds_nothing_to_a_set():
    assert host_roles.effective_capabilities(
        ["viewer", "executor"]
    ) == host_roles.effective_capabilities("executor")


def test_the_ceiling_is_applied_after_the_union_and_defaults_to_nothing():
    roles = ["operator", "executor"]
    assert host_roles.effective_capabilities(roles) == host_roles.declared_capabilities(roles)

    def no_admin_no_self_place(_union):
        return {"admin", "place"}

    capped = host_roles.effective_capabilities(roles, no_admin_no_self_place)
    assert capped == {"author", "claim", "execute", "publish"}
    with pytest.raises(ValueError, match="unknown capabilities"):
        host_roles.effective_capabilities(roles, lambda _union: {"root"})


def test_an_unknown_or_empty_role_set_is_refused():
    with pytest.raises(host_roles.UnknownRole):
        host_roles.role_set(["planner", "super-admin"])
    with pytest.raises(host_roles.UnknownRole):
        host_roles.role_set([])


# ---- host.roles and host.role in the manifest -------------------------------------------------


def test_host_roles_validates_as_a_list_over_the_closed_role_set():
    with pytest.raises(ValidationError):
        _manifest(role="executor", roles=["executor", "super-admin"])
    with pytest.raises(ValidationError):
        _manifest(role="executor", roles="executor")
    with pytest.raises(ValidationError, match="must be a member of roles"):
        _manifest(role="viewer", roles=["operator", "executor"])


def test_host_role_alone_reads_as_a_one_element_set():
    manifest = _manifest(role="transient")
    assert manifest.roles is None
    assert manifest.role_set == ("transient",)
    assert manifest.tenure_role == "transient"


def test_host_roles_alone_takes_the_highest_tenure_role_as_role():
    manifest = _manifest(roles=["operator", "executor", "operator"])
    assert manifest.role == "executor"
    assert manifest.roles == ["operator", "executor"]  # de-duplicated, ADR table order


def test_deprecated_spellings_resolve_inside_a_set():
    manifest = _manifest(role="executor", roles=["worker", "primary-default"])
    assert manifest.roles == ["viewer", "executor"]


@pytest.mark.parametrize(
    "fields",
    [
        {"role": "executor"},
        {"role": "planner"},
        {"role": "executor", "roles": ["operator", "executor"]},
        {"role": "planner", "roles": ["planner"]},
    ],
)
def test_both_forms_round_trip_through_the_manifest_file_and_schema(tmp_path, fields):
    manifest = _manifest(**fields, capabilities=CAPS)
    hosts.save(tmp_path, manifest)
    assert hosts.load(tmp_path, "host") == manifest
    raw = manifest.model_dump(mode="json")
    guard_validate(raw, hosts.HostManifest.model_json_schema())
    assert hosts.HostManifest.model_validate(raw) == manifest


def test_a_legacy_single_role_manifest_serialises_exactly_as_before():
    """An older reader forbids unknown keys: a single-role manifest must not grow any."""
    raw = _manifest(role="executor", capabilities=CAPS).model_dump(mode="json")
    assert "roles" not in raw
    assert raw["capabilities"] == CAPS


# ---- admission is projected from the set ------------------------------------------------------


def test_the_grant_list_is_projected_from_the_role_set():
    assert host_roles.admission_grants("executor") is None
    assert host_roles.admission_grants(["viewer"]) is None
    assert host_roles.admission_grants(["planner"]) == ["author"]
    workstation = _manifest(roles=["operator", "executor"], capabilities=CAPS)
    assert workstation.capabilities.grants == [
        "admin",
        "author",
        "claim",
        "execute",
        "place",
        "publish",
    ]
    assert workstation.capabilities.model_dump()["grants"] == workstation.capabilities.grants


def test_a_hand_declared_grant_list_must_match_the_projection():
    with pytest.raises(ValidationError, match="projected from the role set"):
        _manifest(role="executor", capabilities={**CAPS, "grants": ["admin"]})
    with pytest.raises(ValidationError, match="projected from the role set"):
        _manifest(roles=["planner"], capabilities={**CAPS, "grants": ["author", "admin"]})
    same = _manifest(roles=["planner"], capabilities={**CAPS, "grants": ["author"]})
    assert same.capabilities.grants == ["author"]


def _facts(frame, granted_caps):
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
        caps=granted_caps,
        profile="profile",
        authority={"holder_identity": "host", "instance_ref": "vm"},
    )
    return policy.EligibilityFacts(VerifiedObservation("fresh", True, True, 1.0, lease), desired)


def _frame(**role_fields):
    return _manifest(
        frame_id="frame",
        state="active",
        release=RELEASE,
        capabilities=CAPS,
        instance_ref="vm",
        **role_fields,
    )


def test_admission_granted_for_the_set_matches():
    frame = _frame(roles=["operator", "executor"])
    granted = frame.capabilities.model_dump()  # what admission records as desired.caps
    decision = policy.eligible(frame, {}, _facts(frame, granted))
    assert decision.allowed, decision.reason
    for capability in ("author", "claim", "execute", "publish"):
        assert decision.for_capability(capability).allowed


def test_a_frame_wider_than_its_grant_fails_closed():
    """Admitted as a legacy executor, then re-declared [operator, executor]: the grant does not
    carry admin, so the frame is not the frame that was admitted."""
    legacy_grant = _frame(role="executor").capabilities.model_dump()
    frame = _frame(roles=["operator", "executor"])
    decision = policy.eligible(frame, {}, _facts(frame, legacy_grant))
    assert not decision.allowed
    assert decision.reason == "capabilities_match_admission"
    assert not decision.for_capability("execute").allowed
    narrower = _frame(roles=["planner", "executor"])  # projects to the executor bundle
    assert not policy.eligible(narrower, {}, _facts(narrower, legacy_grant)).allowed


def test_a_legacy_grant_keeps_matching_a_legacy_frame():
    frame = _frame(role="executor")
    assert policy.eligible(frame, {}, _facts(frame, dict(CAPS))).allowed


@pytest.mark.parametrize(
    "roles,allowed",
    [
        (["planner"], {"author"}),
        (["operator"], {"author"}),
        (["viewer"], set()),
        (["transient"], {"author", "claim", "execute", "publish"}),
        (["operator", "executor"], {"author", "claim", "execute", "publish"}),
    ],
)
def test_the_role_predicate_is_membership_of_the_asked_capability(roles, allowed):
    frame = _frame(roles=roles)
    decision = policy.eligible(frame, {}, _facts(frame, frame.capabilities.model_dump()))
    for capability in capabilities.WRITE_CAPABILITIES:
        narrowed = decision.for_capability(capability)
        assert dict(narrowed.predicates)["executor_role"] is (capability in allowed), capability


def test_the_ceiling_input_reaches_the_role_predicate():
    frame = _frame(roles=["operator", "executor"])
    facts = _facts(frame, frame.capabilities.model_dump())
    facts = policy.EligibilityFacts(
        facts.observation, facts.desired, ceiling=lambda _union: {"execute", "publish"}
    )
    decision = policy.eligible(frame, {}, facts)
    assert decision.granted == {"author", "admin", "claim", "place"}
    assert decision.for_capability("claim").allowed
    assert decision.for_capability("execute").reason == "executor_role"


# ---- tenure follows the highest-tenure role ---------------------------------------------------


@pytest.mark.parametrize(
    "roles,role,after",
    [
        (["operator", "executor"], "executor", 3600.0),
        (["planner", "transient"], "transient", 1800.0),
        (["transient", "executor"], "executor", 3600.0),
        (["planner"], "planner", None),
        (["operator", "director"], "operator", None),
        (["viewer"], "viewer", None),
    ],
)
def test_failover_after_resolves_from_the_highest_tenure_role(roles, role, after):
    assert host_roles.tenure_role(roles) == role
    assert failover_policy.failover_after_for(roles) == after


def test_policy_rows_keep_their_executor_transient_scope():
    bounds = failover_policy.Bounds(session_ttl_s=300.0)
    policy_rows = failover_policy.FailoverPolicy(
        bounds, hives={"bh": {"executor": 5400.0, "transient": 2400.0}}
    )
    assert policy_rows.failover_after("bh", ["operator", "executor"]) == 5400.0
    assert policy_rows.failover_after("bh", ["planner", "transient"]) == 2400.0
    assert policy_rows.failover_after("bh", ["operator"]) is None
    assert policy_rows.failover_after("bh", "executor") == 5400.0
    assert failover_policy.ROLES == ("executor", "transient")


def test_lease_ttl_follows_the_set_and_refuses_a_never_placed_set():
    assert host_lease.ttl_for_role(["operator", "executor"], 100.0) == host_lease.ttl_for_role(
        "executor", 100.0
    )
    for never in (["planner"], ["operator"], ["director"], ["operator", "director"]):
        with pytest.raises(host_lease.HostLeaseRejected, match="no placement tenure"):
            host_lease.ttl_for_role(never)
    with pytest.raises(host_lease.HostLeaseRejected, match="viewer"):
        host_lease.ttl_for_role(["viewer"])
