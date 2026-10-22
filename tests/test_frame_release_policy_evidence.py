"""Reader-version guard reads the release a frame attests on the carrier its HQ uses (bh-lc2rv).

Real Ed25519 signed beats and the real verifier; the SQL connection is a fake. Session evidence
and the newest verified signed beat count, only for the grant's current epoch. No network.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.test_frame_release_policy import make_state

from beadhive import frame_release_policy as policy
from beadhive import hq_sql_operator
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator
from beadhive.hq_sql_runtime_schema import evidence_table, inbox_table, session_table
from beadhive.hq_sql_signatures import canonical, fingerprint, sign_heartbeat

NOW = 1_000_000.0
EPOCH = 3


def _digest(release):
    return "sha256:" + hash(release).to_bytes(8, "big", signed=True).hex().ljust(64, "0")


class Fleet:
    """One frame per carrier case; builds the authority state and the fake SQL rows."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.state = make_state(release="0.30.0")
        self.state["frames"] = {}
        self.routes = {}
        self.evidence = {}
        self.inbox = {}
        self.present = {}
        self.keys = {}

    def add(self, name, *, carrier, release, epoch=EPOCH, measured=True, receipt=None):
        private = Ed25519PrivateKey.generate()
        public = (
            private.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        path = self.tmp_path / f"{name}.key"
        path.write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            )
        )
        record = copy.deepcopy(make_state(release="0.30.0")["frames"]["frame"]["active"])
        authority = record["authority"]
        authority.update(
            frame_id=name,
            holder_identity=f"holder-{name}",
            instance_ref=f"vm-{name}",
            key_fingerprint=fingerprint(public),
            epoch=EPOCH,
        )
        record["public_key"] = public
        if receipt:
            record["receipt"]["lease"] = {"release": {"id": receipt, "digest": _digest(receipt)}}
        self.state["frames"][name] = {
            "active": record,
            "candidate": None,
            "retired": [],
            "epoch_floor": EPOCH,
        }
        principal = f"frame_{name}"
        session, evidence = session_table(principal, EPOCH), evidence_table(principal, EPOCH)
        inbox = inbox_table(principal, EPOCH)
        self.routes[name] = (
            principal,
            name,
            authority["holder_identity"],
            authority["instance_ref"],
            EPOCH,
            session if carrier == "session" else inbox,
            fingerprint(public),
        )
        if carrier == "session":
            self.present[name] = {session, evidence}
            self.evidence[evidence] = (
                epoch,
                release or "",
                _digest(release),
                "conformant",
                datetime(2026, 10, 22) if measured else None,
            )
        elif release:
            lease = HeartbeatLease(
                audience=authority["audience"],
                frame_id=name,
                holderIdentity=authority["holder_identity"],
                instance_ref=authority["instance_ref"],
                key_id=fingerprint(public),
                epoch=epoch,
                config_revision=authority["config_revision"],
                seq=1,
                renewTime=datetime.fromtimestamp(NOW - 10, UTC).isoformat(),
                state_seen="active",
                release={"id": release, "digest": _digest(release)},
                report_digest="sha256:" + "2" * 64,
                conformance={"profile": "p", "status": "conformant", "checks": []},
            )
            self.inbox[inbox] = [(canonical(sign_heartbeat(lease, signing_key=str(path))),)]


class FakeSql:
    def __init__(self, fleet, head="head"):
        self.fleet = fleet
        self.head_value = head
        self.reads = []

    def begin(self):
        pass

    def head(self):
        return self.head_value

    def routes_for_frame_at(self, head, frame):
        self.reads.append(("route", head, frame))
        route = self.fleet.routes.get(frame)
        return [route] if route else []

    def tables_present(self, names):
        return {n for n in names if any(n in s for s in self.fleet.present.values())}

    def evidence_release(self, table):
        self.reads.append(("evidence", table))
        return self.fleet.evidence.get(table)

    def inbox_heartbeats(self, table):
        self.reads.append(("inbox", table))
        return self.fleet.inbox.get(table, [])


def operator_for(monkeypatch, fleet, head="head"):
    sql = FakeSql(fleet, head)

    @contextmanager
    def cursor():
        yield object()

    connection = SimpleNamespace(cursor=cursor, rollback=lambda: None, close=lambda: None)
    operator = SqlRuntimeOperator.__new__(SqlRuntimeOperator)
    operator.clock = lambda: NOW
    operator.authority = SimpleNamespace(fresh_runtime_head_fence=lambda *a, **k: None)
    operator._open = lambda deadline=None: (connection, 1.0)
    operator._identity = lambda cursor: None
    monkeypatch.setattr(hq_sql_operator, "runtime_statements", lambda cursor: sql)
    return operator, sql


def read(monkeypatch, fleet):
    operator, _sql = operator_for(monkeypatch, fleet)
    return operator.attested_releases(fleet.state, expected_revision="head")


