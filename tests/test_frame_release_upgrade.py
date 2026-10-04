"""Identity-preserving release upgrades create new pending evidence custody, never admission."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import frame_release_upgrade as upgrade
from beadhive import hq_authority_guard as guard
from beadhive.beadyard_identity import new_document, parse_document
from beadhive.host_heartbeat_core import ObservationAuthority
from beadhive.hq_sql_operator import SqlRuntimeOperator
from beadhive.hq_sql_signatures import fingerprint, sign_authority, verify_authority
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot


@pytest.fixture
def fixture(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    key = tmp_path / "operator"
    key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    identity = new_document()
    owner = parse_document(identity)
    authority = {
        "frame_id": "frame",
        "holder_identity": str(uuid4()),
        "instance_ref": "vm",
        "key_fingerprint": fingerprint(public),
        "epoch": 3,
        "audience": "fleet",
        "config_revision": "policy-v1",
        "candidate_expires_at": 1500,
        "beadyard_id": owner,
    }
    caps = {
        "isolation": "container",
        "trust_zone": "self-hosted",
        "arch": "arm64",
        "harnesses": ["codex"],
        "max_sessions": 1,
    }
    old_release = {"id": "0.21.1", "digest": "sha256:" + "1" * 64}
    new_release = {"id": "0.21.3", "digest": "sha256:" + "3" * 64}
    record = {
        "authority": authority,
        "public_key": public,
        "state": "pending",
        "desired": {"declared": True, "release": old_release, "caps": caps, "profile": "old"},
        "cordoned": False,
        "drain_deadline": None,
        "receipt": {
            "sequence": 0,
            "sha": "",
            "first_seen": None,
            "consecutive": 0,
            "lease": None,
            "registration": None,
        },
    }
    state = {
        "domain": guard.DOMAIN_V2,
        "generation": "runtime-generation",
        "revision": 4,
        "issued_at": 900,
        "expires_at": 1800,
        "frames": {"frame": {"active": None, "candidate": record, "retired": [], "epoch_floor": 3}},
    }
    manifest = {
        "host_id": authority["holder_identity"],
        "frame_id": "frame",
        "instance_ref": "vm",
        "beadyard_id": owner,
        "state": "pending",
        "release": new_release,
        "capabilities": caps,
        "os": "darwin",
        "arch": "arm64",
        "label": "fixture",
        "role": "executor",
        "identity": {"kind": "none"},
    }
    snapshot = FleetConfigSnapshot(
        "config-backend",
        "c" * 32,
        "config-generation",
        900,
        1800,
        (
            FleetConfigDocument("beadyard.json", identity),
            FleetConfigDocument(f"hosts/{authority['holder_identity']}.yaml", json.dumps(manifest)),
            FleetConfigDocument("fleet.yaml", "schema_version: 1\nmanaged_repos: []\n"),
        ),
    )
    request = {
        "expected_revision": "runtime-head",
        "host_id": authority["holder_identity"],
        "epoch": 3,
        "old_release": old_release["digest"],
        "config_head": "c" * 32,
        "release": new_release,
        "profile": "new",
        "config_revision": "policy-v2",
        "expires_at": 1600,
    }
    return SimpleNamespace(
        state=state,
        record=record,
        request=request,
        snapshot=snapshot,
        manifest=manifest,
        key=key,
        public=public,
    )


def prepare(fixture):
    return upgrade.prepare(
        fixture.state, "frame", "runtime-head", fixture.snapshot, fixture.request, now=1000
    )


def test_rotation_preserves_identity_history_and_resets_evidence(fixture):
    old = copy.deepcopy(fixture.state)
    state, digest = prepare(fixture)
    record = state["frames"]["frame"]["candidate"]
    for field in (
        "frame_id",
        "holder_identity",
        "instance_ref",
        "beadyard_id",
        "key_fingerprint",
        "audience",
    ):
        assert record["authority"][field] == fixture.record["authority"][field]
    assert state["frames"]["frame"]["epoch_floor"] == record["authority"]["epoch"] == 4
    assert record["desired"]["release"] == fixture.request["release"]
    assert record["state"] == "pending"
    assert record["receipt"]["sequence"] == record["receipt"]["consecutive"] == 0
    assert record["release_upgrade_history"][0]["record"] == fixture.record
    assert fixture.state == old
    guard.validate_state(state)
    assert len(list(guard.records(state))) == 1  # Archived signer is never a live incarnation.
    new_route = upgrade.route_for(state, "frame")
    old_principal = SqlRuntimeOperator.principal_for(
        ObservationAuthority(**fixture.record["authority"])
    )
    assert new_route[0] != old_principal
    upgrade.validate_publication(
        fixture.state,
        state,
        "runtime-head",
        fixture.snapshot,
        {"frame": "frame", "request": fixture.request, "plan_sha256": digest},
        new_route,
        now=1000,
    )
    signed = sign_authority(state, signing_key=str(fixture.key))
    verify_authority(state, signed, granted_public_key=fixture.public)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_revision", "stale"),
        ("host_id", "foreign"),
        ("epoch", 2),
        ("epoch", True),
        ("old_release", "sha256:" + "0" * 64),
        ("config_head", "stale-config"),
        ("expires_at", 1000),
        ("expires_at", float("inf")),
        ("expires_at", 90000),
        ("profile", ""),
        ("config_revision", ""),
        ("release", {"id": "same", "digest": "sha256:" + "1" * 64}),
    ],
)
def test_reviewed_cas_and_bounded_target_fail_closed(fixture, field, value):
    fixture.request[field] = value
    with pytest.raises(ValueError):
        prepare(fixture)


@pytest.mark.parametrize(
    "field,value",
    [
        ("beadyard_id", "00000000-0000-4000-8000-000000000001"),
        ("host_id", "another-host"),
        ("instance_ref", "another-vm"),
        ("frame_id", "another-frame"),
        ("state", "active"),
        ("release", {"id": "foreign", "digest": "sha256:" + "f" * 64}),
        (
            "capabilities",
            {
                "isolation": "kvm",
                "trust_zone": "self-hosted",
                "arch": "arm64",
                "harnesses": ["codex"],
                "max_sessions": 1,
            },
        ),
    ],
)
def test_prepared_manifest_must_preserve_identity_and_caps(fixture, field, value):
    from dataclasses import replace

    fixture.manifest[field] = value
    document = fixture.snapshot.documents[1]
    fixture.snapshot = replace(
        fixture.snapshot,
        documents=(
            fixture.snapshot.documents[0],
            FleetConfigDocument(document.path, json.dumps(fixture.manifest)),
        ),
    )
    with pytest.raises(ValueError):
        prepare(fixture)


def test_active_quarantined_expired_authority_refused(fixture):
    for state_name in ("active", "quarantined"):
        fixture.record["state"] = state_name
        if state_name == "active":
            entry = fixture.state["frames"]["frame"]
            entry["active"], entry["candidate"] = fixture.record, None
        with pytest.raises(ValueError):
            prepare(fixture)
    fixture.state["frames"]["frame"].update(active=None, candidate=fixture.record)
    fixture.record["state"] = "pending"
    fixture.state["expires_at"] = 1000
    with pytest.raises(ValueError, match="freshness"):
        prepare(fixture)


@pytest.mark.parametrize(
    "mutation", ["archive", "epoch", "receipt", "route", "digest", "config", "expiry"]
)
def test_transaction_rechecks_plan_and_exact_transition(fixture, mutation):
    from dataclasses import replace

    state, digest = prepare(fixture)
    route = upgrade.route_for(state, "frame")
    now = 1000
    if mutation == "archive":
        state["frames"]["frame"]["candidate"]["release_upgrade_history"][0]["record"]["desired"][
            "profile"
        ] = "tampered"
    elif mutation == "epoch":
        state["frames"]["frame"]["candidate"]["authority"]["epoch"] = 9
    elif mutation == "receipt":
        state["frames"]["frame"]["candidate"]["receipt"]["consecutive"] = 3
    elif mutation == "route":
        route = ("wrong-principal", *route[1:])
    elif mutation == "digest":
        digest = "sha256:" + "0" * 64
    elif mutation == "config":
        fixture.snapshot = replace(fixture.snapshot, commit_revision="changed-head")
    else:
        now = 1800
    with pytest.raises(ValueError):
        upgrade.validate_publication(
            fixture.state,
            state,
            "runtime-head",
            fixture.snapshot,
            {"frame": "frame", "request": fixture.request, "plan_sha256": digest},
            route,
            now=now,
        )


def test_generic_publications_cannot_add_or_rewrite_archives(fixture):
    state, _digest = prepare(fixture)
    with pytest.raises(ValueError, match="explicit reviewed"):
        upgrade.preserve_history(fixture.state, state)
    unchanged = copy.deepcopy(state)
    upgrade.preserve_history(state, unchanged)
    unchanged["frames"]["frame"]["candidate"]["release_upgrade_history"][0]["record"]["receipt"][
        "sha"
    ] = "rewrite"
    with pytest.raises(ValueError):
        upgrade.preserve_history(state, unchanged)


@pytest.mark.parametrize(
    "mutation", ["recursive", "duplicate-epoch", "foreign-signer", "unbounded"]
)
def test_archive_schema_rejects_forged_lineage(fixture, mutation):
    state, _digest = prepare(fixture)
    history = state["frames"]["frame"]["candidate"]["release_upgrade_history"]
    if mutation == "recursive":
        history[0]["record"]["release_upgrade_history"] = []
    elif mutation == "duplicate-epoch":
        history.append(copy.deepcopy(history[0]))
    elif mutation == "foreign-signer":
        history[0]["record"]["authority"]["holder_identity"] = "other"
    else:
        history.extend(copy.deepcopy(history[0]) for _ in range(16))
    with pytest.raises(ValueError):
        guard.validate_state(state)


def test_apply_requires_reviewed_plan_and_replay_cannot_rotate_again(fixture):
    published = []

    class Operator:
        def load(self, **kwargs):
            return "runtime-head", fixture.state, (), {}

        def publish(self, state, **kwargs):
            upgrade.validate_publication(
                fixture.state,
                state,
                "runtime-head",
                fixture.snapshot,
                kwargs["release_upgrade"],
                kwargs["provisioned_route"],
                now=1000,
            )
            published.append(kwargs)
            fixture.state = state
            return "next-runtime-head"

    operator = Operator()
    plane = SimpleNamespace(
        _operator=lambda: operator,
        _operator_deadline=lambda: 100000000000,
        config_store=lambda: SimpleNamespace(load_snapshot=lambda: fixture.snapshot),
        clock=lambda: 1000,
    )
    plan = upgrade.release_upgrade(plane, "frame", request=fixture.request)
    assert not published
    for kwargs in (
        {},
        {"confirm": True},
        {"operator_key": "key", "confirm": True},
        {"operator_key": "key", "confirm": True, "plan_sha256": "wrong"},
    ):
        with pytest.raises(ValueError):
            upgrade.release_upgrade(plane, "frame", "apply", request=fixture.request, **kwargs)
    assert not published
    result = upgrade.release_upgrade(
        plane,
        "frame",
        "apply",
        request=fixture.request,
        operator_key="key",
        confirm=True,
        plan_sha256=plan["plan_sha256"],
    )
    assert result["revision"] == "next-runtime-head"
    assert len(published) == 1
    assert upgrade.release_upgrade(plane, "frame", "check")["state"] == "pending"
    with pytest.raises(ValueError):
        upgrade.release_upgrade(
            plane,
            "frame",
            "apply",
            request=fixture.request,
            operator_key="key",
            confirm=True,
            plan_sha256=plan["plan_sha256"],
        )
    assert len(published) == 1


@pytest.mark.parametrize("failure", [None, "unsigned", "inbox", "generic"])
def test_real_sql_writer_signs_rotation_and_keeps_evidence_tables_untouched(
    fixture, monkeypatch, failure
):
    """Exercise the production transaction writer with a bounded recording SQL transport."""
    import time

    from beadhive.hq_sql_operator import SqlOperatorError
    from beadhive.hq_sql_runtime_schema import inbox_table

    state, digest = prepare(fixture)
    route = upgrade.route_for(state, "frame")
    table = inbox_table(route[0], route[4])
    queries = []

    class Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, parameters=None):
            queries.append((sql, parameters))
            self.sql = sql

        def fetchone(self):
            if "CURRENT_USER()" in self.sql:
                return "operator@localhost", "runtime", "main", "2.3.5"
            if "DOLT_HASHOF" in self.sql:
                return ("runtime-head",)
            if "information_schema.tables" in self.sql:
                return None if failure == "inbox" else (table,)
            if "DOLT_COMMIT" in self.sql:
                return ("next-runtime-head",)
            raise AssertionError(self.sql)

        def fetchall(self):
            assert "dolt_status" in self.sql
            return []

    class Connection:
        commits = 0
        rollbacks = 0

        def cursor(self):
            return Cursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            pass

    connection = Connection()
    crossref = (
        fixture.snapshot.backend_identity,
        fixture.snapshot.generation,
        fixture.snapshot.commit_revision,
    )
    operator = SqlRuntimeOperator(
        {
            "authority_writer": {"user": "operator", "database": "runtime"},
            "runtime_operator_public_key": fixture.public,
            "runtime_backend_identity": "runtime-backend",
            "runtime_generation": "runtime-generation",
        },
        clock=lambda: 1000,
    )
    monkeypatch.setattr(operator, "_open", lambda **kwargs: (connection, time.monotonic() + 30))
    monkeypatch.setattr(
        operator.authority,
        "verified_state_at",
        lambda *args, **kwargs: (fixture.state, crossref, {}),
    )
    monkeypatch.setattr(
        operator.authority, "load_latest_config_at", lambda *args, **kwargs: fixture.snapshot
    )
    monkeypatch.setattr(operator.authority, "fresh_config_head_fence", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        operator.authority, "fresh_runtime_head_fence", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        operator, "load", lambda **kwargs: ("next-runtime-head", state, crossref, {})
    )
    kwargs = {
        "expected_revision": "runtime-head",
        "operator_key": str(fixture.key),
        "provisioned_route": route,
        "release_upgrade": {"frame": "frame", "request": fixture.request, "plan_sha256": digest},
    }
    if failure == "unsigned":
        kwargs["operator_key"] = ""
    elif failure == "generic":
        kwargs.pop("release_upgrade")
    if failure:
        with pytest.raises(SqlOperatorError):
            operator.publish(state, **kwargs)
        assert connection.commits == 0
        assert connection.rollbacks == 1
    else:
        assert operator.publish(state, **kwargs) == "next-runtime-head"
        assert connection.commits == 1
        assert any("INSERT INTO hq_principal_registry" in sql for sql, _ in queries)
        assert any("UPDATE hq_authority" in sql for sql, _ in queries)
    assert not any(
        table_name in sql
        for sql, _ in queries
        for table_name in (
            "hq_live_registrations",
            "hq_live_receipts",
            "hq_live_results",
            "hq_live_public_observations",
        )
    )


@pytest.mark.parametrize("conformant", [True, False])
def test_upgraded_candidate_still_needs_fresh_evidence_for_normal_admission(fixture, conformant):
    from datetime import UTC, datetime

    from beadhive.host_heartbeat_core import HeartbeatLease, _authority_matches
    from beadhive.host_manifest_contracts import HostManifest
    from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane

    state, _digest = prepare(fixture)
    record = state["frames"]["frame"]["candidate"]
    authority = ObservationAuthority(**record["authority"])
    lease = HeartbeatLease.model_validate(
        {
            "domain": "beadhive/frame-heartbeat/v2",
            "beadyard_id": authority.beadyard_id,
            "frame_id": authority.frame_id,
            "holderIdentity": authority.holder_identity,
            "instance_ref": authority.instance_ref,
            "key_id": authority.key_fingerprint,
            "epoch": authority.epoch,
            "audience": authority.audience,
            "config_revision": authority.config_revision,
            "seq": 3,
            "renewTime": datetime.fromtimestamp(999, UTC).isoformat(),
            "release": fixture.request["release"],
            "state_seen": "pending",
            "report_digest": "sha256:" + "2" * 64,
            "conformance": {
                "profile": "new",
                "status": "conformant" if conformant else "non-conformant",
                "checks": [{"id": "configuration", "status": "pass" if conformant else "fail"}],
            },
        }
    )
    assert _authority_matches(lease, authority)
    assert not _authority_matches(lease, ObservationAuthority(**fixture.record["authority"]))
    receipts = [
        (3, "3", 999, lease),
        (2, "2", 990, lease.model_copy(update={"seq": 2})),
        (1, "1", 980, lease.model_copy(update={"seq": 1})),
    ]
    published = []

    def publish(after, **kwargs):
        guard.validate_state(after)
        upgrade.preserve_history(state, after)
        published.append(after)
        return "admitted-head"

    plane = object.__new__(SqlControlPlane)
    plane.clock = lambda: 1000
    plane._operator_deadline = lambda: 100000000000
    plane._operator = lambda: SimpleNamespace(
        load=lambda **kwargs: (
            "admitted-head" if published else "upgraded-head",
            published[-1] if published else state,
            (),
            {},
        ),
        evidence=lambda *args, **kwargs: (
            record,
            HostManifest.model_validate(fixture.manifest),
            receipts,
        ),
        publish=publish,
    )
    kwargs = {
        "expected": "upgraded-head",
        "expected_host_id": authority.holder_identity,
        "expected_release": fixture.request["release"]["digest"],
        "operator_key": "reviewed-key",
        "confirm": True,
    }
    if conformant:
        assert plane.lifecycle("admit", "frame", "apply", **kwargs)["state"] == "active"
        assert (
            published[0]["frames"]["frame"]["active"]["release_upgrade_history"]
            == record["release_upgrade_history"]
        )
    else:
        with pytest.raises(ControlPlaneError, match="complete trusted evidence"):
            plane.lifecycle("admit", "frame", "apply", **kwargs)
        assert not published
