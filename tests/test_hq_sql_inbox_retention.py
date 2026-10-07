"""Bounded signed-mode inbox retention (bh-ce886), unit level.

Real Ed25519 signatures and the real verifier; the SQL connection is a fake cursor. The
pinned-Dolt proof (operator-only DELETE, frames still without it) lives in
``tests/test_fleet_membership_e2e_int.py``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from beadhive import hq_operator_settings
from beadhive.cli import app
from beadhive.hq_control_plane import ControlPlaneError
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_sql_inbox_retention import (
    ENV_VAR,
    INBOX_RETENTION_DEFAULT_S,
    SKEW_SECONDS,
    plan_prune,
    resolve_retention,
)
from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator
from beadhive.hq_sql_runtime import PrincipalBinding, newest_signed_heartbeat
from beadhive.hq_sql_signatures import canonical, fingerprint, sign_heartbeat

NOW = float(int(time.time()))
LEASE = 300  # HeartbeatLease default leaseDurationSeconds
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


@pytest.fixture
def frame(tmp_path):
    key = tmp_path / "frame.key"
    public = _keypair(key)
    other = tmp_path / "other.key"
    other_public = _keypair(other)
    signer = fingerprint(public)
    route = PrincipalBinding(
        "frame_a", "frame-1", "host-1", "vm-1", 1, "hq_live_inbox_frame_a_1", signer
    )
    record = {
        "authority": {
            "frame_id": "frame-1",
            "holder_identity": "host-1",
            "instance_ref": "vm-1",
            "key_fingerprint": signer,
            "epoch": 1,
            "audience": "fixture-fleet",
            "config_revision": "desired-1",
            "candidate_expires_at": None,
        },
        "public_key": public,
        "state": "active",
        "cordoned": False,
        "desired": {"declared": True, "release": RELEASE, "profile": "fixture"},
    }

    def beat(seq, renew_at, *, intruder=False, config_revision="desired-1"):
        lease = HeartbeatLease(
            audience="fixture-fleet",
            frame_id="frame-1",
            holderIdentity="host-1",
            instance_ref="vm-1",
            key_id=fingerprint(other_public) if intruder else signer,
            epoch=1,
            config_revision=config_revision,
            seq=seq,
            renewTime=datetime.fromtimestamp(renew_at, UTC).isoformat(),
            state_seen="active",
            release=RELEASE,
            report_digest="sha256:" + "2" * 64,
            conformance={"profile": "fixture", "status": "conformant", "checks": []},
        )
        return canonical(sign_heartbeat(lease, signing_key=str(other if intruder else key)))

    return SimpleNamespace(route=route, record=record, beat=beat, public=public)


def _plan(frame, inbox, *, now, retention_s=60):
    return plan_prune(
        list(inbox.items()), frame.route, frame.public, now=now, retention_s=retention_s
    )


def _newest(frame, inbox, *, now):
    return newest_signed_heartbeat(
        [(p,) for p in inbox.values()], frame.route, frame.record, now=now
    )


# ---- the bound -------------------------------------------------------------------------


def test_growth_is_bounded_across_many_beats_and_fresh_eligibility_is_unchanged(frame):
    interval, retention = 60, 120
    horizon = LEASE + SKEW_SECONDS + retention
    bound = horizon // interval + 2
    inbox: dict[str, bytes] = {}
    for seq in range(1, 241):  # four hours of one-minute beats
        now = NOW + seq * interval
        inbox[f"req-{seq}"] = frame.beat(seq, now)
        before = _newest(frame, inbox, now=now)
        plan = _plan(frame, inbox, now=now, retention_s=retention)
        for request_id in plan.delete:
            del inbox[request_id]
        # The reader's verdict on the same instant is byte-identical after the prune.
        assert _newest(frame, inbox, now=now) == before
        assert before[0] == seq and before[2] > now  # the fresh beat is still eligible
        assert len(inbox) <= bound
    assert len(inbox) <= bound
    assert f"req-{240}" in inbox


def test_no_row_a_reader_could_still_count_is_deleted(frame):
    inbox = {
        # Fresh for a reader whose clock lags the operator by the full skew window.
        "edge": frame.beat(1, NOW - LEASE - SKEW_SECONDS + 1),
        "stale": frame.beat(2, NOW - LEASE - SKEW_SECONDS - 61),
        "newest": frame.beat(3, NOW),
    }
    plan = _plan(frame, inbox, now=NOW, retention_s=60)
    assert plan.delete == ["stale"]
    assert plan.newest_verified == "newest"


def test_newest_verified_beat_survives_however_old(frame):
    """A frame that stopped beating keeps its last beat: heartbeat_report seeds seq from it."""
    inbox = {f"r{seq}": frame.beat(seq, NOW - 86400 + seq) for seq in range(1, 6)}
    plan = _plan(frame, inbox, now=NOW)
    assert plan.newest_verified == "r5"
    assert sorted(plan.delete) == ["r1", "r2", "r3", "r4"]
    assert plan.retained == 1


def test_newest_verified_tolerates_a_config_revision_change(frame):
    """Beats signed under an older desired revision still anchor the keep rule."""
    inbox = {
        "old": frame.beat(1, NOW - 86400),
        "older-rev": frame.beat(2, NOW - 80000, config_revision="desired-0"),
    }
    plan = _plan(frame, inbox, now=NOW)
    assert plan.newest_verified == "older-rev"
    assert plan.delete == ["old"]


def test_forged_rows_never_displace_the_newest_verified_beat(frame):
    inbox = {
        "real": frame.beat(1, NOW - 86400),
        "forged-old": frame.beat(9, NOW - 80000, intruder=True),
        "forged-fresh": frame.beat(10, NOW, intruder=True),
        "junk": b"{not json",
        # Authentic and newest, but not canonical: the reader skips it, so it is not kept.
        "spaced": frame.beat(11, NOW - 70000).replace(b"{", b"{ ", 1),
    }
    plan = _plan(frame, inbox, now=NOW)
    assert plan.newest_verified == "real"
    # A stale forged row can never count, so it goes; fresh and unparseable rows stay.
    assert sorted(plan.delete) == ["forged-old", "spaced"]
    assert plan.unparseable == 1
    assert plan.retained == 2


def test_empty_inbox_plans_nothing(frame):
    plan = _plan(frame, {}, now=NOW)
    assert plan.delete == [] and plan.newest_verified is None and plan.retained == 0


# ---- configuration ---------------------------------------------------------------------


def test_retention_resolution_order():
    assert resolve_retention(env={}) == (INBOX_RETENTION_DEFAULT_S, "built-in default (1h)")
    assert resolve_retention(env={ENV_VAR: "2h"}).seconds == 7200
    assert resolve_retention(settings="30m", env={ENV_VAR: "2h"}).seconds == 1800
    assert resolve_retention(cli=90, settings="30m", env={ENV_VAR: "2h"}).seconds == 90
    with pytest.raises(ValueError, match=ENV_VAR):
        resolve_retention(env={ENV_VAR: "-1"})


BINDING = {
    "host": "127.0.0.1",
    "port": 3306,
    "database": "beadhive_hq",
    "user": "authority_writer",
    "tls_mode": "disabled",
    "credential": {"config_path": "/f/fnox.toml", "profile": "p", "key": "K"},
}


def _settings_file(tmp_path, sql):
    path = tmp_path / "operator.json"
    path.write_text(json.dumps({"hq": {"sql": sql}}))
    return path


def test_operator_settings_carry_the_retention_key(tmp_path):
    path = _settings_file(
        tmp_path,
        {
            "reader": {**BINDING, "user": "reader"},
            "authority_writer": BINDING,
            "inbox_retention_s": "2h",
        },
    )
    plane = hq_operator_settings.operator_plane(path)
    assert plane.inbox_retention_s == "2h"
    assert "inbox_retention_s" not in plane.settings


def test_operator_settings_reject_a_bad_retention(tmp_path):
    path = _settings_file(tmp_path, {"authority_writer": BINDING, "inbox_retention_s": "soon"})
    with pytest.raises(ControlPlaneError, match="inbox_retention_s"):
        hq_operator_settings.load_settings(path)


def test_cli_prune_inbox_is_a_dry_run_without_confirm(monkeypatch):
    calls = []

    class Plane:
        inbox_retention_s = "2h"

        def prune_inbox(self, frame, *, retention_s, dry_run):
            calls.append((frame, retention_s, dry_run))
            return {"frame": frame, "dry_run": dry_run}

    monkeypatch.setattr(
        hq_operator_settings, "select_plane", lambda operator_settings=None: Plane()
    )
    monkeypatch.delenv(ENV_VAR, raising=False)
    runner = CliRunner()
    dry = runner.invoke(app, ["hq", "authority", "prune-inbox", "--frame", "frame-1"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["retention_source"].startswith("operator settings")
    real = runner.invoke(app, ["hq", "authority", "prune-inbox", "--frame", "frame-1", "--confirm"])
    assert real.exit_code == 0, real.output
    assert calls == [("frame-1", 7200, True), ("frame-1", 7200, False)]
    missing = runner.invoke(app, ["hq", "authority", "prune-inbox"])
    assert missing.exit_code == 1 and "--frame" in missing.output


# ---- operator side ---------------------------------------------------------------------


class _Cursor:
    def __init__(self, world):
        self.world = world
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.world.statements.append((sql, params))
        if sql.startswith("SELECT CURRENT_USER()"):
            self._result = [("authority_writer@localhost", "rt", "main", "2.3.5")]
        elif "DOLT_HASHOF" in sql:
            self._result = [("head-1",)]
        elif "FROM hq_principal_registry" in sql:
            self._result = [tuple(self.world.route.__dict__.values())]
        elif sql.startswith("SELECT request_id,payload FROM"):
            self._result = list(self.world.inbox.items())
        elif sql.startswith("DELETE FROM"):
            for request_id in params:
                self.world.inbox.pop(request_id, None)
            self._result = []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _Connection:
    def __init__(self, world):
        self.world = world

    def cursor(self):
        return _Cursor(self.world)

    def commit(self):
        self.world.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _operator(frame, inbox, monkeypatch, *, records=None):
    world = SimpleNamespace(route=frame.route, inbox=dict(inbox), statements=[], commits=0)
    operator = SqlRuntimeOperator(
        {
            "runtime": None,
            "authority_writer": {
                "user": "authority_writer",
                "database": "rt",
                "operation_timeout": 30,
            },
        },
        broker=object(),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(
        operator, "_open", lambda deadline=None: (_Connection(world), time.monotonic() + 30)
    )
    state = {
        "frames": {
            "frame-1": {
                "active": frame.record if records is None else records,
                "candidate": None,
                "retired": [],
            }
        }
    }
    monkeypatch.setattr(
        operator.authority,
        "verified_state_at",
        lambda cursor, head, deadline=None, allow_expired=False: (state, None, None),
    )
    return operator, world


def test_operator_prune_deletes_only_expired_heartbeats(frame, monkeypatch):
    inbox = {
        "r1": frame.beat(1, NOW - 7200),
        "r2": frame.beat(2, NOW - 3600),
        "r3": frame.beat(3, NOW),
    }
    operator, world = _operator(frame, inbox, monkeypatch)
    result = operator.prune_inbox("frame-1", retention_s=60)
    assert result["complete"] and not result["dry_run"]
    assert result["inboxes"] == [
        {
            "inbox": "hq_live_inbox_frame_a_1",
            "epoch": 1,
            "deleted": 2,
            "retained": 1,
            "unparseable": 0,
            "newest_verified": "r3",
        }
    ]
    assert set(world.inbox) == {"r3"}
    deletes = [sql for sql, _ in world.statements if sql.startswith("DELETE")]
    assert deletes and all(
        "hq_live_inbox_frame_a_1 WHERE kind='heartbeat'" in sql for sql in deletes
    )
    assert world.commits == 1


def test_operator_prune_dry_run_deletes_nothing(frame, monkeypatch):
    inbox = {"r1": frame.beat(1, NOW - 7200), "r2": frame.beat(2, NOW)}
    operator, world = _operator(frame, inbox, monkeypatch)
    result = operator.prune_inbox("frame-1", retention_s=60, dry_run=True)
    assert result["inboxes"][0]["would_delete"] == 1
    assert set(world.inbox) == {"r1", "r2"}
    assert not any(sql.startswith("DELETE") for sql, _ in world.statements)
    assert world.commits == 0


def test_operator_prune_skips_an_inbox_without_a_granted_incarnation(frame, monkeypatch):
    inbox = {"r1": frame.beat(1, NOW - 7200)}
    stranger = {**frame.record, "authority": {**frame.record["authority"], "epoch": 2}}
    operator, world = _operator(frame, inbox, monkeypatch, records=stranger)
    result = operator.prune_inbox("frame-1", retention_s=60)
    assert result["inboxes"][0]["skipped"] == "incarnation not in authority"
    assert set(world.inbox) == {"r1"}


def test_operator_prune_requires_a_frame(frame, monkeypatch):
    operator, _ = _operator(frame, {}, monkeypatch)
    with pytest.raises(SqlOperatorError):
        operator.prune_inbox("", retention_s=60)
