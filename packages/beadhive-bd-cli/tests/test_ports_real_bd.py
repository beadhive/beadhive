"""Integration: the lifecycle, review, planning and read routes against a REAL `bd` (bh-o3xuf).

The FakeBd tests in ``test_ports.py`` / ``test_reads.py`` pin the argv each route shapes; this
proves the argv is one a real `bd` accepts and that each route reads back what the other wrote —
a claim lease taken and released, a state dimension written and read, a gate found by its anchored
bead id and resolved, a compiled molecule filed with its parent edges and dependency, and the
kickoff conventions `bh plan file` / `bh plan repair` share.

Every route runs over :class:`beadhive_bd_cli.SubprocessBd` against an embedded-Dolt scratch hive
this test creates in ``tmp_path``. Marked `integration` and self-skips without `bd` on PATH; the
fixtures are local — this package never imports the root test harness.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import beadhive_core as core
from beadhive_bd_cli import (
    CliGateOperations,
    CliIssues,
    CliLeases,
    CliMoleculeFiler,
    CliPlanningGates,
    CliStateOperations,
    CliStateReads,
    SubprocessBd,
    coordination,
    reads,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed"),
]

_TIMEOUT = 60
_SHARED_SERVER_ENV = frozenset(
    {"BEADS_DOLT_SHARED_SERVER", "BEADS_SHARED_SERVER_DIR", "BEADS_DOLT_SERVER_PORT"}
)
_ENV = {key: value for key, value in os.environ.items() if key not in _SHARED_SERVER_ENV}

BD = SubprocessBd(timeout=_TIMEOUT, env=_ENV)


@pytest.fixture
def hive(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["bd", "init", "--prefix", "pc", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=_ENV,
        timeout=_TIMEOUT,
    )
    return tmp_path


def _create(hive: Path, title: str) -> str:
    res = BD.run(["q", title], hive, capture=True)
    assert res.returncode == 0, res.stderr
    return (res.stdout or "").strip().splitlines()[-1].strip()


def test_claim_lease_assign_and_state_round_trip_through_real_bd(hive):
    bead = _create(hive, "leased bead")
    issues, leases = CliIssues(BD, hive), CliLeases(BD, hive)

    leases.acquire(bead, actor="dev/a")
    held = issues.get(bead)
    assert held is not None and held["status"] == "in_progress"
    assert held.get("assignee") == "dev/a"

    leases.release(bead, actor="dev/a")
    released = issues.get(bead)
    assert released is not None and released["status"] == "open"
    assert released.get("assignee") in (None, "")

    issues.assign(bead, "dev/b", actor="disp/x", read=released)
    assert (issues.get(bead) or {}).get("assignee") == "dev/b"

    states = CliStateOperations(BD, hive)
    # Measured: a real `bd state` on an unset dimension exits 0 with a placeholder line, which the
    # route (moved verbatim, bh-o3xuf) passes through — never a real review value either way.
    assert CliStateReads(BD, hive).get_state(bead, "review") in ("", "(no review state set)")
    states.set_state(bead, "review", "pending", reason="submitted abc1234", actor="dev/b")
    assert CliStateReads(BD, hive).get_state(bead, "review") == "pending"

    assert issues.get("pc-does-not-exist") is None
    with pytest.raises(core.WriteFailed):
        leases.acquire("pc-does-not-exist", actor="dev/a")


def test_gate_lookup_narrows_to_the_bead_and_resolve_closes_it_for_real(hive):
    first = _create(hive, "gated")
    sibling = _create(hive, "another gated bead")
    created = coordination.gate_create(BD, hive, blocks=first, reason="bh:review abc1234")
    other = coordination.gate_create(BD, hive, blocks=sibling, reason="bh:review def5678")
    assert created.ok and other.ok

    gates = CliGateOperations(BD, hive)
    found = gates.gates_for(first)
    assert [g.id for g in found] == [created.gate_id]
    assert found[0].status == "open"

    gates.resolve(created.gate_id, reason="approved by rev/bob", actor="rev/bob")
    assert [g.status for g in gates.gates_for(first)] == ["closed"]
    assert [g.status for g in gates.gates_for(sibling)] == ["open"]

    with pytest.raises(core.GateResolveFailed):
        gates.resolve("pc-not-a-gate", reason="x", actor="rev/bob")


def test_compiled_molecule_files_with_edges_and_kickoff_conventions(hive):
    compiled = core.compile_molecule(
        {
            "epic": {"title": "Epic"},
            "issues": [
                {"handle": "a", "title": "A", "acceptance": "works", "priority": 0},
                {"handle": "b", "title": "B", "acceptance": "works", "deps": ["a"]},
            ],
        },
        dimension_fields=(),
    )
    outcome = CliMoleculeFiler(BD, hive).apply(compiled, actor="planner")
    epic = outcome.ids[compiled.epic_key]
    a, b = (outcome.ids[compiled.issue_keys[h]] for h in ("a", "b"))

    kids = reads.children(BD, epic, hive)
    assert kids is not None
    assert {row["id"] for row in kids} == {a, b}
    assert (reads.show(BD, a, hive) or {}).get("priority") == 0

    ready = {row["id"] for row in reads.ready_rows(BD, hive, ["--limit", "0"]) or []}
    assert a in ready and b not in ready  # b waits on a

    gates = CliPlanningGates(BD, hive)
    assert gates.create_swarm(epic, actor="planner") is True
    gates.create_kickoff_gate(a, epic, actor="planner")
    gates.set_kickoff_pending(epic, actor="planner")
    assert CliStateReads(BD, hive).get_state(epic, "kickoff") == "pending"
    kickoff = CliGateOperations(BD, hive).gates_for(a)
    assert any(f"kickoff {epic}" in f"{g.description}\n{g.reason}" for g in kickoff)
