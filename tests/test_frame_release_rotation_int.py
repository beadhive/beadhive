"""Reviewed release rotation of an ACTIVE SQL frame on one isolated Dolt HQ.

The frame is granted, registered, admitted and holds the ``bh`` hive lease
through the real operator, receiver and frame adapters. A newly measured
release is then rotated in with ``release_upgrade`` plan/apply/check: the
identity stays the same, the epoch and principal advance, eligibility passes
only on the new digest, and the hive lease keeps its fencing epoch, so claims
minted before the rotation remain valid. A second rotation is the rollback.
"""

from __future__ import annotations

import json

import pytest

from beadhive import hq_authority_guard as authority_guard
from beadhive.host_heartbeat_core import ObservationAuthority
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hosts import HostManifest
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_hive_policy import project_hive_policies
from beadhive.hq_seed import apply, plan, prepare
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_sql_receiver import ReceiverError, SqlTrustedReceiver
from beadhive.modules.config.domain.ports import FleetConfigDocument
from test_fleet_membership_e2e_int import (
    Clock,
    _frame,
    _frame_role,
    _key,
    _liveness_predicates,
    _liveness_transition,
    _request_id,
    _root,
    _runtime_binding,
    _runtime_roles,
    _runtime_schema,
    _signed_beat,
    _signed_initial_runtime,
    _source_git,
)
from test_hq_sql_config_int import _Broker, _empty_config_server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

STABLE = ("frame_id", "holder_identity", "instance_ref", "key_fingerprint", "audience")


def _publish_manifest(config_store, manifest):
    """Publish the canonical host manifest through the typed config CAS."""
    snapshot = config_store.load_snapshot()
    path = f"hosts/{manifest.host_id}.yaml"
    documents = tuple(
        FleetConfigDocument(
            row.path,
            json.dumps(manifest.model_dump(mode="json", exclude_none=True))
            if row.path == path
            else row.content,
        )
        for row in snapshot.documents
    )
    return config_store.publish_snapshot(documents, expected_revision=snapshot.commit_revision)


def _physical_lease(port):
    connection = _root(port, "beadhive_hq_runtime")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT revision,lease_json FROM hq_live_hive_leases WHERE prefix='bh'")
            revision, body = cursor.fetchone()
            return revision, json.loads(body)
    finally:
        connection.close()


def _rotated_frame(previous, sql_dir, port, result):
    """The same identity bound to the reviewed plan's new principal and epoch."""
    authority = ObservationAuthority(**result["authority"])
    inbox = _frame_role(sql_dir, port, result["principal"], result["new_epoch"])
    assert inbox == result["inbox_table"]
    return {
        **previous,
        "authority": authority,
        "principal": result["principal"],
        "inbox": inbox,
        "plane": SqlControlPlane(
            {
                **previous["plane"].settings,
                "runtime": _runtime_binding(sql_dir, port, result["principal"]),
            },
            broker=_Broker(),
            clock=previous["plane"].clock,
        ),
    }


