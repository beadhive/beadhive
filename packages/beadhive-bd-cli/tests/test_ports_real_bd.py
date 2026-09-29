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
import signal
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


@pytest.fixture
def server_hive(tmp_path: Path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    try:
        result = subprocess.run(
            ["bd", "init", "--server", "--prefix", "pc", "--non-interactive"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=_ENV,
        )
        assert result.returncode == 0, result.stderr
        yield tmp_path
    finally:
        # Owned-mode pidfile names only this fixture's server, including failed setup.
        pidfile = tmp_path / ".beads" / "dolt-server.pid"
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass


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


@pytest.mark.parametrize(
    "lease_state", ["expired", "live", "within_grace", "missing", "foreign_replica", "renewed"]
)
def test_atomic_abandon_reclaim_obeys_real_expiry_replica_and_audit(server_hive, lease_state):
    hive = server_hive
    transport = SubprocessBd(timeout=_TIMEOUT, env={**_ENV, "BEADS_NODE_ID": "replica-a"})
    leases = CliLeases(transport, hive)
    issues = CliIssues(transport, hive)
    bead = _create(hive, "atomic abandon proof")
    leases.acquire(bead, actor="dev/a")
    CliStateOperations(transport, hive).set_state(
        bead, "review", "pending", reason="preserve prior review", actor="dev/a"
    )
    if lease_state in {"expired", "foreign_replica", "renewed"}:
        query = (
            "UPDATE leases SET lease_expires_at = DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR) "
            + f"WHERE issue_id = '{bead}'"
        )
    elif lease_state == "within_grace":
        query = (
            "UPDATE leases SET lease_expires_at = DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 MINUTE) "
            + f"WHERE issue_id = '{bead}'"
        )
    elif lease_state == "missing":
        query = f"DELETE FROM leases WHERE issue_id = '{bead}'"
    else:
        query = "SELECT 1"
    result = transport.run(["sql", query], hive, capture=True)
    assert result.returncode == 0, result.stderr
    if lease_state == "foreign_replica":
        result = transport.run(
            ["sql", f"UPDATE leases SET granted_node = 'replica-b' WHERE issue_id = '{bead}'"],
            hive,
            capture=True,
        )
        assert result.returncode == 0, result.stderr
    before = issues.get(bead)
    if lease_state == "renewed":
        # Renewal after the policy read must win over the stale snapshot.
        assert coordination.heartbeat(transport, hive, bead, actor="dev/a").ok
    if lease_state == "expired":
        leases.abandon(bead, actor="ops/recovery", read=before, reclaim=True)
        after = issues.get(bead)
        assert (after["status"], after.get("assignee") or "") == ("open", "")
        events = transport.json(
            [
                "sql",
                f"SELECT actor, event_type FROM events WHERE issue_id = '{bead}' "
                "AND event_type = 'lease_reclaimed'",
            ],
            hive,
        )
        assert events == [{"actor": "ops/recovery", "event_type": "lease_reclaimed"}]
        assert transport.json(
            ["sql", f"SELECT holder FROM leases WHERE issue_id = '{bead}'"], hive
        ) in ([], None)
    else:
        with pytest.raises(core.WriteFailed):
            leases.abandon(bead, actor="ops/recovery", read=before, reclaim=True)
        after = issues.get(bead)
        assert (after["status"], after.get("assignee")) == ("in_progress", "dev/a")
    assert CliStateReads(transport, hive).get_state(bead, "review") == "pending"


def test_atomic_holder_abandon_is_guarded_and_preserves_labels_on_a_lost_race(hive):
    bead = _create(hive, "holder abandon proof")
    issues, leases = CliIssues(BD, hive), CliLeases(BD, hive)
    leases.acquire(bead, actor="dev/a")
    before = issues.get(bead)
    leases.abandon(bead, actor="dev/a", read=before, reclaim=False)
    released = issues.get(bead)
    assert (released["status"], released.get("assignee") or "") == ("open", "")
    assert "review:abandoned" in released["labels"]
    leases.acquire(bead, actor="dev/b")
    with pytest.raises(core.WriteFailed) as failure:
        leases.abandon(bead, actor="dev/a", read=before, reclaim=False)
    assert failure.value.exit_code == 13
    held = issues.get(bead)
    assert (held["status"], held["assignee"]) == ("in_progress", "dev/b")


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
