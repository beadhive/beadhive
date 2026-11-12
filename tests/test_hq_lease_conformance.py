"""Run the language-neutral hive-lease corpus against the real SqlTrustedReceiver.

The corpus lives in beadhive/hq-service (conformance/hive-lease/v1); the Go
service must pass the same cases. Each case carries the raw HQ rows it stands
for. They are loaded into an in-memory SQLite database and the receiver's own
queries run against them. Every signature is real and verified by the real
verify_* functions, verified_state_at's validation (validate_state, policy
checks, expiry) runs for real, and the config catalog is read from the case's
config rows by the real _committed (including validate_documents). Stubbed: the
rollback floor files, the config/runtime head fences, and the post-commit
emergency audit.

A case with ``config_move`` models a config head H1 later than the signed H0
(bh-u67ve, 27da31b1): the real SqlRuntimeAuthority.bound_config_at runs over the
case's rows (H0) and the case's H1 documents, with HAS_ANCESTOR answered by
``descends_from_signed``. The registration runner reuses this harness.
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import hq_authority_guard as guard
from beadhive import hq_sql_config
from beadhive import hq_sql_receiver as receiver_module
from beadhive.hq_hive_policy import validate_sql_hive_policies
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_sql_receiver import ReceiverError, SqlTrustedReceiver
from beadhive.hq_sql_runtime import SqlRuntimeAuthority, SqlRuntimeError
from beadhive.hq_sql_signatures import verify_authority
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot


def _corpus() -> Path | None:
    if env := os.environ.get("HQ_LEASE_CORPUS"):
        return Path(env)
    if ws := os.environ.get("GIT_WORKSPACE"):
        path = Path(ws) / "github/beadhive/hq-service/conformance/hive-lease/v1"
        if path.is_dir():
            return path
    return None


CORPUS = _corpus()
CASES = sorted((CORPUS / "cases").glob("*.json")) if CORPUS is not None else []
pytestmark = pytest.mark.skipif(
    not CASES, reason="hive-lease corpus not found (set HQ_LEASE_CORPUS or GIT_WORKSPACE)"
)


def _database(rows: dict) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    for table, items in rows.items():
        if not items:
            continue
        columns = list(items[0])
        # Quoted: hq_release_catalog has a `commit` column, an SQLite keyword.
        quoted = ",".join('"' + column + '"' for column in columns)
        db.execute(f"CREATE TABLE {table} ({quoted})")
        db.executemany(
            f"INSERT INTO {table} VALUES ({','.join('?' * len(columns))})",
            [tuple(item[c] for c in columns) for item in items],
        )
    return db


class Cursor:
    """Runs the receiver's reads against SQLite; acknowledges its writes."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.rows = []
        self.rowcount = 1
        self.descends = True  # HAS_ANCESTOR(H1, H0); a case's config_move sets it

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        if sql.startswith("USE "):
            self.rows = []
        elif sql.startswith("SELECT HAS_ANCESTOR"):
            self.rows = [(1 if self.descends else 0,)]
        elif sql.startswith("SELECT CURRENT_USER"):
            self.rows = [("observer@localhost", "authority", "main", "2.3.5")]
        elif sql.startswith("SELECT DOLT_HASHOF"):
            self.rows = [("head-1",)]
        elif sql.startswith(("START TRANSACTION", "UPDATE", "INSERT")):
            self.rows = []
        else:
            if " AS OF %s" in sql:
                sql, params = sql.replace(" AS OF %s", ""), params[1:]
            try:
                self.rows = self.db.execute(sql.replace("%s", "?"), params).fetchall()
            except sqlite3.OperationalError as exc:
                if not str(exc).startswith("no such table"):  # absent table == no rows
                    raise
                self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def _verified(row: dict, state: dict, policies: dict, case: dict):
    """SqlRuntimeAuthority.verified_state_at for real, minus the rollback floor file."""
    signed = {
        "backend_identity": row["backend_identity"],
        "generation": row["generation"],
        "revision": row["revision"],
        "config_backend": row["config_backend"],
        "config_generation": row["config_generation"],
        "config_head": row["config_head"],
        "state": state,
        "hive_policies": policies,
    }
    verify_authority(
        signed,
        row["operator_signature"],
        granted_public_key=case["trust"]["operator_public_key"],
    )
    guard.validate_state(state)
    validate_sql_hive_policies(
        policies, config_head=row["config_head"], now=case["now"], require_fresh=False
    )
    if any(p["valid_until"] > state["expires_at"] for p in policies.values()):
        raise SqlRuntimeError("protected hive policy exceeds signed authority expiry")
    if (
        state["generation"] != row["generation"]
        or state["revision"] != row["revision"]
        or case["now"] < state["issued_at"] - 30
        or case["now"] >= state["expires_at"]
    ):
        raise SqlRuntimeError("protected HQ runtime authority expired or inconsistent")
    return state, (row["config_backend"], row["config_generation"], row["config_head"]), policies


