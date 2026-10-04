"""Exercise emergency permissions at the trusted SQL write boundary.

The transport and already-verified authority are in-memory ports. Only signature
verification is stubbed; request routing, shape, identity, policy projection,
receipt validation, permissions, lease CAS and mutations use the real receiver.
"""

import copy
import hashlib
import time
from types import SimpleNamespace

import pytest

from beadhive import hq_sql_receiver as receiver_module
from beadhive.host_lease_contracts import now_stamp
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_hive_policy import project_hive_policies
from beadhive.hq_sql_receiver import ReceiverError, SqlTrustedReceiver
from beadhive.hq_sql_runtime_schema import inbox_table
from beadhive.hq_sql_signatures import canonical
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot

NOW = 2000.0


class Cursor:
    def __init__(self, world):
        self.world = world
        self.rowcount = 1
        self.rows = []
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        world = self.world
        if sql.startswith("SELECT CURRENT_USER"):
            self.rows = [("observer@localhost", "authority", "main", "2.3.5")]
        elif sql.startswith("SELECT DOLT_HASHOF"):
            self.rows = [("head",)]
        elif "FROM hq_principal_registry" in sql:
            self.rows = [world.route]
        elif sql.startswith("SELECT kind,payload"):
            body = canonical(world.request)
            self.rows = [("hive_lease", body, hashlib.sha256(body).hexdigest())]
        elif "FROM hq_live_hive_leases" in sql:
            self.rows = [("previous", canonical(world.prior), "prior-request", "prior-sha")]
        elif "FROM hq_live_results" in sql:
            self.rows = []
        elif "FROM hq_live_floors" in sql:
            self.rows = [(1, world.beat_digest, NOW - 1000)]
        elif "FROM hq_live_receipts" in sql:
            self.rows = [(canonical(world.beat), canonical({"beat": world.beat}), NOW - 1000)]
        elif sql.startswith(
            ("START TRANSACTION", "UPDATE hq_live_hive_leases", "INSERT INTO hq_live_results")
        ):
            self.rows = []
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


@pytest.fixture
def world(monkeypatch):
    yard = "12345678-1234-4234-8234-123456789abc"
    authority = {
        "frame_id": "factory",
        "holder_identity": "host",
        "instance_ref": "instance",
        "key_fingerprint": "SHA256:key",
        "epoch": 1,
        "audience": yard,
        "beadyard_id": yard,
        "config_revision": "rev",
        "candidate_expires_at": None,
    }
    release = {"id": "reviewed-artifact", "digest": "sha256:" + "a" * 64}
    caps = {
        "max_sessions": 1,
        "isolation": "container",
        "trust_zone": "self-hosted",
        "arch": "x86_64",
        "harnesses": ["codex"],
    }
    record = {
        "authority": authority,
        "state": "active",
        "cordoned": False,
        "public_key": "key",
        "desired": {"release": release, "caps": caps, "profile": "reviewed", "declared": True},
    }
    record["emergency"] = {
        "domain": "beadhive/emergency-admission/v1",
        "prefix": "bh",
        "reason": "repair enrollment",
        "issued_at": NOW - 10,
        "expires_at": NOW + 100,
        "revoked_at": None,
        "authority": copy.deepcopy(authority),
        "release": release,
        "original_revision": "original",
        "review_required": True,
        "execution_digest": "sha256:" + "b" * 64,
    }
    beat = HeartbeatLease.model_validate(
        {
            "domain": "beadhive/frame-heartbeat/v2",
            "audience": yard,
            "beadyard_id": yard,
            "frame_id": "factory",
            "holderIdentity": "host",
            "instance_ref": "instance",
            "key_id": "SHA256:key",
            "epoch": 1,
            "config_revision": "rev",
            "seq": 1,
            "renewTime": now_stamp(NOW - 1000),
            "release": release,
            "state_seen": "pending",
            "conformance": {
                "profile": "reviewed",
                "status": "non-conformant",
                "checks": [{"id": "broken", "status": "fail"}],
            },
            "report_digest": "sha256:" + "c" * 64,
        }
    ).model_dump(mode="json", exclude_none=True)
    lease = {
        "host_id": "host",
        "label": "host",
        "epoch": 1,
        "adopted_at": now_stamp(NOW - 50),
        "expires_at": now_stamp(NOW + 60),
    }
    request = {
        "domain": "beadhive/sql-hive-lease/v2",
        "request_id": "request",
        "principal": "frame",
        "frame_id": "factory",
        "holder_identity": "host",
        "instance_ref": "instance",
        "epoch": 1,
        "key_fingerprint": "SHA256:key",
        "audience": yard,
        "beadyard_id": yard,
        "config_revision": "rev",
        "authority_revision": "head",
        "prefix": "bh",
        "expected_revision": "previous",
        "operation": "renew",
        "force": False,
        "lease": lease,
    }
    snapshot = FleetConfigSnapshot(
        "sql:config",
        "a" * 32,
        "generation",
        NOW - 50,
        NOW + 500,
        (
            FleetConfigDocument(
                "fleet.yaml",
                "managed_repos:\n- provider: github\n  org: bee\n  repo: hive\n"
                "  prefix: bh\n  frame_policy:\n    config_revision: rev\n"
                "    requires: {max_sessions: 1}\n"
                "    evict_after_s: 900\n",
            ),
        ),
    )
    policies = project_hive_policies(snapshot, valid_until=NOW + 500, now=NOW)
    state = {
        "expires_at": NOW + 500,
        "frames": {"factory": {"active": record, "candidate": None, "retired": []}},
    }
    world = SimpleNamespace(
        record=record,
        request=request,
        beat=beat,
        state=state,
        route=("frame", "factory", "host", "instance", 1, inbox_table("frame", 1), "SHA256:key"),
        beat_digest="heartbeat-digest",
        prior={"authority": authority, "lease": copy.deepcopy(lease)},
        committed=False,
        rolled_back=False,
    )
    cursor = Cursor(world)
    connection = SimpleNamespace(
        cursor=lambda: cursor,
        close=lambda: None,
        commit=lambda: setattr(world, "committed", True),
        rollback=lambda: setattr(world, "rolled_back", True),
    )
    receiver = object.__new__(SqlTrustedReceiver)
    receiver.settings = {"observer": {"user": "observer", "database": "authority"}}
    receiver.clock = lambda: NOW
    receiver._open = lambda: (connection, time.monotonic() + 60)
    receiver.authority = SimpleNamespace(
        verified_state_at=lambda *args, **kwargs: (state, ("config", "rev", "a" * 32), policies),
        load_config_at=lambda *args, **kwargs: snapshot,
        fresh_config_head_fence=lambda *args, **kwargs: None,
        fresh_runtime_head_fence=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        receiver_module,
        "verify_hive_request",
        lambda envelope, **kwargs: (envelope, hashlib.sha256(canonical(envelope)).hexdigest()),
    )
    monkeypatch.setattr(
        receiver_module,
        "verify_heartbeat",
        lambda envelope, **kwargs: (
            HeartbeatLease.model_validate(envelope["beat"]),
            world.beat_digest,
        ),
    )
    world.receiver, world.cursor = receiver, cursor
    return world


