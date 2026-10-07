"""Session/evidence liveness, unit level (bh-owqdg, P-M9; ADR §5).

The data switch in ``read_frame_composite``, the session branch of frame eligibility, the
``BH_FRAME_HEARTBEAT`` logged no-op, claim-time audit stamps, Φ3 dual-write routing and the
sender verbs. Only SQL connections are fakes; the pinned-Dolt proof is
``tests/test_hq_sql_session_int.py``.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from beadhive import claim_authority, heartbeat_sender, hosts, work_assignment
from beadhive import frame_eligibility as policy
from beadhive import hq_sql_runtime as runtime
from beadhive.hq_control_plane import SqlControlPlane
from beadhive.hq_sql_operator import SqlRuntimeOperator
from beadhive.hq_sql_placement import PROTECTED_TABLES, check_triggers
from beadhive.hq_sql_runtime import (
    LIVENESS_ENV,
    PrincipalBinding,
    SessionOnlyIncarnation,
    SqlRuntimeAuthority,
    SqlRuntimeError,
)
from beadhive.hq_sql_runtime_schema import (
    LIVENESS_POLICY_TABLE,
    evidence_table,
    inbox_table,
    routed_table_valid,
    routed_to_inbox,
    session_table,
)
from beadhive.hq_sql_session import (
    ELIGIBILITY_SQL,
    EvidenceReport,
    LivenessPolicy,
    SessionError,
    SessionObservation,
    account_statements,
    evidence_ddl,
    liveness_schema_statements,
    pinned_address,
    session_ddl,
)
from beadhive.hq_sql_session_provision import check_account

NOW = float(int(time.time()))
RELEASE = {"id": "fixture", "digest": "sha256:" + "1" * 64}
SIGNER = "SHA256:fixture"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(LIVENESS_ENV, raising=False)
    monkeypatch.delenv(policy.HEARTBEAT_ENV, raising=False)
    policy.last_admission()


# ---- naming, DDL and the account ------------------------------------------------------------


def test_incarnation_tables_are_route_derived_and_ignored():
    assert session_table("frame_a", 3) == "frame_frame_a_3_session"
    assert evidence_table("frame_a", 3) == "frame_frame_a_3_evidence"
    assert routed_table_valid(inbox_table("frame_a", 3), "frame_a", 3)
    assert routed_table_valid(session_table("frame_a", 3), "frame_a", 3)
    assert not routed_table_valid(evidence_table("frame_a", 3), "frame_a", 3)
    assert not routed_table_valid(session_table("frame_a", 4), "frame_a", 3)
    assert routed_to_inbox(inbox_table("frame_a", 3), "frame_a", 3)
    assert not routed_to_inbox(session_table("frame_a", 3), "frame_a", 3)
    assert liveness_schema_statements()[0] == "INSERT INTO dolt_ignore VALUES ('frame_*', TRUE)"
    assert all(len(name) <= 64 for name in (session_table("a" * 31, 9999999999),))


def test_session_ddl_is_single_row_update_stamped_and_never_seeded_live():
    create, trigger, seed = session_ddl("frame_a", 1)
    assert "CHECK (id = 1)" in create and "renewed_at DATETIME(6) NULL" in create
    assert trigger.endswith(
        "BEFORE UPDATE ON frame_frame_a_1_session "
        "FOR EACH ROW SET NEW.renewed_at = UTC_TIMESTAMP(6)"
    )
    assert "NULL)" in seed  # provisioning alone never makes the frame look live
    # The trigger only stamps NEW: condition 6's conformance check passes it, and flags DML.
    create, trigger, _seed = evidence_ddl("frame_a", 1)
    assert "SET NEW.measured_at = UTC_TIMESTAMP(6)" in trigger and "CHECK (id = 1)" in create
    body = "SET NEW.renewed_at = UTC_TIMESTAMP(6)"
    assert check_triggers([("t", session_table("frame_a", 1), body)]) == []
    assert check_triggers(
        [("t", session_table("frame_a", 1), "UPDATE frame_frame_b_1_session SET epoch = 1")]
    )
    assert LIVENESS_POLICY_TABLE in PROTECTED_TABLES


def test_accounts_are_host_pinned_and_require_tls():
    statements = account_statements("frame_a", 1, frame_address="10.0.0.5", database="rt")
    assert statements[0] == "CREATE USER 'frame_a'@'10.0.0.5' IDENTIFIED BY %s REQUIRE SSL"
    assert all("'frame_a'@'10.0.0.5'" in statement for statement in statements)
    for address in ("%", "", "10.0.0.%", "frame_host", "10.0.0.0/255.0.0.0", "a b"):
        assert not pinned_address(address)
        with pytest.raises(SessionError, match="pinned"):
            account_statements("frame_a", 1, frame_address=address, database="rt")
    assert check_account("10.0.0.5", "ANY", secure_transport=False) == []
    assert check_account("10.0.0.5", "", secure_transport=True) == []
    assert check_account("%", "ANY", secure_transport=True)
    assert check_account("10.0.0.5", "", secure_transport=False)


def test_policy_and_evidence_values_are_refused_never_clamped():
    for bad in (0, -1, 7 * 86400 + 1, 1.5, True):
        with pytest.raises(SessionError):
            LivenessPolicy(session_ttl_s=bad)
    with pytest.raises(SessionError):
        EvidenceReport(1, "r", "not-a-digest", "p", "conformant")
    with pytest.raises(SessionError):
        EvidenceReport(1, "r", RELEASE["digest"], "p", "great")


# ---- the data switch in read_frame_composite ----------------------------------------------


def _route(table=None):
    return PrincipalBinding(
        "frame_a", "frame-1", "host-1", "vm-1", 1, table or inbox_table("frame_a", 1), SIGNER
    )


def _record():
    return {
        "authority": {
            "frame_id": "frame-1",
            "holder_identity": "host-1",
            "instance_ref": "vm-1",
            "key_fingerprint": SIGNER,
            "epoch": 1,
            "audience": "fixture-fleet",
            "config_revision": "desired-1",
            "candidate_expires_at": None,
        },
        "public_key": "unused",
        "state": "active",
        "cordoned": False,
        "desired": {"declared": True, "release": RELEASE, "profile": "fixture"},
    }


SESSION_ROW = (
    1, 1, "2026-10-06 10:00:00.000001", 1, "fixture", RELEASE["digest"], "fixture",
    "conformant", "", "2026-10-06 09:58:00.000001", 2_000_000, 120_000_000, 300, 900,
    1, 1, 1, 0, "ab" * 32,
)  # fmt: skip


class _Cursor:
    def __init__(self, world):
        self.world, self.rows, self.rowcount = world, [], 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        world = self.world
        world.statements.append((sql, params))
        if sql.startswith("SELECT CURRENT_USER"):
            self.rows = [("frame_a@10.0.0.5", "rt", "main", "2.3.5")]
        elif sql.startswith("SELECT DOLT_HASHOF"):
            self.rows = [("head",)]
        elif "AS authenticated_fresh_heartbeat" in sql:
            self.rows = [world.session_row]
        elif "FROM hq_principal_registry" in sql:
            self.rows = [tuple(vars(world.route).values())]
        elif "information_schema.tables" in sql:
            self.rows = [(name,) for name in params if name in world.tables]
        elif "hq_live_inbox_" in sql:
            self.rows = []
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


def _world(monkeypatch, *, tables=(), table=None, liveness=None, public=None):
    world = SimpleNamespace(
        route=_route(table),
        tables=set(tables),
        session_row=SESSION_ROW,
        public=public,
        statements=[],
        fences=[],
    )
    state = {"frames": {"frame-1": {"active": _record(), "candidate": None}}, "expires_at": 1e12}
    policies = {"bh": {"config_revision": "desired-1", "valid_until": 1e12}}
    settings = {"runtime": {"user": "frame_a", "database": "rt"}}
    if liveness:
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


BOTH = (session_table("frame_a", 1), evidence_table("frame_a", 1))
PUBLIC = (4, "sha256:" + "3" * 64, NOW + 100, b"{}", b"{}", SIGNER)


def test_switched_incarnation_reads_one_statement_and_skips_the_inbox(monkeypatch):
    authority, world = _world(monkeypatch, tables=BOTH, liveness="signed")
    result = authority.read_frame_composite(session_liveness=True, session_prefix="bh")
    assert len(result) == 10 and result[7] is None
    session = result[9]
    assert isinstance(session, SessionObservation) and session.carrier == "session"
    assert session.predicates == {
        "authenticated_fresh_heartbeat": True,
        "conformance_pass": True,
        "release_matches": True,
        "current_hive_lease_holder": False,
    }
    assert session.age_seconds == 2.0 and session.evidence_age_seconds == 120.0
    assert session.evidence_digest == "sha256:" + "ab" * 32
    eligibility = [
        params
        for sql, params in world.statements
        if sql == ELIGIBILITY_SQL.format(session=BOTH[0], evidence=BOTH[1])
    ]
    # The grant, profile and placement inputs: AS OF the authority head, for this principal.
    assert eligibility == [("fixture", RELEASE["digest"], "head", "head", "bh", "frame_a", 1)]
    assert not any("hq_live_inbox_" in sql for sql, _ in world.statements)
    assert "fresh_public_observation_fence" not in world.fences


def test_legacy_incarnation_reads_exactly_as_before(monkeypatch):
    authority, world = _world(monkeypatch, tables=(), public=PUBLIC)
    result = authority.read_frame_composite(session_liveness=True)
    assert len(result) == 10 and result[9] is None and result[7] == PUBLIC
    assert "fresh_public_observation_fence" in world.fences
    assert not any("AS authenticated_fresh_heartbeat" in sql for sql, _ in world.statements)


def test_phase3_dual_write_keeps_the_inbox_reader_for_0_22_callers(monkeypatch):
    """Φ3: registry still names the inbox and session tables exist. A caller that does not ask
    for session liveness (every 0.22.x-shaped read) gets the signed/receiver observation."""
    authority, world = _world(monkeypatch, tables=BOTH, public=PUBLIC)
    result = authority.read_frame_composite()
    assert len(result) == 9 and result[7] == PUBLIC
    signed, _world_signed = _world(monkeypatch, tables=BOTH, liveness="signed")
    signed.read_frame_composite()
    assert any("hq_live_inbox_frame_a_1" in sql for sql, _ in _world_signed.statements)
    # The inbox stays publishable for the dual-written beat.
    assert routed_to_inbox(world.route.inbox_table, "frame_a", 1)


def test_session_only_incarnation_has_no_inbox(monkeypatch):
    authority, world = _world(
        monkeypatch, tables=BOTH, table=session_table("frame_a", 1), liveness="signed"
    )
    assert authority.read_frame_composite()[7] is None
    assert not any("hq_live_inbox_" in sql for sql, _ in world.statements)
    with pytest.raises(SessionOnlyIncarnation):
        authority.publish_inbox(
            "heartbeat", {}, binding=world.route, expected_head="head", deadline=None
        )


def test_half_provisioned_incarnation_fails_closed(monkeypatch):
    authority, _world_half = _world(monkeypatch, tables=BOTH[:1])
    with pytest.raises(SqlRuntimeError, match="half-provisioned"):
        authority.read_frame_composite(session_liveness=True)


def test_plane_read_eligibility_returns_the_session_observation(monkeypatch):
    authority, _ = _world(monkeypatch, tables=BOTH)
    plane = SqlControlPlane({}, clock=lambda: NOW)
    monkeypatch.setattr(plane, "_runtime_authority", lambda: authority)
    snapshot = SimpleNamespace(beadyard_id=None)
    monkeypatch.setattr(authority, "load_config_at", lambda *a, **k: snapshot)
    manifest = SimpleNamespace(
        frame_id="frame-1", host_id="host-1", instance_ref="vm-1", beadyard_id=None
    )
    _head, desired, observation = plane.read_eligibility(manifest, prefix="bh")
    assert observation.carrier == "session" and desired["state"] == "active"
    assert plane.observe(manifest) == replace(observation)


# ---- frame eligibility on the session carrier ---------------------------------------------


def _session_obs(**overrides):
    values = dict(
        status="fresh",
        fresh=True,
        age_seconds=2.0,
        authenticated_fresh_heartbeat=True,
        conformance_pass=True,
        release_matches=True,
        current_hive_lease_holder=True,
        session_epoch_matches=True,
        renewed_at="2026-10-06T10:00:00+00:00",
        measured_at="2026-10-06T09:58:00+00:00",
        evidence_age_seconds=120.0,
        evidence_status="conformant",
        evidence_release=RELEASE,
        evidence_profile="profile",
        evidence_digest="sha256:" + "ab" * 32,
        session_ttl_s=300,
        evidence_ttl_s=900,
        session_table=BOTH[0],
        evidence_table=BOTH[1],
        predicates={"authenticated_fresh_heartbeat": True},
    )
    values.update(overrides)
    return SessionObservation(**values)


@pytest.fixture
def frame():
    caps = dict(
        isolation="kvm", trust_zone="self-hosted", arch="x86_64", harnesses=["codex"],
        max_sessions=2,
    )  # fmt: skip
    manifest = hosts.HostManifest(
        host_id="host", frame_id="frame", state="active", release=RELEASE, capabilities=caps,
        instance_ref="vm", label="fixture", os="linux", arch="x86_64", role="executor",
        identity={"kind": "none"},
    )  # fmt: skip
    desired = dict(
        declared=True, state="active", cordoned=False, release=RELEASE, caps=caps,
        profile="profile", authority={"holder_identity": "host", "instance_ref": "vm"},
    )  # fmt: skip
    return manifest, desired


def test_session_predicates_come_from_the_statement(frame):
    manifest, desired = frame
    decision = policy.eligible(manifest, {}, policy.EligibilityFacts(_session_obs(), desired))
    assert decision.allowed, decision.reason
    stamps = dict(decision.admission)
    assert stamps["session_renewed_at"] and stamps["evidence_digest"].startswith("sha256:")
    for field, predicate in (
        ("authenticated_fresh_heartbeat", "authenticated_fresh_heartbeat"),
        ("conformance_pass", "conformance_pass"),
        ("release_matches", "release_matches"),
        ("session_epoch_matches", "current_frame_incarnation"),
    ):
        facts = policy.EligibilityFacts(_session_obs(**{field: False}), desired)
        assert policy.eligible(manifest, {}, facts).reason == predicate


def test_stale_session_is_never_waived_by_the_advisory_escape(frame, monkeypatch, capsys):
    manifest, desired = frame
    monkeypatch.setenv(policy.HEARTBEAT_ENV, "advisory")
    stale = _session_obs(authenticated_fresh_heartbeat=False, fresh=False, status="stale")
    assert policy._heartbeat_advisory(stale, manifest) is False
    assert "BH_FRAME_HEARTBEAT ignored: session liveness" in capsys.readouterr().err
    facts = policy.EligibilityFacts(stale, desired, heartbeat_advisory=False)
    assert policy.eligible(manifest, {}, facts).reason == "authenticated_fresh_heartbeat"
    # Even an invalid value is only logged on a switched frame, never raised.
    monkeypatch.setenv(policy.HEARTBEAT_ENV, "bogus")
    assert policy._heartbeat_advisory(stale, manifest) is False
    # A legacy observation keeps today's behavior.
    from beadhive.host_heartbeat_core import VerifiedObservation

    monkeypatch.setenv(policy.HEARTBEAT_ENV, "advisory")
    assert policy._heartbeat_advisory(VerifiedObservation("stale"), manifest) is True


def test_load_facts_names_the_hive_to_a_session_reader(frame, monkeypatch):
    manifest, desired = frame
    calls = []

    class Plane:
        session_liveness_reader = True

        def read_eligibility(self, _frame, *, now=None, prefix=None):
            calls.append(prefix)
            return "head", desired, _session_obs()

    monkeypatch.setattr("beadhive.hq_control_plane.control_plane", lambda _root: Plane())
    facts = policy.load_facts(manifest, hq_dir=".", cfg={}, prefix="bh")
    assert calls == ["bh"] and facts.observation.carrier == "session"


# ---- claim-time audit stamps --------------------------------------------------------------


def test_claim_record_carries_the_admitted_stamps(tmp_path, monkeypatch, frame):
    manifest, desired = frame
    decision = policy.eligible(manifest, {}, policy.EligibilityFacts(_session_obs(), desired))
    monkeypatch.setattr(policy, "require_local", lambda *a, **k: decision)
    lease = SimpleNamespace(held_by=lambda identity: True)
    monkeypatch.setattr(policy, "authoritative_primary", lambda *a, **k: ("bh", "host", lease))
    monkeypatch.setattr("beadhive.hq_authority_expiry.warn_if_expiring", lambda: None)
    policy.require_intake(hive_dir=tmp_path)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    api = SimpleNamespace(
        claim_authority=claim_authority,
        config=SimpleNamespace(claim_authority=lambda cfg, entry: "local"),
        _claim_fence=lambda cfg, hive: ("host", 3),
    )
    work_assignment.impl__issue_claim(api, {}, {}, "bh-1", "dev/x", worktree, "")
    record = claim_authority.get_authority("local").read(worktree)
    assert record.epoch == 3
    assert record.admission["session_renewed_at"] == "2026-10-06T10:00:00+00:00"
    assert record.admission["evidence_measured_at"] == "2026-10-06T09:58:00+00:00"
    assert record.admission["evidence_digest"] == "sha256:" + "ab" * 32
    assert record.admission["admitted_at"]
    # Consumed: the next claim never inherits it, and a legacy claim records nothing.
    assert policy.last_admission() == {}
    work_assignment.impl__issue_claim(api, {}, {}, "bh-1", "dev/x", worktree, "")
    assert claim_authority.get_authority("local").read(worktree).admission == {}


# ---- operator registry routing and the sender --------------------------------------------


class _OperatorCursor:
    def __init__(self, present):
        self.present, self.rows = present, []

    def execute(self, sql, params):
        self.rows = [(name,) for name in params if name in self.present]

    def fetchall(self):
        return self.rows


def test_registry_names_the_inbox_during_phase3_and_the_session_table_after():
    inbox, session, evidence = (
        inbox_table("frame_a", 2), session_table("frame_a", 2), evidence_table("frame_a", 2)
    )  # fmt: skip
    pick = SqlRuntimeOperator._provisioned_table
    assert pick(_OperatorCursor({inbox}), "frame_a", 2) == inbox
    assert pick(_OperatorCursor({inbox, session, evidence}), "frame_a", 2) == inbox
    assert pick(_OperatorCursor({session, evidence}), "frame_a", 2) == session
    with pytest.raises(ValueError):
        pick(_OperatorCursor({session}), "frame_a", 2)


def test_sender_beat_skips_a_session_only_incarnation(monkeypatch, capsys):
    def session_only(_free):
        raise SessionOnlyIncarnation("no inbox")

    monkeypatch.setattr(heartbeat_sender, "_beat", session_only)
    assert heartbeat_sender.main(["beat"]) == 0
    assert "session-only" in capsys.readouterr().err


def test_sender_renew_is_a_no_op_off_switched_frames(monkeypatch, capsys):
    from beadhive import heartbeat_report
    from beadhive.hq_sql_session import NotSwitched

    monkeypatch.setattr(heartbeat_report, "session_renewer", lambda: None)
    assert heartbeat_sender.main(["renew"]) == 0

    class Legacy:
        def tick(self):
            raise NotSwitched("legacy")

    monkeypatch.setattr(heartbeat_report, "session_renewer", lambda: Legacy())
    assert heartbeat_sender.main(["renew"]) == 0
    assert "not switched" in capsys.readouterr().err

    class Broken:
        def tick(self):
            raise RuntimeError("password=SECRET")

    monkeypatch.setattr(heartbeat_report, "session_renewer", lambda: Broken())
    assert heartbeat_sender.main(["renew"]) == 1
    assert "SECRET" not in capsys.readouterr().err


def test_conformance_job_publishes_evidence_after_refreshing(monkeypatch, tmp_path):
    from beadhive import heartbeat_report

    calls = []
    monkeypatch.setattr(
        heartbeat_report, "refresh_conformance", lambda: calls.append("refresh") or 1
    )
    monkeypatch.setattr(heartbeat_sender, "status", lambda: {})
    monkeypatch.setattr(
        heartbeat_report, "publish_session_evidence", lambda: calls.append("evidence")
    )
    assert heartbeat_sender.main(["conformance"]) == 0
    assert calls == ["refresh", "evidence"]


def test_host_list_reports_a_switched_frame_by_its_session_row(frame, monkeypatch, tmp_path):
    from beadhive import host_cli, host_heartbeat

    manifest, _desired = frame
    monkeypatch.setattr(host_cli, "iter_manifests", lambda _root: [(manifest, tmp_path / "h.yaml")])
    monkeypatch.setattr(host_heartbeat, "observe", lambda *a, **k: _session_obs())
    (row,) = host_cli.list_payload(tmp_path, cfg={})
    assert row["liveness_source"] == "session-row"
    assert row["last_seen"] == "2026-10-06T10:00:00+00:00"
    assert row["heartbeat_age_basis"] == "hq-server-session-row" and not row["stale"]
