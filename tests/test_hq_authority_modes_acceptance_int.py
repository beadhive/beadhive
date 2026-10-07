"""Authority-mode acceptance (bh-arbpg, epic bh-taa04): the 0.24.0 goal as composed scenarios.

Simulated clock, no real waits. Legs:

1. signed: one rebind, 30 d with an unrelated config edit and a cordon/resume, eligible
   throughout and silent about expiry; a ``frame_policy`` edit fences until a rebind (SQL HQ;
   the no-action 30 d part also on the real SSH-signed Git HQ).
2. trusted: no operator key anywhere (the private key is deleted after seeding); key-less grant,
   cordon, resume and fleet-config edit; the frame stays eligible and honours the cordon.
3. mixed: the unsigned record a trusted operator wrote is refused by a signed frame with the
   actionable message while a trusted frame accepts it.
4. fleet default: one operator command makes the fleet trusted, a new frame joins with no grant
   or key and works; a key-less downgrade attempt on a signed fleet does not take effect.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.test_hq_authority_backend import _prepared_hqs, apply, backend  # noqa: F401
from tests.test_hq_laptop_off_acceptance_int import (
    BYPASS_FLEET,
    CONFIG_SCHEMA,
    FLEET,
    GRANTS,
    POLICY_FLEET,
    RELEASE,
    Broker,
    SimClock,
    _binding,
    _cli,
    _key,
    _seed_authority,
    _seed_config,
    _start_server,
)

from beadhive import hq_authority_ceiling as ceiling_mod
from beadhive import hq_authority_enforce as ae
from beadhive import hq_authority_expiry, hq_open_admission
from beadhive import hq_authority_fleet_mode as fleet_mode
from beadhive.frame_eligibility import EligibilityFacts, eligible
from beadhive.host_heartbeat_core import HeartbeatLease, ObservationAuthority
from beadhive.hosts import HostManifest
from beadhive.hq_authority_ceiling import AUTHORITY_NO_EXPIRY
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_hive_policy import project_hive_policies
from beadhive.hq_seed import apply as seed_apply
from beadhive.hq_seed import plan as seed_plan
from beadhive.hq_seed import prepare as seed_prepare
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_sql_operator import SqlRuntimeOperator
from beadhive.hq_sql_receiver import SqlTrustedReceiver
from beadhive.hq_sql_runtime_schema import COMMITTED_SCHEMA, PROTECTED_LIVE_SCHEMA, inbox_ddl
from beadhive.hq_sql_signatures import fingerprint
from beadhive.modules.config.domain.ports import FleetConfigDocument
from harness.world import dolt_server_slot
from test_fleet_membership_e2e_int import (
    Clock,
    _Broker,
    _empty_config_server,
    _frame_role,
    _request_id,
    _runtime_binding,
    _runtime_roles,
    _runtime_schema,
    _signed_initial_runtime,
    _source_git,
)
from test_fleet_membership_e2e_int import _key as fleet_key

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

HOUR = 3600.0
DAY = 24 * HOUR


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    for name in (ae.MODE_ENV, ae.ENFORCE_ENV, "BH_HQ_OPERATOR_SETTINGS", ceiling_mod.ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    ae.reset_cache()
    yield
    ae.reset_cache()


def _manifest():
    return HostManifest.model_validate(
        {
            "frame_id": "frame-1",
            "host_id": "host-1",
            "instance_ref": "vm-1",
            "state": "active",
            "label": "fixture",
            "os": "linux",
            "arch": "x86_64",
            "role": "executor",
            "identity": {"kind": "none", "value": ""},
            "release": RELEASE,
            "capabilities": {
                "isolation": "container",
                "trust_zone": "self-hosted",
                "arch": "x86_64",
                "harnesses": ["claude"],
                "max_sessions": 1,
            },
        }
    )


@contextmanager
def sql_world(tmp_path, test_id):
    """One SQL HQ, one frame, a signed seed, a simulated clock; yields a namespace of drivers."""
    clock = SimClock(time.time())
    operator_key, frame_key = tmp_path / "operator-key", tmp_path / "frame-key"
    operator_public, frame_public = _key(operator_key), _key(frame_key)
    manifest = _manifest()
    host = FleetConfigDocument(
        "hosts/host-1.yaml", json.dumps(manifest.model_dump(mode="json", exclude_none=True))
    )

    def documents(fleet):
        return (FleetConfigDocument("fleet.yaml", fleet), host)

    with dolt_server_slot(test_id=test_id):
        server, port = _start_server(tmp_path)
        try:
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_runtime")
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(COMMITTED_SCHEMA))
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(PROTECTED_LIVE_SCHEMA))
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + inbox_ddl("frame_a", 1))
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_config")
            _cli(tmp_path, port, CONFIG_SCHEMA)
            config_head = _seed_config(port, documents(FLEET))
            initial = _seed_authority(
                port,
                config_head=config_head,
                documents=documents(FLEET),
                manifest=manifest,
                frame_public=frame_public,
                operator_key=operator_key,
                now=clock(),
            )
            _cli(tmp_path, port, GRANTS)
            settings = {
                "enabled": True,
                "liveness": "signed",
                "reader": _binding(port, "config_reader", "beadhive_hq_config"),
                "runtime": _binding(port, "frame_a", "beadhive_hq_runtime"),
                "floor_path": str(tmp_path / "config-floor.json"),
                "backend_identity": "config-backend",
                "generation": "config-generation",
                "initial_revision": config_head,
                "minimum_sequence": 1,
                "cache_ttl": 30,
                "runtime_floor_path": str(tmp_path / "runtime-floor.json"),
                "runtime_backend_identity": "runtime-backend",
                "runtime_generation": "runtime-generation",
                "runtime_initial_revision": initial,
                "runtime_operator_public_key": operator_public,
            }
            frame = SqlControlPlane(settings, broker=Broker(), clock=clock)
            operator = SqlControlPlane(
                {
                    **settings,
                    "runtime": None,
                    "authority_writer": _binding(port, "authority_writer", "beadhive_hq_runtime"),
                },
                broker=Broker(),
                clock=clock,
            )
            publisher = SqlFleetConfigRevisionStore(
                {
                    **settings,
                    "runtime": None,
                    "publisher": _binding(port, "config_publisher", "beadhive_hq_config"),
                },
                broker=Broker(),
                clock=clock,
            )
            seq = iter(range(1, 10_000))

            def decision():
                frame.heartbeat(
                    HeartbeatLease(
                        audience="fixture-fleet",
                        frame_id="frame-1",
                        holderIdentity="host-1",
                        instance_ref="vm-1",
                        key_id=fingerprint(frame_public),
                        epoch=1,
                        config_revision="desired-1",
                        seq=next(seq),
                        renewTime=datetime.fromtimestamp(clock(), UTC).isoformat(),
                        state_seen="active",
                        release=RELEASE,
                        report_digest="sha256:" + "2" * 64,
                        conformance={"profile": "fixture", "status": "conformant", "checks": []},
                    ),
                    signing_key=str(frame_key),
                )
                _head, desired, observation = frame.read_eligibility(manifest)
                _rev, _state, _crossref, policies = frame._runtime_authority().load_state()
                return desired, eligible(
                    manifest, policies["bh"], EligibilityFacts(observation, desired, at=clock())
                )

            def lifecycle(verb, revision, key=""):
                return operator.lifecycle(
                    verb,
                    "frame-1",
                    "apply",
                    expected=revision,
                    expected_host_id="host-1",
                    operator_key=key,
                    confirm=True,
                )["revision"]

            def authority():
                revision, state, _crossref, policies = operator._operator().load()
                return revision, state, policies

            def stored_signature():
                return _cli(
                    tmp_path,
                    port,
                    "SELECT CAST(operator_signature AS CHAR) FROM beadhive_hq_runtime.hq_authority",
                )

            yield SimpleNamespace(
                clock=clock,
                port=port,
                tmp=tmp_path,
                operator_key=operator_key,
                frame_public=frame_public,
                manifest=manifest,
                documents=documents,
                config_head=config_head,
                initial=initial,
                frame=frame,
                operator=operator,
                publisher=publisher,
                decision=decision,
                lifecycle=lifecycle,
                authority=authority,
                stored_signature=stored_signature,
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def _healthy_and_quiet(world, capsys, step):
    """Eligible, not cordoned, and every expiry surface is silent or says 'never'."""
    desired, verdict = world.decision()
    assert verdict.allowed, (step, verdict)
    assert not desired["cordoned"], step
    status = hq_authority_expiry.authority_status(world.frame)
    assert status["expires_never"] and status["expires_in"] == "never", step
    assert not status["expiring_soon"] and status["config_bound"], step
    ok, _status, message = hq_authority_expiry.check(world.operator, min_remaining=3 * DAY)
    assert ok and message == "OK: HQ authority does not expire", (step, message)
    hq_authority_expiry._reset_for_tests()
    assert hq_authority_expiry.warn_if_expiring(world.frame) is None, step
    assert hq_authority_expiry.warn_if_expiring(world.operator) is None, step
    captured = capsys.readouterr()
    assert "WARN" not in captured.out + captured.err, step


# ---- leg 1: signed, no operator action for 30 days ---------------------------------------------


def test_leg1_signed_authority_30_days_sql(tmp_path, capsys):
    with sql_world(tmp_path, "hq-modes-leg1-sql") as w:
        t0 = w.clock.now
        key = str(w.operator_key)
        # The operator's one action: a rebind with no --duration signs the no-expiry sentinel.
        w.operator.renew(expected=w.initial, operator_key=key)
        revision, state, _policies = w.authority()
        assert state["expires_at"] == AUTHORITY_NO_EXPIRY
        _healthy_and_quiet(w, capsys, "t0")

        # t+1 d: an unrelated fleet-config edit does not fence; cordon/resume keep the sentinel.
        w.clock.now = t0 + DAY
        bypass = w.publisher.publish_snapshot(
            w.documents(BYPASS_FLEET), expected_revision=w.config_head
        )
        _healthy_and_quiet(w, capsys, "t+1d after unrelated config edit")
        assert w.authority()[:2] == (revision, state)
        revision = w.lifecycle("cordon", revision, key)
        desired, verdict = w.decision()
        assert desired["cordoned"] and not verdict.allowed
        revision = w.lifecycle("resume", revision, key)
        assert w.authority()[1]["expires_at"] == AUTHORITY_NO_EXPIRY
        _healthy_and_quiet(w, capsys, "t+1d after cordon/resume")

        # 30 days of nothing: eligible and silent at every sample.
        for days in (2, 7, 14, 21, 30):
            w.clock.now = t0 + days * DAY
            _healthy_and_quiet(w, capsys, f"t+{days}d")
        assert w.authority()[:2] == (revision, w.authority()[1])

        # The negative leg: a frame-enforced edit fences until a rebind re-signs the head.
        w.publisher.publish_snapshot(
            w.documents(POLICY_FLEET), expected_revision=bypass.commit_revision
        )
        with pytest.raises(ControlPlaneError):
            w.decision()
        ok, status, message = hq_authority_expiry.check(w.operator, min_remaining=3 * DAY)
        assert not ok and status["config_bound"] is False
        assert message == "FAIL: authority is not bound to the latest config head"
        w.operator.renew(expected=revision, operator_key=key)
        assert w.authority()[1]["expires_at"] == AUTHORITY_NO_EXPIRY
        _healthy_and_quiet(w, capsys, "after rebind of the policy edit")


def test_leg1_signed_authority_30_days_git(backend, capsys):  # noqa: F811
    b = backend
    plane, key = b["plane"], str(b["operator"])
    real = plane.clock
    plane.renew(expected=plane._read()[0], operator_key=key)  # no --duration
    for sequence in (1, 2, 3):
        b["accept"](sequence)
    assert apply(b, "admit")["state"] == "active"
    apply(b, "cordon")
    assert not plane.watch_state("frame-one").eligible
    assert apply(b, "resume")["state"] == "active"
    sha, state, _ = plane._read()
    assert state["expires_at"] == AUTHORITY_NO_EXPIRY
    try:
        for days in (1, 7, 30):
            plane.clock = lambda days=days: real() + days * DAY
            assert plane._read()[0] == sha  # no operator action, still readable and signed
            assert plane.watch_state("frame-one").eligible, days
            status = hq_authority_expiry.authority_status(plane)
            assert status["expires_never"] and not status["expiring_soon"], days
            hq_authority_expiry._reset_for_tests()
            assert hq_authority_expiry.warn_if_expiring(plane) is None
    finally:
        plane.clock = real
    captured = capsys.readouterr()
    assert "WARN" not in captured.out + captured.err


# ---- legs 2 and 3: trusted without any operator key, then the mixed-mode refusal ---------------


def test_leg2_trusted_keyless_and_leg3_mixed_mode_sql(tmp_path, monkeypatch, capsys):
    with sql_world(tmp_path, "hq-modes-leg2-sql") as w:
        # No operator key anywhere: the only private key is destroyed after the seed.
        os.unlink(w.operator_key)
        monkeypatch.setenv(ae.MODE_ENV, "trusted")

        def signed_frame_refuses():
            """Leg 3: the unsigned record is refused by a signed frame, with the way out."""
            assert ae.UNSIGNED_SIGNATURE in w.stored_signature()
            monkeypatch.delenv(ae.MODE_ENV)
            with pytest.raises(ValueError, match="UNSIGNED.*hq.authority_mode: trusted"):
                w.frame._runtime_authority().load_state()
            with pytest.raises(ControlPlaneError):
                w.decision()
            monkeypatch.setenv(ae.MODE_ENV, "trusted")

        head = w.operator.renew(expected=w.initial, operator_key="")  # key-less rebind
        assert w.decision()[1].allowed
        signed_frame_refuses()

        # Key-less cordon/resume: the trusted frame honours both.
        w.clock.now += HOUR
        head = w.lifecycle("cordon", head)
        desired, verdict = w.decision()
        assert desired["cordoned"] and not verdict.allowed
        head = w.lifecycle("resume", head)
        desired, verdict = w.decision()
        assert not desired["cordoned"] and verdict.allowed

        # Key-less grant of a second incarnation: recorded pending, the first frame unaffected.
        second_key = w.tmp / "second-key"
        second_public = _key(second_key)
        second = ObservationAuthority(
            "frame-2",
            "host-2",
            "vm-2",
            fingerprint(second_public),
            1,
            "fixture-fleet",
            "desired-1",
            w.clock() + 1800,
        )
        principal = SqlRuntimeOperator.principal_for(second)
        _cli(w.tmp, w.port, "USE beadhive_hq_runtime; " + inbox_ddl(principal, 1))
        head = w.operator.grant(
            second,
            second_public,
            {
                "declared": True,
                "release": RELEASE,
                "caps": w.manifest.capabilities.model_dump(),
                "profile": "fixture",
            },
            expected=head,
            operator_key="",
        )
        _rev, state, _crossref, _policies = w.operator._operator().load()
        assert state["frames"]["frame-2"]["candidate"]["state"] == "pending"

        # Key-less fleet-config edit that a signed frame would fence on: trusted stays eligible.
        w.clock.now += HOUR
        w.publisher.publish_snapshot(w.documents(POLICY_FLEET), expected_revision=w.config_head)
        desired, verdict = w.decision()
        assert verdict.allowed and not desired["cordoned"]
        # ... and still honours a cordon issued after the edit.
        head = w.lifecycle("cordon", head)
        assert w.decision()[0]["cordoned"] and not w.decision()[1].allowed
        head = w.lifecycle("resume", head)
        assert w.decision()[1].allowed

        # Long key-less idle: still eligible, no expiry noise, expiry reported as not enforced.
        for days in (8, 30):
            w.clock.now += days * DAY
            assert w.decision()[1].allowed, days
        ok, status, message = hq_authority_expiry.check(w.operator, min_remaining=3 * DAY)
        assert ok and status["mode"] == "trusted"
        hq_authority_expiry._reset_for_tests()
        assert hq_authority_expiry.warn_if_expiring(w.frame) is None
        assert "WARN" not in "".join(capsys.readouterr())
        signed_frame_refuses()

        # A key-less mutation outside trusted mode still refuses.
        monkeypatch.delenv(ae.MODE_ENV)
        with pytest.raises(ControlPlaneError, match="requires separate --operator-key"):
            w.operator.renew(expected=head, operator_key="")


# ---- leg 4: fleet default via one operator command, open admission, no downgrade ----------------


def _beat(frame, clock, sequence, authority, desired):
    manifest = frame["manifest"]
    beat = HeartbeatLease(
        domain="beadhive/frame-heartbeat/v2",
        beadyard_id=authority.beadyard_id,
        audience=authority.audience,
        frame_id=manifest.frame_id,
        holderIdentity=manifest.host_id,
        instance_ref=manifest.instance_ref,
        key_id=authority.key_fingerprint,
        epoch=authority.epoch,
        config_revision=authority.config_revision,
        seq=sequence,
        renewTime=datetime.fromtimestamp(clock(), UTC).isoformat(),
        state_seen="active",
        release=manifest.release.model_dump(),
        report_digest="sha256:" + "2" * 64,
        conformance={
            "profile": desired["profile"],
            "status": "conformant",
            "checks": [{"id": "required", "status": "pass"}],
        },
    )
    return frame["plane"].heartbeat(beat, signing_key=str(frame["key"]))


def _failing_predicates(frame, policy):
    _head, desired, observation = frame["plane"].read_eligibility(frame["manifest"])
    decision = eligible(frame["manifest"], policy, EligibilityFacts(observation, desired))
    return {name for name, value in decision.predicates if not value}


def test_leg4_fleet_default_trusted_open_admission_and_no_keyless_downgrade_sql(
    tmp_path, monkeypatch
):
    clock = Clock()
    git_hq, owner, manifests = _source_git(tmp_path)
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    with _empty_config_server(sql_dir) as (port, _schema_parent, config_settings):
        config_store = SqlFleetConfigRevisionStore(config_settings, broker=_Broker(), clock=clock)

        def fresh_plan():
            return seed_plan(
                hq_dir=git_hq,
                fleet_path=git_hq / "fleet.yaml",
                workspace_sources=(),
                destination=config_store,
            )

        first_plan = fresh_plan()
        intent = tmp_path / "seed-intent.json"
        seed_prepare(first_plan, intent)
        receipt = seed_apply(first_plan, intent, config_store, fresh_plan=fresh_plan)
        config_settings["initial_revision"] = receipt.committed_revision
        snapshot = config_store.load_snapshot()
        _runtime_schema(sql_dir, port)
        _runtime_roles(sql_dir, port)
        operator_key = tmp_path / "operator.key"
        operator_public = fleet_key(operator_key)
        runtime_head = _signed_initial_runtime(
            port, snapshot, owner, operator_key, operator_public, clock
        )
        common = {
            **config_settings,
            "enabled": True,
            "runtime_floor_path": str(tmp_path / "runtime-floor.json"),
            "runtime_backend_identity": "fixture-runtime-backend",
            "runtime_generation": "fixture-runtime-generation",
            "runtime_initial_revision": runtime_head,
            "runtime_operator_public_key": operator_public,
        }
        operator = SqlControlPlane(
            {
                **common,
                "runtime": None,
                "authority_writer": _runtime_binding(sql_dir, port, "authority_writer"),
            },
            broker=_Broker(),
            clock=clock,
        )
        receiver = SqlTrustedReceiver(
            {**common, "runtime": None, "observer": _runtime_binding(sql_dir, port, "observer")},
            broker=_Broker(),
            clock=clock,
        )
        manifest = manifests["a"]
        key = tmp_path / "host-a.key"
        public = fleet_key(key)

        # Signed fleet: a key-less write of the trusted default is not a binding and is not
        # learned; the key-less command refuses; open admission refuses; key-less rebind refuses.
        current = config_store.load_snapshot()
        keyless = config_store.publish_snapshot(
            fleet_mode.with_fleet_default(current.documents, "trusted"),
            expected_revision=current.commit_revision,
        )
        fleet_mode.refresh(operator)
        assert ae.fleet_default() == "signed" and ae.enforced() and not ae.trusted()
        with pytest.raises(ControlPlaneError, match="operator key once"):
            fleet_mode.set_fleet_mode(operator, "trusted")
        with pytest.raises(ControlPlaneError):
            hq_open_admission.join(operator, manifest.host_id, public)
        with pytest.raises(ControlPlaneError, match="requires separate --operator-key"):
            operator.renew(expected=operator._operator().load()[0], operator_key="")
        assert ae.fleet_default() == "signed" and not ae.trusted()
        config_store.publish_snapshot(
            fleet_mode.with_fleet_default(keyless.documents, "signed"),
            expected_revision=keyless.commit_revision,
        )

        # One operator command (with the key, once) makes the fleet trusted ...
        result = fleet_mode.set_fleet_mode(operator, "trusted", operator_key=str(operator_key))
        assert result["published"] and result["rebound"]
        ae.reset_cache()
        assert ae.fleet_default() == "trusted" and ae.mode() == "trusted"

        # ... and a brand-new frame joins with no grant, enrollment or operator key, and works.
        authority, _public, desired, _expected = hq_open_admission.request(
            operator, manifest.host_id, public
        )
        principal = SqlRuntimeOperator.principal_for(authority)
        inbox = _frame_role(sql_dir, port, principal, authority.epoch)
        joined = hq_open_admission.join(operator, manifest.host_id, public)
        assert joined["changed"] and joined["admitted"]
        frame = {
            "key": key,
            "manifest": manifest,
            "plane": SqlControlPlane(
                {
                    **common,
                    "runtime": _runtime_binding(sql_dir, port, principal),
                    "runtime_floor_path": str(tmp_path / "floor-host-a.json"),
                },
                broker=_Broker(),
                clock=clock,
            ),
        }
        digest = frame["plane"].publish_registration_evidence(manifest, signing_key=str(key))
        assert receiver.accept_registration(principal, _request_id(port, inbox, digest)) == digest
        for sequence in (1, 2, 3):
            digest = _beat(frame, clock, sequence, authority, desired)
            assert receiver.accept_heartbeat(principal, _request_id(port, inbox, digest))
        policy = project_hive_policies(snapshot, valid_until=clock() + 3600, now=clock())["bh"]
        assert _failing_predicates(frame, policy) == set()

        # A frame pinned signed in a trusted fleet refuses with the actionable message.
        monkeypatch.setenv(ae.MODE_ENV, "signed")
        with pytest.raises(ValueError, match="pins hq.authority_mode: signed"):
            frame["plane"].read_eligibility(manifest)
        monkeypatch.delenv(ae.MODE_ENV)

        # A key-less cordon still removes the new frame.
        plan = operator.lifecycle("cordon", "frame-a")
        operator.lifecycle(
            "cordon",
            "frame-a",
            "apply",
            expected=plan["revision"],
            expected_host_id="host-a",
            expected_release=plan["release"],
            operator_key="",
            confirm=True,
        )
        assert "not_cordoned" in _failing_predicates(frame, policy)