def test_active_frame_rotates_release_keeps_identity_and_hive_lease(tmp_path):
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
        # The operator key and authority writer live only in the operator's plane;
        # the frame's own planes carry neither.
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

        # ---- an admitted active frame that holds the bh hive lease ----
        a = _frame(tmp_path, sql_dir, port, common, runtime_head, owner, manifests["a"], 1, clock)
        old_release = a["manifest"].release.model_dump()
        head = operator.grant(
            a["authority"],
            a["public"],
            {
                "declared": True,
                "release": old_release,
                "caps": a["manifest"].capabilities.model_dump(),
                "profile": "fixture",
            },
            expected=runtime_head,
            operator_key=str(operator_key),
        )
        digest = a["plane"].publish_registration_evidence(a["manifest"], signing_key=str(a["key"]))
        assert receiver.accept_registration(a["principal"], _request_id(port, a["inbox"], digest))
        for sequence in (1, 2, 3):
            digest = _signed_beat(a, owner, clock, sequence, state="pending")
            assert receiver.accept_heartbeat(a["principal"], _request_id(port, a["inbox"], digest))
        admitted = operator.lifecycle(
            "admit",
            "frame-a",
            "apply",
            expected=head,
            expected_host_id="host-a",
            expected_release=old_release["digest"],
            operator_key=str(operator_key),
            confirm=True,
        )
        assert admitted["state"] == "active"
        adopted = HostLease(
            host_id="host-a",
            label="host-a",
            epoch=1,
            adopted_at=now_stamp(clock()),
            expires_at=now_stamp(clock() + 1200),
        )
        proposal, *_ = a["plane"].propose_hive_lease(
            "bh", adopted, expected="", operation="adopt", signing_key=str(a["key"])
        )
        receiver.accept_hive_lease(a["principal"], proposal)
        policy = project_hive_policies(snapshot, valid_until=clock() + 3600, now=clock())["bh"]
        _liveness_transition(a, policy, false=set(), holder=("host-a", 1))
        before = operator.release_upgrade("frame-a", "check")
        assert before["state"] == "active" and not before["upgraded"]

        # ---- step 2: the config publisher prepares the canonical manifest ----
        new_release = {"id": "fixture-2", "digest": "sha256:" + "9" * 64}
        rotated_manifest = HostManifest.model_validate(
            {
                **a["manifest"].model_dump(mode="json", exclude_none=True),
                "release": new_release,
                "state": "active",
            }
        )
        stale_config_head = config_store.load_snapshot().commit_revision
        prepared = _publish_manifest(config_store, rotated_manifest)
        clock.advance(5)

        request = {
            "expected_revision": before["revision"],
            "host_id": "host-a",
            "epoch": 1,
            "old_release": old_release["digest"],
            "config_head": prepared.commit_revision,
            "release": new_release,
            "profile": "fixture",
            "config_revision": "desired-1",
            "expires_at": clock() + 900,
        }

        # ---- step 3: any stale expectation refuses the plan ----
        for field, value in (
            ("expected_revision", runtime_head),
            ("host_id", "host-b"),
            ("epoch", 0),
            ("old_release", "sha256:" + "8" * 64),
            ("config_head", stale_config_head),
            ("expires_at", clock() + 90000),
            ("release", old_release),
        ):
            with pytest.raises(ValueError):
                operator.release_upgrade("frame-a", "plan", request={**request, field: value})
        reviewed = operator.release_upgrade("frame-a", "plan", request=request)
        assert reviewed["rotation"] == "active" and reviewed["state"] == "active"
        assert reviewed["old_epoch"] == 1 and reviewed["new_epoch"] == 2
        assert reviewed["principal"] != a["principal"]

        # ---- step 5: apply needs --confirm, the operator key and the exact digest ----
        for kwargs in (
            {"plan_sha256": reviewed["plan_sha256"], "operator_key": str(operator_key)},
            {"plan_sha256": reviewed["plan_sha256"], "confirm": True},
            {
                "plan_sha256": "sha256:" + "0" * 64,
                "operator_key": str(operator_key),
                "confirm": True,
            },
        ):
            with pytest.raises(ValueError):
                operator.release_upgrade("frame-a", "apply", request=request, **kwargs)
        assert operator.release_upgrade("frame-a", "check")["revision"] == before["revision"]
        # ---- step 4: the server provisions the reviewed new principal and inbox ----
        b = _rotated_frame(a, sql_dir, port, reviewed)
        b["manifest"] = rotated_manifest
        applied = operator.release_upgrade(
            "frame-a",
            "apply",
            request=request,
            plan_sha256=reviewed["plan_sha256"],
            operator_key=str(operator_key),
            confirm=True,
        )
        assert applied["revision"] != before["revision"]
        # A replay of the reviewed request cannot rotate the new incarnation again.
        with pytest.raises(ValueError):
            operator.release_upgrade(
                "frame-a",
                "apply",
                request=request,
                plan_sha256=reviewed["plan_sha256"],
                operator_key=str(operator_key),
                confirm=True,
            )

        # ---- identity unchanged; epoch, release and route advanced ----
        after = operator.release_upgrade("frame-a", "check")
        assert after["state"] == "active" and after["upgraded"]
        assert after["release"] == new_release
        assert after["authority"]["epoch"] == 2
        assert after["authority"]["candidate_expires_at"] is None
        for field in (*STABLE, "beadyard_id"):
            assert after["authority"][field] == before["authority"][field]
        assert [item["record"]["state"] for item in after["history"]] == ["active"]
        assert after["history"][0]["record"]["authority"] == before["authority"]
        assert after["history"][0]["plan_sha256"] == reviewed["plan_sha256"]

        # ---- the old principal and old epoch are refused ----
        with pytest.raises(ControlPlaneError):
            _signed_beat(a, owner, clock, 4)
        with pytest.raises(ControlPlaneError):
            a["plane"].read_eligibility(a["manifest"])

        # ---- in-flight claims: the incumbent lease read still names host-a epoch 1 ----
        incumbent = b["plane"].read_hive_lease_record("bh", incumbent_identity="host-a")[1]
        assert (incumbent.host_id, incumbent.epoch) == ("host-a", 1)
        assert not _liveness_predicates(b, policy)["authenticated_fresh_heartbeat"]

        # ---- an authentic new-epoch beat still carrying the OLD digest is refused ----
        digest = _signed_beat(b, owner, clock, 1, release=old_release)
        assert receiver.accept_heartbeat(b["principal"], _request_id(port, b["inbox"], digest))
        predicates = _liveness_predicates(b, policy)
        assert predicates["authenticated_fresh_heartbeat"]
        assert not predicates["release_matches"]
        lease_revision, _envelope = _physical_lease(port)
        renewal = HostLease(
            host_id="host-a",
            label="host-a",
            epoch=1,
            adopted_at=adopted.adopted_at,
            expires_at=now_stamp(clock() + 1200),
        )
        proposal, *_ = b["plane"].propose_hive_lease(
            "bh", renewal, expected=lease_revision, operation="renew", signing_key=str(b["key"])
        )
        with pytest.raises(ReceiverError):
            receiver.accept_hive_lease(b["principal"], proposal)

        # ---- eligibility passes on the new digest; the lease renews at the SAME epoch ----
        clock.advance(5)
        digest = _signed_beat(b, owner, clock, 2)
        assert receiver.accept_heartbeat(b["principal"], _request_id(port, b["inbox"], digest))
        _liveness_transition(b, policy, false=set(), holder=("host-a", 1))
        proposal, *_ = b["plane"].propose_hive_lease(
            "bh", renewal, expected=lease_revision, operation="renew", signing_key=str(b["key"])
        )
        receiver.accept_hive_lease(b["principal"], proposal)
        _revision, envelope = _physical_lease(port)
        assert envelope["authority"] == {"frame_id": "frame-a", **after["authority"]}
        assert envelope["lease"]["epoch"] == 1  # claim fencing token unchanged
        assert envelope["lease"]["adopted_at"] == adopted.adopted_at
        _liveness_transition(b, policy, false=set(), holder=("host-a", 1))

        # ---- rollback is another reviewed rotation, forward to a new epoch ----
        restored = _publish_manifest(
            config_store, rotated_manifest.model_copy(update={"release": a["manifest"].release})
        )
        clock.advance(5)
        back = {
            **request,
            "expected_revision": after["revision"],
            "epoch": 2,
            "old_release": new_release["digest"],
            "config_head": restored.commit_revision,
            "release": old_release,
            "expires_at": clock() + 900,
        }
        rollback = operator.release_upgrade("frame-a", "plan", request=back)
        assert rollback["new_epoch"] == 3
        c = _rotated_frame(b, sql_dir, port, rollback)
        c["manifest"] = rotated_manifest.model_copy(update={"release": a["manifest"].release})
        operator.release_upgrade(
            "frame-a",
            "apply",
            request=back,
            plan_sha256=rollback["plan_sha256"],
            operator_key=str(operator_key),
            confirm=True,
        )
        final = operator.release_upgrade("frame-a", "check")
        assert final["release"] == old_release and final["authority"]["epoch"] == 3
        assert [item["record"]["authority"]["epoch"] for item in final["history"]] == [1, 2]
        digest = _signed_beat(c, owner, clock, 1)
        assert receiver.accept_heartbeat(c["principal"], _request_id(port, c["inbox"], digest))
        _liveness_transition(c, policy, false=set(), holder=("host-a", 1))
        with pytest.raises(ControlPlaneError):
            _signed_beat(b, owner, clock, 3)
        connection = _root(port, "beadhive_hq_runtime")
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT epoch FROM hq_principal_registry WHERE frame_id='frame-a' "
                    "ORDER BY epoch"
                )
                # Every incarnation's route survives; archives authorize none of them.
                assert [row[0] for row in cursor.fetchall()] == [1, 2, 3]
                cursor.execute("SELECT state_json FROM hq_authority")
                state = json.loads(cursor.fetchone()[0])
        finally:
            connection.close()
        authority_guard.validate_state(state)
        assert len(list(authority_guard.records(state))) == 1
