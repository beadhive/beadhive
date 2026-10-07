"""SQL HQ: fleet default trusted via one operator command, then open admission (bh-taa04.3).

One isolated Dolt HQ. The operator raises the fleet default with the key once (config publish +
signed rebind); a key-less HQ write is not a binding; a brand-new frame is open-admitted with no
grant, enrollment or operator key, registers through the receiver, beats, is eligible, and a
key-less cordon removes it. A frame pinned signed refuses with the actionable message.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from beadhive import hq_authority_enforce as ae
from beadhive import hq_authority_fleet_mode, hq_open_admission
from beadhive.frame_eligibility import EligibilityFacts, eligible
from beadhive.host_heartbeat_core import HeartbeatLease
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_hive_policy import project_hive_policies
from beadhive.hq_seed import apply, plan, prepare
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_sql_operator import SqlRuntimeOperator
from beadhive.hq_sql_receiver import SqlTrustedReceiver
from test_fleet_membership_e2e_int import (
    Clock,
    _Broker,
    _empty_config_server,
    _frame_role,
    _key,
    _request_id,
    _runtime_binding,
    _runtime_roles,
    _runtime_schema,
    _signed_initial_runtime,
    _source_git,
)

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    for name in (ae.MODE_ENV, ae.ENFORCE_ENV, "BH_HQ_OPERATOR_SETTINGS"):
        monkeypatch.delenv(name, raising=False)
    ae.reset_cache()
    yield
    ae.reset_cache()


def _forget():
    path = ae._fleet_state_path()
    if path and os.path.exists(path):
        os.unlink(path)
    ae.reset_cache()


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


def _predicates(frame, policy):
    _head, desired, observation = frame["plane"].read_eligibility(frame["manifest"])
    decision = eligible(frame["manifest"], policy, EligibilityFacts(observation, desired))
    return {name for name, value in decision.predicates if not value}


def test_sql_trusted_fleet_open_admission_lifecycle(tmp_path, monkeypatch):
    clock = Clock()
    git_hq, owner, manifests = _source_git(tmp_path)
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    with _empty_config_server(sql_dir) as (port, _schema_parent, config_settings):
        config_store = SqlFleetConfigRevisionStore(config_settings, broker=_Broker(), clock=clock)

        def fresh_plan():
            return plan(
                hq_dir=git_hq,
                fleet_path=git_hq / "fleet.yaml",
                workspace_sources=(),
                destination=config_store,
            )

        seed_plan = fresh_plan()
        intent = tmp_path / "seed-intent.json"
        prepare(seed_plan, intent)
        seed_receipt = apply(seed_plan, intent, config_store, fresh_plan=fresh_plan)
        config_settings["initial_revision"] = seed_receipt.committed_revision
        snapshot = config_store.load_snapshot()

        _runtime_schema(sql_dir, port)
        _runtime_roles(sql_dir, port)
        operator_key = tmp_path / "operator.key"
        operator_public = _key(operator_key)
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

        # A key-less HQ write raising the default is not a binding: nothing learns it.
        current = config_store.load_snapshot()
        keyless = config_store.publish_snapshot(
            hq_authority_fleet_mode.with_fleet_default(current.documents, "trusted"),
            expected_revision=current.commit_revision,
        )
        _head, state, crossref, policies = operator._operator().load()
        bound, _latest, _snapshot = operator.config_store().authority_binding(
            crossref, policies, expires_at=state["expires_at"]
        )
        assert not bound  # signed frames are fenced, not downgraded
        hq_authority_fleet_mode.refresh(operator)
        assert ae.fleet_default() == "signed" and ae.enforced()
        # Restore the signed default before the operator's command.
        config_store.publish_snapshot(
            hq_authority_fleet_mode.with_fleet_default(keyless.documents, "signed"),
            expected_revision=keyless.commit_revision,
        )

        # Key-less raise refuses; the operator key is needed once.
        with pytest.raises(ControlPlaneError, match="operator key once"):
            hq_authority_fleet_mode.set_fleet_mode(operator, "trusted")
        result = hq_authority_fleet_mode.set_fleet_mode(
            operator, "trusted", operator_key=str(operator_key)
        )
        assert result["published"] and result["rebound"]
        # Another host learns it from the operator-SIGNED binding alone.
        _forget()
        hq_authority_fleet_mode.refresh(operator)
        assert ae.fleet_default() == "trusted" and ae.mode() == "trusted"

        # A brand-new frame: server-local principal provisioning only (no authority step).
        manifest = manifests["a"]
        key = tmp_path / "host-a.key"
        public = _key(key)
        authority, _public, desired, _expected = hq_open_admission.request(
            operator, manifest.host_id, public
        )
        principal = SqlRuntimeOperator.principal_for(authority)
        inbox = _frame_role(sql_dir, port, principal, authority.epoch)
        joined = hq_open_admission.join(operator, manifest.host_id, public)
        assert joined["changed"] and joined["frame_id"] == "frame-a"
        _head, state, _crossref, _policies = operator._operator().load()
        record = state["frames"]["frame-a"]["active"]
        assert record["state"] == "active" and record["desired"]["declared"] is True
        assert record["authority"]["holder_identity"] == "host-a"
        assert record["authority"]["instance_ref"] == "vm-a"
        assert hq_open_admission.join(operator, manifest.host_id, public)["changed"] is False

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
        assert _predicates(frame, policy) == set()

        # A frame pinned signed in this trusted fleet refuses with the actionable message.
        monkeypatch.setenv(ae.MODE_ENV, "signed")
        with pytest.raises(ValueError, match="pins hq.authority_mode: signed"):
            frame["plane"].read_eligibility(manifest)
        monkeypatch.delenv(ae.MODE_ENV)

        # A key-less cordon still removes it.
        plan_ = operator.lifecycle("cordon", "frame-a")
        operator.lifecycle(
            "cordon",
            "frame-a",
            "apply",
            expected=plan_["revision"],
            expected_host_id="host-a",
            expected_release=plan_["release"],
            operator_key="",
            confirm=True,
        )
        assert "not_cordoned" in _predicates(frame, policy)