def test_authorized_stale_nonconformant_renewal_is_committed(world):
    assert world.receiver.accept_hive_lease("frame", "request")
    assert world.committed
    assert any(sql.startswith("UPDATE hq_live_hive_leases") for sql, _ in world.cursor.statements)


@pytest.mark.parametrize("case", ["ttl", "expired", "revoked", "scope"])
def test_permission_denials_do_not_write_lease(world, case):
    if case == "ttl":
        world.request["lease"]["expires_at"] = now_stamp(NOW + 101)
    elif case == "expired":
        world.record["emergency"]["expires_at"] = NOW
    elif case == "revoked":
        world.record["emergency"]["revoked_at"] = NOW
    else:
        world.record["emergency"]["prefix"] = "different"
    with pytest.raises(ReceiverError, match="emergency|lifetime"):
        world.receiver.accept_hive_lease("frame", "request")
    assert world.rolled_back and not world.committed
    assert not any(sql.startswith("UPDATE") for sql, _ in world.cursor.statements)


def test_ordinary_authority_rejects_same_stale_nonconformant_receipt(world):
    del world.record["emergency"]
    with pytest.raises(ReceiverError, match="fresh conformant"):
        world.receiver.accept_hive_lease("frame", "request")
    assert not world.committed


@pytest.mark.parametrize("case", ["identity", "release", "capacity", "cas"])
def test_emergency_keeps_nonwaivable_checks(world, case):
    if case == "identity":
        world.beat["holderIdentity"] = "different"
    elif case == "release":
        world.beat["release"] = {"id": "different", "digest": "sha256:" + "d" * 64}
    elif case == "capacity":
        world.record["desired"]["caps"]["max_sessions"] = 0
    else:
        world.request["expected_revision"] = "wrong"
    with pytest.raises(ReceiverError):
        world.receiver.accept_hive_lease("frame", "request")
    assert not world.committed
    assert not any(sql.startswith("UPDATE") for sql, _ in world.cursor.statements)


def test_emergency_cannot_renew_another_incumbent(world):
    world.prior["lease"]["host_id"] = "other-host"
    world.prior["authority"] = {**world.prior["authority"], "holder_identity": "other-host"}
    with pytest.raises(ReceiverError, match="exact incumbent"):
        world.receiver.accept_hive_lease("frame", "request")
    assert not world.committed
    assert not any(sql.startswith("UPDATE") for sql, _ in world.cursor.statements)
