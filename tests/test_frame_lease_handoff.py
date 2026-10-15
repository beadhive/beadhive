"""Moving a hive off a stale frame (bh-rlzab).

A session-only incarnation cannot publish its own hive-lease release, so the refusal must name
the director's placement verb instead of the runtime's internal "has no signed inbox". A lease
whose recorded expiry has passed under signed/session liveness is still held (ADR §4: expiry is
a failover hint), so adopt's refusal must explain that instead of advising to wait for expiry.

Every clock here is a fixed number passed as ``at``; nothing sleeps or reads wall time, and
nothing reaches a live HQ, lease or hive remote.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import host_adopt, host_fence, host_lease
from beadhive.host_fence import EpochFence
from beadhive.host_lease import HostLeaseRejected, held_refusal
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_sql_runtime import PrincipalBinding, SessionOnlyIncarnation

NOW = 1_791_400_000.0  # fixed: 2026-10-07-ish, never the wall clock
DAY = 86400.0


def _lease(*, expires_at: float, advisory: bool) -> HostLease:
    return HostLease(
        host_id="beadhive-factory",
        label="factory",
        epoch=1,
        adopted_at=now_stamp(expires_at - 7200),
        expires_at=now_stamp(expires_at),
        advisory_expiry=advisory,
    )


# ---- HostLease.hint_elapsed -----------------------------------------------------------------


def test_hint_elapsed_only_for_an_advisory_holder_past_its_recorded_expiry():
    past = _lease(expires_at=NOW - 4 * DAY, advisory=True)
    assert past.hint_elapsed(NOW) and not past.is_expired(NOW)
    assert not _lease(expires_at=NOW + DAY, advisory=True).hint_elapsed(NOW)
    # A non-advisory lease's expiry is real: is_expired answers, not the hint.
    real = _lease(expires_at=NOW - DAY, advisory=False)
    assert real.is_expired(NOW) and not real.hint_elapsed(NOW)
    # The fence-derived holder carries no parseable stamp; never "elapsed".
    unknown = HostLease("h", "", 3, "unknown", "unknown", advisory_expiry=True)
    assert not unknown.hint_elapsed(NOW)
    tombstone = HostLease("", "", 3, now_stamp(NOW - DAY), now_stamp(NOW - DAY), True)
    assert not tombstone.hint_elapsed(NOW)


# ---- adopt's refusal on a held lease --------------------------------------------------------


def test_refusal_for_a_live_lease_keeps_the_wait_advice():
    message = held_refusal("bh-app", _lease(expires_at=NOW + DAY, advisory=True), at=NOW)
    assert "held by another host" in message
    assert "Wait for it to expire" in message
    assert "--force" in message


def test_refusal_for_an_advisory_lease_past_expiry_explains_why_expiry_does_not_apply():
    message = held_refusal("bh-app", _lease(expires_at=NOW - 4 * DAY, advisory=True), at=NOW)
    assert "Wait for it to expire" not in message
    assert "only a failover hint" in message
    assert "ADR §4" in message
    assert "host.lease.evict_after_s" in message
    assert "bh hq placement place bh-app" in message
    assert "--force" in message


def _adopt_harness(tmp_path, monkeypatch, lease):
    hive = tmp_path / "hive"
    (hive / ".git").mkdir(parents=True)
    monkeypatch.setattr("beadhive.frame_eligibility.require_eligible", lambda *a, **k: None)
    monkeypatch.setattr("beadhive.frame_eligibility.evictable", lambda *a, **k: False)
    monkeypatch.setattr(host_adopt, "_director_placement", lambda *a, **k: None)
    monkeypatch.setattr(host_fence, "read_fence", lambda *a, **k: ("sha1", EpochFence(1, "f")))
    monkeypatch.setattr(host_lease, "read", lambda *a, **k: lease)
    monkeypatch.setattr(
        host_fence, "install_fence", lambda *a, **k: pytest.fail("fence moved on a held hive")
    )
    return hive


def test_two_phase_adopt_of_a_past_expiry_signed_lease_refuses_with_the_reason(
    tmp_path, monkeypatch
):
    lease = _lease(expires_at=NOW - 4 * DAY, advisory=True)
    hive = _adopt_harness(tmp_path, monkeypatch, lease)
    with pytest.raises(HostLeaseRejected, match="only a failover hint"):
        host_adopt.adopt(
            prefix="bh-app",
            hive_remote="origin",
            hq_remote="origin",
            hive_cwd=hive,
            hq_cwd=tmp_path,
            host_id="xeno-mac",
            label="mac",
            at=NOW,
        )


def test_two_phase_adopt_of_a_past_expiry_legacy_lease_needs_no_force(tmp_path, monkeypatch):
    """Non-advisory (legacy Git) expiry is real: past it, adopt proceeds without --force."""
    lease = _lease(expires_at=NOW - 4 * DAY, advisory=False)
    hive = _adopt_harness(tmp_path, monkeypatch, lease)
    moved = []

    def install(*_a, **_k):
        moved.append(True)
        raise RuntimeError("stop after the refusal gate")

    monkeypatch.setattr(host_fence, "install_fence", install)
    monkeypatch.setattr(host_adopt, "_require_hive_policy", lambda *a, **k: None)
    monkeypatch.setattr(host_adopt, "fence_data_for", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="stop after the refusal gate"):
        host_adopt.adopt(
            prefix="bh-app",
            hive_remote="origin",
            hq_remote="origin",
            hive_cwd=hive,
            hq_cwd=tmp_path,
            host_id="xeno-mac",
            label="mac",
            at=NOW,
        )
    assert moved == [True]


def test_single_phase_lease_adopt_uses_the_same_refusal(tmp_path, monkeypatch):
    lease = _lease(expires_at=NOW - 4 * DAY, advisory=True)
    monkeypatch.setattr("beadhive.frame_eligibility.require_eligible", lambda *a, **k: None)
    monkeypatch.setattr("beadhive.frame_eligibility.evictable", lambda *a, **k: False)
    monkeypatch.setattr(host_lease, "_read", lambda *a, **k: ("sha1", lease))
    with pytest.raises(HostLeaseRejected, match="only a failover hint"):
        host_lease.adopt("origin", "bh-app", host_id="xeno-mac", label="mac", cwd=tmp_path, at=NOW)


# ---- a session-only frame releasing its own lease -------------------------------------------


def _session_only_plane(monkeypatch):
    route = PrincipalBinding(
        "frame_f", "frame-f", "beadhive-factory", "vm-f", 1, "frame_f_1_session", "sig"
    )
    record = {
        "authority": {
            "frame_id": "frame-f",
            "holder_identity": "beadhive-factory",
            "instance_ref": "vm-f",
            "key_fingerprint": "sig",
            "epoch": 1,
            "audience": "fleet",
            "config_revision": "rev-1",
            "candidate_expires_at": None,
        },
        "state": "active",
        "cordoned": False,
    }
    published = []

    def publish_inbox(kind, payload, **_kwargs):
        published.append(kind)
        raise SessionOnlyIncarnation("session-only incarnation has no signed inbox")

    runtime = SimpleNamespace(
        load_state=lambda **_k: ("head", {"frames": {"frame-f": {"active": record}}}, None, None),
        load_frame_binding=lambda **_k: route,
        publish_inbox=publish_inbox,
    )
    plane = SqlControlPlane({}, clock=lambda: NOW)
    plane.settings["runtime"] = {"operation_timeout": 5}
    monkeypatch.setattr(plane, "_runtime_authority", lambda: runtime)
    monkeypatch.setattr(
        "beadhive.hq_sql_signatures.sign_hive_request", lambda request, signing_key: request
    )
    return plane, published


def test_session_only_release_names_the_director_placement_verb(monkeypatch):
    plane, published = _session_only_plane(monkeypatch)
    tombstone = HostLease("", "", 1, now_stamp(NOW - DAY), now_stamp(NOW))
    with pytest.raises(ControlPlaneError) as caught:
        plane.propose_hive_lease(
            "bh-app", tombstone, expected="rev", operation="release", signing_key="k"
        )
    message = str(caught.value)
    assert published == ["hive_lease"]
    assert "has no signed inbox" not in message
    assert "session-only" in message
    assert "bh hq placement release bh-app --expected-revision <R> --confirm" in message
    assert "placement_writer" in message
    assert "No hive-lease proposal was published" in message
