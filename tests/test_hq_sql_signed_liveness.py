"""Signed liveness mode (``hq.sql.liveness: signed`` / ``BH_HQ_SQL_LIVENESS``), unit level.

Real Ed25519 signatures and the real verifier; only the SQL connection is a fake cursor.
Covers read-time heartbeat selection (bh-0acs8) and advisory hive-lease expiry with no
receiver renewal (bh-7y6b2). The pinned-Dolt proof lives in
``tests/test_fleet_membership_e2e_int.py``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import host, host_lease, localloop
from beadhive import hq_sql_runtime as runtime
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_sql_runtime import (
    LIVENESS_ENV,
    PrincipalBinding,
    SqlRuntimeAuthority,
    SqlRuntimeError,
    liveness_mode,
    newest_signed_heartbeat,
)
from beadhive.hq_sql_signatures import canonical, fingerprint, sign_heartbeat
from beadhive.modules.config.contracts import HqSqlConfig

NOW = float(int(time.time()))
RELEASE = {"id": "fixture", "digest": "sha256:" + "1" * 64}


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


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch):
    monkeypatch.delenv(LIVENESS_ENV, raising=False)


@pytest.fixture
def frame(tmp_path):
    key = tmp_path / "frame.key"
    public = _keypair(key)
    other_key = tmp_path / "other.key"
    _keypair(other_key)
    signer = fingerprint(public)
    route = PrincipalBinding(
        "frame_a", "frame-1", "host-1", "vm-1", 1, "hq_live_inbox_frame_a_1", signer
    )
    authority = {
        "frame_id": "frame-1",
        "holder_identity": "host-1",
        "instance_ref": "vm-1",
        "key_fingerprint": signer,
        "epoch": 1,
        "audience": "fixture-fleet",
        "config_revision": "desired-1",
        "candidate_expires_at": None,
    }
    record = {
        "authority": authority,
        "public_key": public,
        "state": "active",
        "cordoned": False,
        "desired": {"declared": True, "release": RELEASE, "profile": "fixture"},
    }

    def beat(seq, renew_at, *, key_path=None, **overrides):
        """One signed inbox payload; overrides re-sign with a different identity."""
        signing = key_path or key
        key_id = overrides.pop("key_id", None)
        if key_id is None:
            signing_public = serialization.load_ssh_private_key(
                signing.read_bytes(), password=None
            ).public_key()
            key_id = fingerprint(
                signing_public.public_bytes(
                    serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
                ).decode()
            )
        lease = HeartbeatLease(
            audience=overrides.pop("audience", "fixture-fleet"),
            frame_id=overrides.pop("frame_id", "frame-1"),
            holderIdentity="host-1",
            instance_ref="vm-1",
            key_id=key_id,
            epoch=overrides.pop("epoch", 1),
            config_revision="desired-1",
            seq=seq,
            renewTime=datetime.fromtimestamp(renew_at, UTC).isoformat(),
            state_seen="active",
            release=RELEASE,
            report_digest="sha256:" + "2" * 64,
            conformance={"profile": "fixture", "status": "conformant", "checks": []},
        )
        assert not overrides
        return canonical(sign_heartbeat(lease, signing_key=str(signing)))

    return SimpleNamespace(
        route=route, record=record, beat=beat, other_key=other_key, key=key, public=public
    )


def _pick(frame, *payloads, now=NOW):
    return newest_signed_heartbeat([(p,) for p in payloads], frame.route, frame.record, now=now)


# ---- newest_signed_heartbeat -----------------------------------------------------------


def test_greatest_signed_renew_time_wins_with_public_observation_shape(frame):
    row = _pick(frame, frame.beat(1, NOW - 100), frame.beat(3, NOW - 10), frame.beat(2, NOW - 50))
    sequence, digest, accepted_until, lease_json, envelope_json, signer = row
    assert sequence == 3
    assert digest.startswith("sha256:")
    assert accepted_until == NOW - 10 + 300
    assert isinstance(envelope_json, bytes) and b'"seq":3' in envelope_json
    assert signer == frame.route.signer_fingerprint
    assert b'"seq":3' in lease_json


def test_older_valid_envelope_never_outranks_a_newer_one(frame):
    # A higher seq signed with an OLDER renewTime is still older evidence.
    row = _pick(frame, frame.beat(9, NOW - 200), frame.beat(4, NOW - 5))
    assert row[0] == 4
    assert row[2] == NOW - 5 + 300


@pytest.mark.parametrize(
    "bad",
    [
        "wrong-key",
        "wrong-epoch",
        "wrong-frame",
        "wrong-audience",
        "not-json",
        "non-canonical",
        "oversize",
        "null",
        "tampered",
    ],
)
def test_wrong_key_epoch_frame_and_malformed_rows_are_skipped(frame, bad):
    good = frame.beat(1, NOW - 60)
    newer = NOW - 1
    payload = {
        "wrong-key": lambda: frame.beat(7, newer, key_path=frame.other_key),
        "wrong-epoch": lambda: frame.beat(7, newer, epoch=2),
        "wrong-frame": lambda: frame.beat(7, newer, frame_id="frame-2"),
        "wrong-audience": lambda: frame.beat(7, newer, audience="other-fleet"),
        "not-json": lambda: b"{not json",
        "non-canonical": lambda: frame.beat(7, newer).replace(b"{", b"{ ", 1),
        "oversize": lambda: b" " * 70000,
        "null": lambda: None,
        "tampered": lambda: frame.beat(7, newer).replace(b'"seq":7', b'"seq":8'),
    }[bad]()
    row = _pick(frame, payload, good)
    assert row is not None and row[0] == 1


def test_rows_accept_str_and_memoryview_payloads(frame):
    body = frame.beat(5, NOW - 1)
    assert _pick(frame, memoryview(body))[0] == 5
    assert _pick(frame, body.decode())[0] == 5


def test_future_skew_beyond_thirty_seconds_is_skipped(frame):
    assert _pick(frame, frame.beat(2, NOW + 31), frame.beat(1, NOW - 1))[0] == 1
    assert _pick(frame, frame.beat(2, NOW + 30), frame.beat(1, NOW - 1))[0] == 2


def test_nothing_verifiable_is_missing(frame):
    assert _pick(frame) is None
    assert _pick(frame, frame.beat(1, NOW, key_path=frame.other_key)) is None


# ---- _public_observation in signed mode ------------------------------------------------


@pytest.mark.parametrize(
    "age, fresh",
    [(0, True), (299, True), (299.999, True), (300, False), (3600, False)],
)
def test_signed_observation_age_is_reader_clock_minus_signed_renew_time(frame, age, fresh):
    row = _pick(frame, frame.beat(1, NOW))
    observation = SqlControlPlane._public_observation(
        row,
        frame.route,
        "active",
        granted_public_key=frame.public,
        now=NOW + age,
        signed=True,
    )
    assert observation.verified is True
    assert observation.fresh is fresh
    assert observation.status == ("fresh" if fresh else "stale")
    assert observation.age_seconds == pytest.approx(age)
    assert observation.age_basis == "signed-envelope-reader-clock"


def test_signed_observation_clamps_small_future_skew_and_rejects_large(frame):
    row = _pick(frame, frame.beat(1, NOW + 10), now=NOW + 10)

    def observe(now):
        return SqlControlPlane._public_observation(
            row,
            frame.route,
            "active",
            granted_public_key=frame.public,
            now=now,
            signed=True,
        )

    clamped = observe(NOW)
    assert clamped.age_seconds == 0 and clamped.fresh
    with pytest.raises(ControlPlaneError, match="observation invalid"):
        observe(NOW - 25)


def test_receiver_observation_keeps_first_seen_basis(frame):
    row = _pick(frame, frame.beat(1, NOW))
    observation = SqlControlPlane._public_observation(
        row, frame.route, "active", granted_public_key=frame.public, now=NOW + 1
    )
    assert observation.age_basis == "protected-receiver-first-seen"


# ---- read_frame_composite with a fake cursor -------------------------------------------


class _Cursor:
    def __init__(self, world):
        self.world = world
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        world = self.world
        world.statements.append(sql)
        if sql.startswith("SELECT CURRENT_USER"):
            self.rows = [("frame_a@localhost", "rt", "main", "2.3.5")]
        elif sql.startswith("SELECT DOLT_HASHOF"):
            self.rows = [("head",)]
        elif "FROM hq_principal_registry" in sql:
            self.rows = [tuple(vars(world.route).values())]
        elif "FROM hq_live_inbox_frame_a_1" in sql:
            assert "kind='heartbeat'" in sql
            self.rows = [(payload,) for payload in world.inbox]
        elif "FROM hq_live_public_observations" in sql:
            self.rows = [world.public] if world.public else []
        elif sql.startswith("START TRANSACTION"):
            self.rows = []
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, world):
        self.world = world

    def cursor(self):
        return _Cursor(self.world)

    def rollback(self):
        pass

    def close(self):
        pass


def _composite_world(frame, monkeypatch, *, liveness, inbox=(), public=None):
    world = SimpleNamespace(
        route=frame.route, inbox=list(inbox), public=public, statements=[], fences=[]
    )
    state = {"frames": {"frame-1": {"active": frame.record, "candidate": None}}, "expires_at": 1e12}
    policies = {"bh": {"config_revision": "desired-1", "valid_until": 1e12}}
    settings = {"runtime": {"user": "frame_a", "database": "rt"}}
    if liveness is not None:
        settings["liveness"] = liveness
    authority = SqlRuntimeAuthority(settings, broker=object(), clock=lambda: NOW)
    monkeypatch.setattr(
        authority, "_open", lambda deadline=None: (_Connection(world), time.monotonic() + 30)
    )
    monkeypatch.setattr(
        authority, "verified_state_at", lambda *a, **k: (state, ("b", "g", "c"), policies)
    )
    monkeypatch.setattr(authority, "load_config_at", lambda *a, **k: "snapshot")
    monkeypatch.setattr(runtime, "project_hive_policies", lambda *a, **k: policies)
    for fence in (
        "fresh_config_head_fence",
        "fresh_runtime_head_fence",
        "fresh_public_observation_fence",
    ):
        monkeypatch.setattr(
            authority, fence, lambda *a, _name=fence, **k: world.fences.append(_name)
        )
    return authority, world


def test_signed_composite_reads_own_inbox_and_skips_public_observation(frame, monkeypatch):
    frozen = _pick(frame, frame.beat(1, NOW - 4000))
    authority, world = _composite_world(
        frame,
        monkeypatch,
        liveness="signed",
        inbox=[frame.beat(2, NOW - 30), b"junk", frame.beat(3, NOW - 3)],
        public=frozen,
    )
    result = authority.read_frame_composite()
    assert len(result) == 9
    row = result[7]
    assert row[0] == 3 and row[2] == NOW - 3 + 300
    assert not any("hq_live_public_observations" in sql for sql in world.statements)
    assert "fresh_public_observation_fence" not in world.fences


def test_receiver_composite_is_unchanged(frame, monkeypatch):
    frozen = _pick(frame, frame.beat(1, NOW - 4000))
    authority, world = _composite_world(
        frame, monkeypatch, liveness=None, inbox=[frame.beat(3, NOW - 3)], public=frozen
    )
    assert authority.read_frame_composite()[7] == frozen
    assert not any("hq_live_inbox" in sql for sql in world.statements)
    assert "fresh_public_observation_fence" in world.fences


def test_env_override_selects_signed_composite(frame, monkeypatch):
    monkeypatch.setenv(LIVENESS_ENV, "signed")
    authority, world = _composite_world(
        frame, monkeypatch, liveness="receiver", inbox=[frame.beat(3, NOW - 3)]
    )
    assert authority.read_frame_composite()[7][0] == 3
    assert any("hq_live_inbox_frame_a_1" in sql for sql in world.statements)


# ---- BH_HQ_SQL_LIVENESS precedence -----------------------------------------------------


def test_env_override_wins_over_config_key(monkeypatch):
    assert liveness_mode({}) == "receiver"
    assert liveness_mode({"liveness": "signed"}) == "signed"
    monkeypatch.setenv(LIVENESS_ENV, "receiver")
    assert liveness_mode({"liveness": "signed"}) == "receiver"
    monkeypatch.setenv(LIVENESS_ENV, "signed")
    assert liveness_mode({"liveness": "receiver"}) == "signed"
    assert liveness_mode({}) == "signed"


@pytest.mark.parametrize("value", ["", "Signed", "true", "signed ", "off"])
def test_invalid_env_override_fails_loudly(monkeypatch, value):
    monkeypatch.setenv(LIVENESS_ENV, value)
    with pytest.raises(SqlRuntimeError, match=LIVENESS_ENV):
        liveness_mode({"liveness": "receiver"})
    with pytest.raises(ControlPlaneError, match=LIVENESS_ENV):
        SqlControlPlane({})


def test_config_key_validates():
    assert HqSqlConfig.model_validate({}).liveness == "receiver"
    assert HqSqlConfig.model_validate({"liveness": "signed"}).liveness == "signed"
    with pytest.raises(ValueError):
        HqSqlConfig.model_validate({"liveness": "bogus"})
    assert SqlControlPlane({"liveness": "signed"}).signed_liveness is True
    assert SqlControlPlane({}).signed_liveness is False


# ---- advisory hive-lease expiry (bh-7y6b2) ---------------------------------------------


def _hive_lease(host_id="host-1", *, expires=NOW - 3 * 3600, advisory=False):
    return HostLease(
        host_id=host_id,
        label=host_id,
        epoch=7,
        adopted_at=now_stamp(NOW - 4 * 3600),
        expires_at=now_stamp(expires),
        advisory_expiry=advisory,
    )


def test_advisory_expiry_is_not_part_of_the_record():
    plain, advisory = _hive_lease(), _hive_lease(advisory=True)
    assert plain == advisory
    assert advisory.to_record() == plain.to_record()
    assert "advisory_expiry" not in advisory.to_record()
    assert HostLease.from_record(advisory.to_record()).advisory_expiry is False


def test_advisory_holder_lease_three_hours_past_is_held():
    assert _hive_lease().held_by("host-1", NOW)  # bh-12hev: held_by is clock-free everywhere
    assert _hive_lease().is_expired(NOW)  # ...while the non-advisory hint still reads lapsed
    advisory = _hive_lease(advisory=True)
    assert advisory.held_by("host-1", NOW)
    assert not advisory.is_expired(NOW)
    assert not advisory.held_by("host-2", NOW)
    assert host_lease.lease_state(advisory, at=NOW) == "held"
    assert host_lease.lease_state(_hive_lease(), at=NOW) == "free"


def test_advisory_tombstone_is_still_free():
    tombstone = _hive_lease("", advisory=True)
    assert tombstone.is_expired(NOW)
    assert not tombstone.held_by("host-1", NOW)
    assert host_lease.lease_state(tombstone, at=NOW) == "free"


def _lease_plane(frame, settings, lease):
    observation = _pick(frame, frame.beat(4, NOW - 5))
    authority = {"frame_id": "frame-1", **frame.record["authority"]}
    body = canonical({"authority": authority, "lease": lease.to_record()})
    composite = (
        "head",
        {},
        frame.route,
        "active",
        frame.record,
        "snapshot",
        {"bh": {"config_revision": "desired-1", "valid_until": NOW + 3600}},
        observation,
        ("revision-1", body, "request", "sha"),
    )
    plane = SqlControlPlane(settings, clock=lambda: NOW)
    plane._runtime_authority = lambda: SimpleNamespace(
        read_frame_composite=lambda prefix=None, **_: composite
    )
    return plane


@pytest.mark.parametrize("mode", ["config", "env"])
def test_signed_mode_returns_lapsed_holder_lease_default_does_not(frame, monkeypatch, mode):
    lapsed = _hive_lease()
    assert _lease_plane(frame, {}, lapsed).read_hive_lease_record(
        "bh", holder_identity="host-1"
    ) == ("revision-1", None)
    if mode == "env":
        monkeypatch.setenv(LIVENESS_ENV, "signed")
        settings = {}
    else:
        settings = {"liveness": "signed"}
    revision, lease = _lease_plane(frame, settings, lapsed).read_hive_lease_record(
        "bh", holder_identity="host-1"
    )
    assert revision == "revision-1" and lease == lapsed
    assert lease.advisory_expiry and lease.held_by("host-1", NOW)
    assert lease.epoch == 7


@pytest.mark.parametrize("liveness", [None, "signed"])
@pytest.mark.parametrize("holder", ["", "host-2"])
def test_tombstone_and_foreign_holder_refused_in_both_modes(frame, liveness, holder):
    settings = {} if liveness is None else {"liveness": liveness}
    plane = _lease_plane(frame, settings, _hive_lease(holder, expires=NOW + 3600))
    assert plane.read_hive_lease_record("bh", holder_identity="host-1")[1] is None
    _revision, physical = plane.read_hive_lease_record("bh")
    assert not physical.held_by("host-1", NOW)


# ---- no receiver renewal on the dev path -----------------------------------------------


def _forbid_receiver(monkeypatch):
    def fail(*_a, **_k):
        pytest.fail("signed liveness must make no receiver round trip")

    monkeypatch.setattr(SqlControlPlane, "publish_hive_lease", fail)
    monkeypatch.setattr(SqlControlPlane, "propose_hive_lease", fail)
    monkeypatch.setattr(host_lease, "renew", fail)
    monkeypatch.setattr(host_lease, "_frame_plane", fail)


def _select_sql(monkeypatch, liveness=None):
    """A selected SQL HOST whose plane is the real SqlControlPlane over `liveness`."""
    from beadhive import hq_control_plane

    settings = {} if liveness is None else {"liveness": liveness}
    monkeypatch.setattr(host, "sql_hq_selected", lambda: True)
    monkeypatch.setattr(
        hq_control_plane, "control_plane", lambda *_a, **_k: SqlControlPlane(settings)
    )


@pytest.mark.parametrize("mode", ["config", "env"])
def test_renew_if_due_is_a_no_op_in_signed_mode(tmp_path, monkeypatch, mode):
    _select_sql(monkeypatch, "signed" if mode == "config" else "receiver")
    if mode == "env":
        monkeypatch.setenv(LIVENESS_ENV, "signed")
    _forbid_receiver(monkeypatch)
    assert host_lease.renew_if_due("origin", "bh", host_id="host-1", cwd=tmp_path, at=NOW) is None


def test_renew_if_due_still_renews_in_receiver_mode(tmp_path, monkeypatch):
    _select_sql(monkeypatch)
    due = _hive_lease(expires=NOW + 10)
    plane = SimpleNamespace(read_hive_lease=lambda prefix, holder_identity: due)
    monkeypatch.setattr(host_lease, "_frame_plane", lambda cwd: plane)
    renewed = []
    monkeypatch.setattr(
        host_lease, "renew", lambda *a, **k: renewed.append(k) or SimpleNamespace(sha="s")
    )
    monkeypatch.setattr(host_lease, "cache", lambda *a, **k: None)
    assert host_lease.renew_if_due("origin", "bh", host_id="host-1", cwd=tmp_path, at=NOW)
    assert len(renewed) == 1


def test_invalid_env_override_still_fails_at_the_plane_not_at_renewal(tmp_path, monkeypatch):
    _select_sql(monkeypatch, "signed")
    monkeypatch.setenv(LIVENESS_ENV, "maybe")
    _forbid_receiver(monkeypatch)
    assert host_lease.renew_if_due("origin", "bh", host_id="host-1", cwd=tmp_path, at=NOW) is None
    with pytest.raises(ControlPlaneError, match=LIVENESS_ENV):
        _ = SqlControlPlane({"liveness": "signed"}).signed_liveness


def test_work_loop_keeper_holds_a_lapsed_signed_lease_without_renewing(tmp_path, monkeypatch):
    _select_sql(monkeypatch, "signed")
    _forbid_receiver(monkeypatch)
    lease = _hive_lease(advisory=True)
    backend = SimpleNamespace(read_hive_lease=lambda prefix, holder_identity: lease)
    keeper = localloop.HostLeaseKeeper(
        prefix="bh",
        host_id="host-1",
        hq_dir=tmp_path,
        ttl=1800,
        renew_interval=300,
        backend=backend,
    )
    status = keeper.renew(active=True)
    assert status.held and not status.renewed
    assert lease.epoch == 7


# ---- BH_HQ_AUTHORITY_ENFORCE=false at the SQL read boundary (UNSUPPORTED, bh-6pqul) ----------

ENFORCE_ENV = "BH_HQ_AUTHORITY_ENFORCE"


def _recording_composite(frame, monkeypatch):
    frozen = _pick(frame, frame.beat(1, NOW - 4000))
    authority, world = _composite_world(frame, monkeypatch, liveness=None, public=frozen)
    original = authority.verified_state_at
    seen = SimpleNamespace(allow_expired=[], config_heads=[])

    def verified(*a, **k):
        seen.allow_expired.append(k.get("allow_expired", False))
        return original(*a, **k)

    monkeypatch.setattr(authority, "verified_state_at", verified)
    monkeypatch.setattr(
        authority, "fresh_config_head_fence", lambda head, **k: seen.config_heads.append(head)
    )
    return authority, seen


def test_enforced_composite_is_unchanged(frame, monkeypatch):
    authority, seen = _recording_composite(frame, monkeypatch)
    assert authority.read_frame_composite()[5] == "snapshot"
    assert seen.allow_expired == [False] and seen.config_heads == ["c"]


def test_unenforced_composite_tolerates_expiry_and_unbound_config_head(frame, monkeypatch):
    authority, seen = _recording_composite(frame, monkeypatch)
    latest = SimpleNamespace(commit_revision="latest-head", beadyard_id=None)
    monkeypatch.setattr(
        authority, "load_config_at", lambda *a, **k: pytest.fail("bound config required")
    )
    monkeypatch.setattr(authority, "load_latest_config_at", lambda *a, **k: latest)
    monkeypatch.setattr(
        runtime, "project_hive_policies", lambda *a, **k: pytest.fail("projection compared")
    )
    assert authority.read_frame_composite(enforce=False)[5] is latest
    # Expiry is accepted; the config fence pins the head actually read, not the cross-reference.
    assert seen.allow_expired == [True] and seen.config_heads == ["latest-head"]


def _eligibility_plane(frame, record, policies):
    observation = _pick(frame, frame.beat(4, NOW - 5))
    calls = []

    def composite(**kwargs):
        calls.append(kwargs)
        return (
            "head",
            {},
            frame.route,
            "active",
            record,
            SimpleNamespace(beadyard_id=None),
            policies,
            observation,
            None,
        )

    plane = SqlControlPlane({}, clock=lambda: NOW)
    plane._runtime_authority = lambda: SimpleNamespace(read_frame_composite=composite)
    manifest = SimpleNamespace(
        frame_id="frame-1", host_id="host-1", instance_ref="vm-1", beadyard_id=None
    )
    return plane, manifest, calls


def test_config_unbound_eligibility_read_fails_closed_unless_disabled(frame, monkeypatch):
    unbound = {"bh": {"config_revision": "other-revision", "valid_until": NOW + 3600}}
    plane, manifest, calls = _eligibility_plane(frame, frame.record, unbound)
    with pytest.raises(ControlPlaneError, match="differs from current grant"):
        plane.read_eligibility(manifest)
    monkeypatch.setenv(ENFORCE_ENV, "true")
    with pytest.raises(ControlPlaneError, match="differs from current grant"):
        plane.read_eligibility(manifest)
    monkeypatch.setenv(ENFORCE_ENV, "false")
    head, desired, observation = plane.read_eligibility(manifest)
    assert head == "head" and desired["state"] == "active" and observation.verified
    assert [call["enforce"] for call in calls] == [True, True, False]


def test_identity_mismatch_still_fails_closed_when_disabled(frame, monkeypatch):
    monkeypatch.setenv(ENFORCE_ENV, "false")
    plane, manifest, _calls = _eligibility_plane(frame, frame.record, {})
    with pytest.raises(ControlPlaneError, match="differs from current grant"):
        plane.read_eligibility(SimpleNamespace(**{**vars(manifest), "instance_ref": "vm-2"}))


def test_cordoned_holder_lease_waived_only_when_disabled(frame, monkeypatch):
    cordoned = SimpleNamespace(**{**vars(frame), "record": {**frame.record, "cordoned": True}})
    lease = _hive_lease(expires=NOW + 3600)
    plane = _lease_plane(cordoned, {}, lease)
    assert plane.read_hive_lease_record("bh", holder_identity="host-1") == ("revision-1", None)
    monkeypatch.setenv(ENFORCE_ENV, "false")
    assert plane.read_hive_lease_record("bh", holder_identity="host-1") == ("revision-1", lease)
    # Lease ownership is not authority: a foreign holder is still refused.
    foreign = _lease_plane(cordoned, {}, _hive_lease("host-2", expires=NOW + 3600))
    assert foreign.read_hive_lease_record("bh", holder_identity="host-1")[1] is None
