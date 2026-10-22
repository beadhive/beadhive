"""Range-aware release eligibility (bh-vfrem.6, ADR section 4): the pure predicate, the
signed-beat carrier and the waived mode. The session carrier is covered in
``test_hq_sql_session``; every catalog row here is an in-memory fixture."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from beadhive import frame_eligibility as eligibility
from beadhive import hosts, release_measurement
from beadhive.host_heartbeat_core import HeartbeatLease, VerifiedObservation
from beadhive.release_policy import admits, catalog_row_for

V1 = release_measurement.ALGORITHM
REF = {"id": "0.28.2", "digest": "sha256:" + "a" * 64}
NEW = {"id": "0.28.3", "digest": "sha256:" + "b" * 64}
POLICY = {"format": 1, "spec": ">=0.28.2,==0.28.*", "catalog": "beadhive"}
RANGED = {"release": REF, "release_policy": POLICY}


def _rows(*releases, status="active", algorithm=V1, catalog="beadhive"):
    return [(catalog, r["id"], r["digest"], status, algorithm) for r in releases]


def _row(release=NEW, **kwargs):
    return catalog_row_for(_rows(release, **kwargs), "beadhive", release)


def test_exact_pin_ignores_the_catalog_and_compares_the_pair():
    exact = {"release": REF}
    assert admits(dict(REF), exact, None)
    assert not admits(dict(NEW), exact, _row())
    assert not admits({"id": "0.28.2", "digest": NEW["digest"]}, exact, None)


def test_in_range_release_in_the_catalog_is_admitted():
    assert admits(NEW, RANGED, _row())


@pytest.mark.parametrize(
    ("name", "measured", "row"),
    [
        ("unknown digest", NEW, None),
        (
            "out of range",
            {"id": "0.29.0", "digest": NEW["digest"]},
            _row({"id": "0.29.0", "digest": NEW["digest"]}),
        ),
        (
            "below the floor",
            {"id": "0.28.1", "digest": NEW["digest"]},
            _row({"id": "0.28.1", "digest": NEW["digest"]}),
        ),
        ("id disagrees with the row", {"id": "0.28.9", "digest": NEW["digest"]}, _row()),
        ("tampered digest", {"id": NEW["id"], "digest": "sha256:" + "c" * 64}, _row()),
        ("other algorithm", NEW, _row(algorithm="beadhive-tree-sha256-v2")),
        ("other catalog", NEW, {**_row(), "catalog": "other"}),
        ("not a row", NEW, "active"),
    ],
)
def test_range_refuses_anything_the_signed_binding_does_not_vouch_for(name, measured, row):
    assert not admits(measured, RANGED, row), name


def test_tombstone_and_second_active_digest_are_refused():
    other = {"id": NEW["id"], "digest": "sha256:" + "d" * 64}
    revoked = catalog_row_for(_rows(NEW) + _rows(NEW, status="revoked"), "beadhive", NEW)
    assert revoked["status"] == "revoked" and not admits(NEW, RANGED, revoked)
    two = catalog_row_for(_rows(NEW, other), "beadhive", NEW)
    assert two["active_digests"] == 2 and not admits(NEW, RANGED, two)
    # Revoking the other digest leaves exactly one effective digest, which is admitted again.
    healed = catalog_row_for(_rows(NEW, other) + _rows(other, status="revoked"), "beadhive", NEW)
    assert admits(NEW, RANGED, healed)


def test_a_digest_only_in_another_version_is_returned_so_the_id_mismatch_refuses():
    row = catalog_row_for(_rows(NEW), "beadhive", {"id": "0.28.9", "digest": NEW["digest"]})
    assert row["version"] == "0.28.3"
    assert not admits({"id": "0.28.9", "digest": NEW["digest"]}, RANGED, row)


def test_prerelease_needs_a_spec_that_names_one():
    pre = {"id": "0.28.3rc1", "digest": "sha256:" + "e" * 64}
    assert not admits(pre, RANGED, _row(pre))
    loose = {"release": REF, "release_policy": {**POLICY, "spec": ">=0.28.2rc1,<0.29"}}
    assert admits(pre, loose, _row(pre))


def test_catalog_import_uses_the_shared_fold():
    from beadhive import hq_release_catalog

    assert hq_release_catalog.fold_digest is release_measurement.fold_digest
    assert not hasattr(hq_release_catalog, "_fold")


# ---- the signed-beat carrier and the waived mode ------------------------------------------


def _frame(release=REF):
    return hosts.HostManifest(
        host_id="host", frame_id="frame", state="active", release=release,
        capabilities=dict(isolation="kvm", trust_zone="self-hosted", arch="x86_64",
                          harnesses=["codex"], max_sessions=2),
        instance_ref="vm", label="fixture", os="linux", arch="x86_64", role="executor",
        identity={"kind": "none"},
    )  # fmt: skip


def _facts(measured, row, *, desired_extra=None, frame_release=REF):
    lease = HeartbeatLease(
        frame_id="frame", holderIdentity="host", instance_ref="vm", key_id="key", epoch=9,
        audience="fleet", config_revision="config", seq=1,
        renewTime=datetime.now(UTC).isoformat(), release=measured, state_seen="active",
        conformance={"profile": "profile", "status": "conformant",
                     "checks": [{"id": "required", "status": "pass"}]},
        report_digest="sha256:" + "2" * 64,
    )  # fmt: skip
    beat = VerifiedObservation("fresh", True, True, 1.0, lease, release_catalog_row=row)
    frame = _frame(frame_release)
    desired = dict(
        declared=True, state="active", cordoned=False, release=REF,
        caps=frame.capabilities.model_dump(),
        profile="profile", authority={"holder_identity": "host", "instance_ref": "vm"},
        **(desired_extra if desired_extra is not None else {"release_policy": POLICY}),
    )  # fmt: skip
    return frame, eligibility.EligibilityFacts(beat, desired)


def _release_ok(frame, facts):
    return dict(eligibility.eligible(frame, {}, facts).predicates)["release_matches"]


def test_signed_beat_in_range_upgrade_is_eligible_with_the_reference_manifest():
    frame, facts = _facts(NEW, _row())
    assert eligibility.eligible(frame, {}, facts).allowed
    assert _release_ok(frame, facts)


@pytest.mark.parametrize(
    "row",
    [
        None,
        catalog_row_for(_rows(NEW) + _rows(NEW, status="revoked"), "beadhive", NEW),
        _row(algorithm="x"),
    ],
)
def test_signed_beat_refuses_unknown_revoked_and_foreign_algorithm(row):
    frame, facts = _facts(NEW, row)
    assert not _release_ok(frame, facts)


def test_signed_beat_refuses_an_out_of_range_beat_and_a_foreign_manifest():
    far = {"id": "0.30.0", "digest": "sha256:" + "f" * 64}
    assert not _release_ok(*_facts(far, _row(far)))
    stray = {"id": "0.28.4", "digest": "sha256:" + "9" * 64}
    assert not _release_ok(*_facts(NEW, _row(), frame_release=stray))


def test_signed_exact_pin_is_unchanged_by_a_row_or_an_upgraded_beat():
    frame, facts = _facts(REF, None, desired_extra={})
    assert _release_ok(frame, facts)
    frame, facts = _facts(NEW, _row(), desired_extra={})
    assert not _release_ok(frame, facts)


def test_waived_mode_drops_the_range_conjunct_like_the_pin_conjunct():
    frame, facts = _facts(NEW, None)
    facts = replace(facts, authority_waived=True)
    decision = eligibility.eligible(frame, {}, facts)
    assert dict(decision.predicates)["release_matches"] and "release_matches" in decision.waived
    exact_frame, exact = _facts(NEW, None, desired_extra={})
    waived = eligibility.eligible(exact_frame, {}, replace(exact, authority_waived=True))
    assert "release_matches" not in waived.waived  # beat != manifest stays refused
