"""Run the language-neutral release-eligibility corpus against the real range-aware predicate.

The corpus lives in beadhive/hq-service (conformance/release-eligibility/v1). Each case is
the signed-beat carrier's release judgement for one frame (bh-vfrem.6): the real signed
authority read (SqlRuntimeAuthority.load_state, as in test_hq_authority_conformance.py),
then, only when the frame's grant carries a ``release_policy``, the real catalog read
(read_release_catalog_row, the case's ``hq_release_catalog`` rows ``AS OF`` the same HEAD),
then release_policy.admits on the case's measured release. Raw rows in SQLite; only the
connection is stubbed. Without an ``hq_release_catalog`` table the catalog is absent.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive.hq_sql_runtime import SqlRuntimeAuthority
from beadhive.release_policy import admits
from test_hq_lease_conformance import Cursor, _database


def _corpus() -> Path | None:
    if env := os.environ.get("HQ_RELEASE_ELIGIBILITY_CORPUS"):
        return Path(env)
    if ws := os.environ.get("GIT_WORKSPACE"):
        path = Path(ws) / "github/beadhive/hq-service/conformance/release-eligibility/v1"
        if path.is_dir():
            return path
    return None


CORPUS = _corpus()
CASES = sorted((CORPUS / "cases").glob("*.json")) if CORPUS is not None else []
pytestmark = pytest.mark.skipif(
    not CASES,
    reason="release-eligibility corpus not found "
    "(set HQ_RELEASE_ELIGIBILITY_CORPUS or GIT_WORKSPACE)",
)


class CatalogCursor(Cursor):
    """The base cursor drops one ``AS OF`` parameter; the catalog read pins two."""

    def execute(self, sql, params=()):
        if sql.count(" AS OF %s") > 1:
            parts = sql.split("%s")
            params = tuple(p for i, p in enumerate(params) if not parts[i].endswith(" AS OF "))
            sql = sql.replace(" AS OF %s", "")
        super().execute(sql, params)


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_release_eligibility_conformance(path, tmp_path):
    case = json.loads(path.read_text())
    cursor = CatalogCursor(_database(case["rows"]))
    connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: None, rollback=lambda: None)
    reader = SqlRuntimeAuthority(
        {
            "runtime": {"user": "observer", "database": "authority", "operation_timeout": 15},
            "runtime_backend_identity": "dolt:hq",
            "runtime_generation": "gen-1",
            "runtime_operator_public_key": case["trust"]["operator_public_key"],
            "runtime_floor_path": str(tmp_path / "floor.json"),
            "runtime_initial_revision": "head-1",
        },
        clock=lambda: case["now"],
    )
    reader._open = lambda deadline=None: (connection, float("inf"))
    head, state, _crossref, _policies = reader.load_state()
    desired = state["frames"][case["frame_id"]]["active"]["desired"]
    measured = case["measured"]
    row = None
    if (policy := desired.get("release_policy")) is not None:
        # hq_control_plane's signed-beat carrier: the catalog is read only under a policy.
        row = reader.read_release_catalog_row(head, policy["catalog"], measured)
    assert admits(measured, desired, row) is case["expect"]["admits"], row
