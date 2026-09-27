"""Opt-in: the routing table names the proven ``bd`` path, and that path still holds its
concurrency guarantee, for one representative coordination operation — contested merge-slot
acquire.

Lease, heartbeat, reclaim, merge-slot and gate resolution have no v1.3 HTTP route (see
``COORDINATION_OPERATIONS`` in :mod:`beadhive_core.routing`); their exclusivity and staleness
guarantees are proven exhaustively elsewhere, against the real ``bd`` binary, by this repo's own
``tests/test_coordination_int.py`` (concurrent real-thread acquire, stale-lease reclaim via a
server-mode store) and ``tests/test_merge_slot.py`` (holder-token staleness policy). This test
does not re-prove those from scratch; it proves the one thing specific to bh-l5sxi.1 — that
*selecting* ``work.merge-slot.acquire`` through :class:`beadhive_core.RoutingTable` still points
at real ``bd``, not an in-memory stand-in, and that contested acquire through that exact path is
still exclusive.

This never touches a managed hive. It creates its own disposable scratch hive (embedded Dolt, no
server) in a temp directory, exactly the discipline ``test_core_review_real_service.py`` uses,
and never runs against ``bh host beads`` or any hive this repo's own operator uses. It is
opt-in via ``BEADS_ROUTING_SCRATCH=1``, the same "explicit env var, self-skip otherwise"
convention every other ``real_service`` test in this workspace follows (see
``packages/beadhive-beads-client/tests/test_real_service.py``) even where, as here, the test can
technically provision its own scratch hive: ``just packages-check`` runs every package's test
suite with no ``-m`` filter, so an unconditional real-``bd``, real-Dolt test would fire on every
plain run instead of only when explicitly requested.

    BEADS_ROUTING_SCRATCH=1 \
    uv run --locked --all-packages pytest packages/beadhive-core/tests -m real_service
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from beadhive_core import RouteKind, default_table

pytestmark = pytest.mark.real_service

TABLE = default_table()


def _bd(*args: str, cwd: Path) -> dict:
    result = subprocess.run(
        ["bd", *args, "--json"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _init_scratch_hive(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["bd", "init", "--prefix", "rtsc", "--non-interactive"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_routed_merge_slot_acquire_is_still_exclusive_against_real_bd(tmp_path: Path) -> None:
    if not os.environ.get("BEADS_ROUTING_SCRATCH"):
        pytest.skip("set BEADS_ROUTING_SCRATCH=1 to run the real-bd scratch-hive proof")

    route = TABLE.select_cli("work.merge-slot.acquire")
    assert route.kind is RouteKind.CLI_COMPATIBILITY
    assert "merge-slot" in route.reason

    _init_scratch_hive(tmp_path)
    assert _bd("merge-slot", "create", "--actor", "tester", cwd=tmp_path)["status"] == "open"

    first = _bd("merge-slot", "acquire", "--holder", "agentA", cwd=tmp_path)
    assert first == {"acquired": True, "holder": "agentA", "id": "rtsc-merge-slot"}

    contested = subprocess.run(
        ["bd", "merge-slot", "acquire", "--holder", "agentB", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert contested.returncode != 0
    assert json.loads(contested.stdout) == {
        "acquired": False,
        "holder": "agentA",
        "id": "rtsc-merge-slot",
    }

    refused_release = subprocess.run(
        ["bd", "merge-slot", "release", "--holder", "agentB", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert refused_release.returncode != 0
    assert "not agentB" in json.loads(refused_release.stdout)["error"]

    released = _bd("merge-slot", "release", "--holder", "agentA", cwd=tmp_path)
    assert released == {"id": "rtsc-merge-slot", "released": True}

    check = _bd("merge-slot", "check", cwd=tmp_path)
    assert check["available"] is True
    assert check["holder"] is None
