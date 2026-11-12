"""Run the language-neutral authority-read corpus against the real SqlRuntimeAuthority.

The corpus lives in beadhive/hq-service (conformance/authority/v1). The read is
what homelab's refresh_native does: load_state (verified_state_at and the real
rollback floor file), then operator_signature AS OF the same HEAD. Raw rows in
SQLite as in test_hq_lease_conformance.py; only the connection is stubbed.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive.hq_sql_runtime import SqlRuntimeAuthority, SqlRuntimeError
from test_hq_lease_conformance import Cursor, _database


def _corpus() -> Path | None:
    if env := os.environ.get("HQ_AUTHORITY_CORPUS"):
        return Path(env)
    if ws := os.environ.get("GIT_WORKSPACE"):
        path = Path(ws) / "github/beadhive/hq-service/conformance/authority/v1"
        if path.is_dir():
            return path
    return None


CORPUS = _corpus()
CASES = sorted((CORPUS / "cases").glob("*.json")) if CORPUS is not None else []
pytestmark = pytest.mark.skipif(
    not CASES, reason="authority corpus not found (set HQ_AUTHORITY_CORPUS or GIT_WORKSPACE)"
)


class ObserverCursor(Cursor):
    def __init__(self, db, branch):
        super().__init__(db)
        self.branch = branch

    def execute(self, sql, params=()):
        super().execute(sql, params)
        if sql.startswith("SELECT CURRENT_USER"):
            self.rows = [("observer@localhost", "authority", self.branch, "2.3.5")]


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_authority_conformance(path, tmp_path):
    case = json.loads(path.read_text())
    codes = json.loads((CORPUS / "refusal-codes.json").read_text())
    row = case["rows"]["hq_authority"][0]
    floor = tmp_path / "floor.json"
    if case["floor"]:
        floor.write_text(
            json.dumps(
                {
                    "backend": row["backend_identity"],
                    "generation": row["generation"],
                    **case["floor"],
                },
                sort_keys=True,
            )
        )
        floor.chmod(0o600)
    cursor = ObserverCursor(_database(case["rows"]), (case["observer"] or {}).get("branch", "main"))
    connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: None, rollback=lambda: None)
    reader = SqlRuntimeAuthority(
        {
            "runtime": {"user": "observer", "database": "authority", "operation_timeout": 15},
            "runtime_backend_identity": "dolt:hq",
            "runtime_generation": "gen-1",
            "runtime_operator_public_key": case["trust"]["operator_public_key"],
            "runtime_floor_path": str(floor),
            "runtime_initial_revision": "head-1",
        },
        clock=lambda: case["now"],
    )
    reader._open = lambda deadline=None: (connection, float("inf"))
    expect = case["expect"]
    try:
        head, state, crossref, policies = reader.load_state()
    except SqlRuntimeError as exc:
        code = "observer_mismatch" if str(exc) == codes["observer_mismatch"] else "authority_failed"
        assert code == expect.get("refuse"), (str(exc), expect)
        return
    assert "serve" in expect, f"served, expected {expect}"
    cursor.execute("SELECT operator_signature FROM hq_authority AS OF %s", (head,))
    record = {
        "backend_identity": row["backend_identity"],
        "generation": row["generation"],
        "revision": row["revision"],
        "config_backend": crossref[0],
        "config_generation": crossref[1],
        "config_head": crossref[2],
        "state": state,
        "hive_policies": policies,
    }
    assert head == "head-1"
    assert record == {
        **{
            k: row[k]
            for k in (
                "backend_identity",
                "generation",
                "revision",
                "config_backend",
                "config_generation",
                "config_head",
            )
        },
        "state": json.loads(row["state_json"]),
        "hive_policies": json.loads(row["hive_policies_json"]),
    }
    assert cursor.fetchone()[0] == row["operator_signature"]