def test_session_and_signed_beat_frames_are_known(monkeypatch, tmp_path):
    fleet = Fleet(tmp_path)
    fleet.add("factory", carrier="session", release="0.29.1")
    fleet.add("xeno", carrier="inbox", release="0.29.0")
    found = read(monkeypatch, fleet)
    assert found == {("factory", "active"): "0.29.1", ("xeno", "active"): "0.29.0"}
    assert policy.unsafe_readers(fleet.state, found) == []
    # Without the evidence the record carries no receipt: the reported bug.
    assert [kind for *_rest, kind in policy.unsafe_readers(fleet.state)] == ["unknown"] * 2


def test_old_release_evidence_refuses_and_allow_unknown_does_not_waive(monkeypatch, tmp_path):
    fleet = Fleet(tmp_path)
    fleet.add("factory", carrier="session", release="0.28.4")
    fleet.add("xeno", carrier="inbox", release="0.28.4")
    found = read(monkeypatch, fleet)
    assert {kind for *_r, kind in policy.unsafe_readers(fleet.state, found)} == {"old"}
    with pytest.raises(ValueError, match="no override"):
        policy.prepare(
            fleet.state, "factory", "head", "^0.30", allow_unknown_readers=True, evidence=found
        )


def test_stale_epoch_unmeasured_and_missing_evidence_are_unknown(monkeypatch, tmp_path):
    fleet = Fleet(tmp_path)
    fleet.add("stale", carrier="session", release="0.29.1", epoch=EPOCH - 1)
    fleet.add("unmeasured", carrier="session", release="0.29.1", measured=False)
    fleet.add("empty", carrier="session", release="")
    fleet.add("stalebeat", carrier="inbox", release="0.29.1", epoch=EPOCH - 1)
    fleet.add("nobeat", carrier="inbox", release=None)
    found = read(monkeypatch, fleet)
    assert found == {}
    assert {
        f for f, _r, kind in policy.unsafe_readers(fleet.state, found) if kind == "unknown"
    } == {
        "stale",
        "unmeasured",
        "empty",
        "stalebeat",
        "nobeat",
    }
    with pytest.raises(ValueError, match="--allow-unknown-readers"):
        policy.prepare(fleet.state, "stale", "head", "^0.30", evidence=found)
    policy.prepare(
        fleet.state, "stale", "head", "^0.30", evidence=found, allow_unknown_readers=True
    )


def test_beat_from_a_foreign_key_does_not_count(monkeypatch, tmp_path):
    fleet = Fleet(tmp_path)
    fleet.add("a", carrier="inbox", release="0.29.1")
    fleet.add("b", carrier="inbox", release=None)
    # Frame b's inbox holds a beat signed by a's key: it must not verify for b.
    fleet.inbox[inbox_table("frame_b", EPOCH)] = fleet.inbox[inbox_table("frame_a", EPOCH)]
    assert read(monkeypatch, fleet) == {("a", "active"): "0.29.1"}


def test_head_moved_refuses_and_reads_are_as_of_the_head(monkeypatch, tmp_path):
    fleet = Fleet(tmp_path)
    fleet.add("factory", carrier="session", release="0.29.1")
    operator, sql = operator_for(monkeypatch, fleet, head="other")
    with pytest.raises(SqlOperatorError, match="changed"):
        operator.attested_releases(fleet.state, expected_revision="head")
    operator, sql = operator_for(monkeypatch, fleet)
    operator.attested_releases(fleet.state, expected_revision="head")
    assert ("route", "head", "factory") in sql.reads


def test_downgrade_guard_uses_the_evidence_release(monkeypatch, tmp_path):
    fleet = Fleet(tmp_path)
    fleet.add("factory", carrier="session", release="0.29.1")
    fleet.state["frames"]["factory"]["active"]["desired"]["release"]["id"] = "0.28.4"
    found = read(monkeypatch, fleet)
    # ==0.28.* excludes the attested 0.29.1 while admitting lower releases: a downgrade.
    with pytest.raises(ValueError, match="--allow-downgrade"):
        policy.prepare(fleet.state, "factory", "head", "0.28.x", evidence=found)
    _state, plan = policy.prepare(
        fleet.state, "factory", "head", "0.28.x", evidence=found, allow_downgrade=True
    )
    assert plan["attested_release"] == "0.29.1"
    assert plan["downgrade"] is True


def test_receipt_carrier_is_unchanged_without_evidence(tmp_path):
    fleet = Fleet(tmp_path)
    fleet.add("legacy", carrier="inbox", release=None, receipt="0.29.2")
    assert policy.attested_release(fleet.state["frames"]["legacy"]["active"]) == "0.29.2"
    assert policy.unsafe_readers(fleet.state) == []
    # Evidence, when present, is the fresher carrier and wins.
    assert policy.attested_release(fleet.state["frames"]["legacy"]["active"], "0.29.3") == "0.29.3"