def _config(cursor, crossref: tuple, now: float) -> FleetConfigSnapshot:
    """load_config_at for real: the committed config rows AS OF the signed config head."""
    backend, generation, _sequence, documents, _version = SqlFleetConfigRevisionStore._committed(
        cursor, crossref[2]
    )
    snapshot = FleetConfigSnapshot(
        "sql:" + backend, crossref[2], generation, now - 50, now + 10**6, documents
    )
    if (snapshot.backend_identity, snapshot.generation, snapshot.commit_revision) != crossref:
        raise SqlRuntimeError("HQ config and authority publications are not bound")
    return snapshot


def _moved_authority(case: dict, world, monkeypatch) -> SqlRuntimeAuthority:
    """The real bound_config_at, reading H1 (the case's config_move) after the signed H0."""
    move, now = case["config_move"], case["now"]
    world.cursor.descends = move["descends_from_signed"]
    # committed_snapshot_at memoizes per (backend, generation, head); cases share the signed head.
    monkeypatch.setattr(hq_sql_config, "_COMMITTED_CACHE", {})
    documents = tuple(FleetConfigDocument(d["path"], d["content"]) for d in move["documents"])
    authority = object.__new__(SqlRuntimeAuthority)
    authority.clock = lambda: now
    authority.broker = None
    authority.settings = {
        "reader": {"database": "config"},
        "observer": {"database": "authority"},
        "backend_identity": "config",
        "generation": "generation",
        "cache_ttl": 10**6,
    }
    authority.load_latest_config_at = lambda cursor, **k: FleetConfigSnapshot(
        "sql:config", move["head"], "generation", now - 50, now + 10**6, documents
    )
    return authority


def _receiver(case: dict, monkeypatch) -> SimpleNamespace:
    now, rows = case["now"], case["rows"]
    authority = rows["hq_authority"][0]
    state = json.loads(authority["state_json"])
    policies = json.loads(authority["hive_policies_json"])
    world = SimpleNamespace(committed=False, cursor=Cursor(_database(rows)))
    connection = SimpleNamespace(
        cursor=lambda: world.cursor,
        close=lambda: None,
        commit=lambda: setattr(world, "committed", True),
        rollback=lambda: None,
    )
    receiver = object.__new__(SqlTrustedReceiver)
    receiver.settings = {"observer": {"user": "observer", "database": "authority"}}
    receiver.clock = lambda: now
    receiver._open = lambda: (connection, time.monotonic() + 60)
    receiver.authority = SimpleNamespace(
        verified_state_at=lambda *a, **k: _verified(authority, state, policies, case),
        load_config_at=lambda cursor, crossref, **k: _config(cursor, crossref, now),
        # beadhive >= 27da31b1 (bh-u67ve) reads the bound config through bound_config_at;
        # the corpus binds exactly, so the signed head is the head read.
        bound_config_at=lambda cursor, crossref, **k: (_config(cursor, crossref, now), crossref[2]),
        fresh_config_head_fence=lambda *a, **k: None,
        fresh_runtime_head_fence=lambda *a, **k: None,
    )
    if "config_move" in case:
        bound = _moved_authority(case, world, monkeypatch)
        receiver.authority.bound_config_at = bound.bound_config_at
    monkeypatch.setattr(receiver_module.time, "monotonic", lambda: 0.0)
    # Post-commit emergency audit is I/O, not a lease rule.
    monkeypatch.setattr("beadhive.frame_emergency.audit", lambda *a, **k: None)
    world.receiver = receiver
    return world


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_hive_lease_conformance(path, monkeypatch):
    case = json.loads(path.read_text())
    refusal_codes = json.loads((CORPUS / "refusal-codes.json").read_text())
    codes = {msg: code for code, msg in refusal_codes.items()}
    world = _receiver(case, monkeypatch)
    expect = case["expect"]
    try:
        revision = world.receiver.accept_hive_lease(
            case["call"]["principal"], case["call"]["request_id"]
        )
    except ReceiverError as exc:
        assert codes.get(str(exc), str(exc)) == expect.get("refuse"), expect
        return
    if "replay" in expect:
        assert (revision, world.committed) == (expect["replay"], False)
    else:
        assert "accept" in expect, f"accepted {revision}, expected {expect}"
        assert (revision, world.committed) == (expect["accept"]["revision"], True)
