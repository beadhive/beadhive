"""One canonical committed catalog drives the SQL hive policy projection."""

from dataclasses import replace

import pytest

from beadhive import hq_authority_guard as guard
from beadhive.frame_eligibility import EligibilityFacts, eligible
from beadhive.host_heartbeat_core import HeartbeatLease, VerifiedObservation
from beadhive.hosts import HostManifest
from beadhive.hq_hive_policy import HivePolicyError, project_hive_policies
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot


def _snapshot(fleet):
    return FleetConfigSnapshot(
        backend_identity="sql:config-backend",
        commit_revision="a" * 32,
        generation="config-generation",
        fetched_at=100,
        valid_until=130,
        documents=(FleetConfigDocument("fleet.yaml", fleet),),
    )


def test_projection_is_canonical_explicit_and_bound_to_exact_head():
    snapshot = _snapshot(
        "managed_repos:\n"
        "- provider: github\n  org: bee\n  repo: hive\n  prefix: bh\n"
        "  frame_policy:\n    config_revision: desired-7\n"
        "    requires: {max_sessions: 2, isolation: sandbox}\n"
        "    evict_after_s: 900\n"
        "- provider: github\n  org: bee\n  repo: legacy\n  prefix: lg\n"
    )
    assert project_hive_policies(snapshot, valid_until=500, now=101) == {
        "bh": {
            "config_revision": "desired-7", "config_head": "a" * 32,
            "valid_until": 500, "requires": {"max_sessions": 2, "isolation": "sandbox"},
            "evict_after_s": 900,
        }
    }
    with pytest.raises(HivePolicyError, match="expired"):
        project_hive_policies(snapshot, valid_until=500, now=130)


@pytest.mark.parametrize(
    "catalog",
    [
        "managed_repos:\n- prefix: bh\n- prefix: bh\n",
        "managed_repos:\n- prefix: bad/name\n",
        "managed_repos:\n- prefix: hq\n  kind: hq\n"
        "  frame_policy: {config_revision: x, requires: {}, evict_after_s: 1}\n",
        "managed_repos:\n- prefix: bh\n"
        "  frame_policy: {config_revision: x, requires: {unknown: x}, evict_after_s: 1}\n",
        "managed_repos:\n- prefix: bh\n"
        "  frame_policy: {config_revision: ' x ', requires: {}, evict_after_s: 1}\n",
        "managed_repos:\n- prefix: bh\n"
        "  frame_policy: {config_revision: x, requires: {max_sessions: true}, evict_after_s: 1}\n",
        "managed_repos:\n- prefix: bh\n"
        "  frame_policy: {config_revision: x, requires: {max_sessions: '2'}, evict_after_s: 1}\n",
        "managed_repos:\n- prefix: bh\n"
        "  frame_policy: {config_revision: x, requires: {}, evict_after_s: true}\n",
        "managed_repos:\n- prefix: bh\n"
        "  frame_policy: {config_revision: x, requires: {}, evict_after_s: '5'}\n",
        "managed_repos:\n- prefix: bh\n"
        "  frame_policy: {config_revision: true, requires: {}, evict_after_s: 5}\n",
    ],
)
def test_invalid_catalog_denies_projection(catalog):
    with pytest.raises(HivePolicyError):
        project_hive_policies(_snapshot(catalog), valid_until=500, now=101)


@pytest.mark.parametrize(
    "case,allowed",
    [
        ("fresh", True),
        ("stale", False),
        ("unsigned", False),
        ("revoked", False),
        ("pending", False),
        ("draining", False),
        ("wrong_incarnation", False),
        ("release_mismatch", False),
        ("capabilities_mismatch", False),
        ("config_unavailable", False),
    ],
)
def test_git_and_sql_share_full_backend_qualified_eligibility_matrix(case, allowed):
    snapshot = _snapshot(
        "managed_repos:\n"
        "- provider: github\n  org: bee\n  repo: hive\n  prefix: bh\n"
        "  frame_policy:\n    config_revision: desired-7\n"
        "    requires: {max_sessions: 2, harness: claude}\n"
        "    evict_after_s: 900\n"
    )
    sql_policy = project_hive_policies(snapshot, valid_until=500, now=101)["bh"]
    git_policy = {**sql_policy, "config_head": "f" * 40}
    guard.validate_hive_policies({"bh": git_policy})
    manifest = HostManifest.model_validate(
        {
            "frame_id": "frame-one", "host_id": "host-one", "instance_ref": "vm-one",
            "label": "fixture", "os": "linux", "arch": "x86_64", "role": "executor",
            "identity": {"kind": "none", "value": ""},
            "release": {"id": "fixture", "digest": "sha256:" + "1" * 64},
            "capabilities": {
                "isolation": "container", "trust_zone": "self-hosted", "arch": "x86_64",
                "harnesses": ["claude"], "max_sessions": 2,
            },
        }
    )
    lease = HeartbeatLease.model_validate(
        {
            "audience": "fixture-fleet", "frame_id": "frame-one",
            "holderIdentity": "host-one", "instance_ref": "vm-one",
            "key_id": "SHA256:fixture", "epoch": 1,
            "config_revision": "desired-7", "seq": 1,
            "renewTime": "2026-10-02T00:00:00+00:00", "state_seen": "active",
            "release": manifest.release.model_dump(),
            "report_digest": "sha256:" + "2" * 64,
            "conformance": {
                "profile": "fixture", "status": "conformant",
                "checks": [{"id": "required", "status": "pass"}],
            },
        }
    )
    desired = {
        "state": "active", "declared": True, "cordoned": False,
        "authority": {"holder_identity": "host-one", "instance_ref": "vm-one"},
        "release": manifest.release.model_dump(),
        "caps": manifest.capabilities.model_dump(), "profile": "fixture",
    }
    observation = VerifiedObservation(
        "verified", verified=True, fresh=True, age_seconds=1, lease=lease
    )
    available = True
    if case == "stale":
        observation = replace(observation, fresh=False, age_seconds=301)
    elif case == "unsigned":
        observation = replace(observation, status="unsigned", verified=False)
    elif case == "revoked":
        desired = {}
    elif case in {"pending", "draining"}:
        desired = {**desired, "state": case}
    elif case == "wrong_incarnation":
        observation = replace(
            observation,
            lease=HeartbeatLease.model_validate(
                {**lease.model_dump(), "holderIdentity": "different-host"}
            ),
        )
    elif case == "release_mismatch":
        desired = {
            **desired, "release": {"id": "other", "digest": "sha256:" + "3" * 64}
        }
    elif case == "capabilities_mismatch":
        desired = {**desired, "caps": {**desired["caps"], "max_sessions": 1}}
    elif case == "config_unavailable":
        available = False
    facts = EligibilityFacts(observation, desired, available=available)
    sql_decision = eligible(manifest, sql_policy, facts)
    git_decision = eligible(manifest, git_policy, facts)
    assert sql_decision.allowed is allowed
    assert sql_decision.predicates == git_decision.predicates
