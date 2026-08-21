"""Real-bd regressions for blocker-correct molecule readiness (bh-a74aa.1).

The M4 regression cannot be proved by a fake: it is specifically a disagreement between two bd
query paths over a mixed persistent/ephemeral graph.  These tests build that graph in a disposable
embedded hive and exercise the bh payload producer against the installed bd binary.
"""

from __future__ import annotations

import pytest

from beadhive import work
from harness.beads import bd, init_embedded, skip_if_no_bd

pytestmark = [pytest.mark.integration, skip_if_no_bd]


def _create(hive, title, *, ephemeral=False, parent="") -> str:
    args = ["create", title, "--type", "task", "--silent"]
    if ephemeral:
        args.append("--ephemeral")
    if parent:
        args.extend(["--parent", parent])
    res = bd(*args, cwd=hive, capture=True)
    return (res.stdout or "").strip().splitlines()[-1]


def _molecule(hive, title, *, ephemeral=False) -> str:
    args = ["create", title, "--type", "epic", "--silent"]
    if ephemeral:
        args.append("--ephemeral")
    res = bd(*args, cwd=hive, capture=True)
    return (res.stdout or "").strip().splitlines()[-1]


def _hive(tmp_path, prefix):
    init_embedded(tmp_path, prefix)
    return tmp_path


def test_m4_open_persistent_gate_blocks_ephemeral_release_step(tmp_path):
    """Exact M4 topology: release wisp, three satisfied predecessors, persistent human gate."""
    hive = _hive(tmp_path, "m4ready")
    molecule = _molecule(hive, "release run", ephemeral=True)
    attest = _create(hive, "attest", ephemeral=True, parent=molecule)
    bump = _create(hive, "bump", ephemeral=True, parent=molecule)
    preview = _create(hive, "preview", ephemeral=True, parent=molecule)
    release = _create(hive, "release one-way door", ephemeral=True, parent=molecule)
    bd("dep", "add", bump, "--depends-on", attest, cwd=hive, capture=True)
    bd("dep", "add", preview, "--depends-on", bump, cwd=hive, capture=True)
    bd("dep", "add", release, "--depends-on", preview, cwd=hive, capture=True)
    external = _create(hive, "unrelated consumer")
    # A reverse dependency-tree walk includes this bead even though it is NOT a molecule child.
    # Membership must come from the parent-child edge, not reachability from the molecule root.
    bd("dep", "add", external, "--depends-on", preview, cwd=hive, capture=True)
    for step in (attest, bump, preview):
        bd("close", step, "--reason", "measured complete", cwd=hive, capture=True)
    gate = bd(
        "gate",
        "create",
        "--type",
        "human",
        "--blocks",
        release,
        "--reason",
        "releaser sign-off",
        "--title",
        "Gate: releaser sign-off",
        cwd=hive,
        capture=True,
    )

    payload = work.molecule_readiness_payload(molecule, hive)
    release_row = next(row for row in payload["steps"] if row["id"] == release)

    assert {row["id"] for row in payload["steps"]} == {attest, bump, preview, release}
    assert external not in {row["id"] for row in payload["steps"]}
    assert release_row["readiness"] == "blocked"
    assert len(release_row["blocked_by"]) == 1
    assert release_row["blocked_by"][0]["id"] in (gate.stdout or "")


def test_satisfied_persistent_molecule_step_remains_ready(tmp_path):
    """Ordinary all-persistent molecules retain the ready answer users already expect."""
    hive = _hive(tmp_path, "persistready")
    molecule = _molecule(hive, "persistent run")
    predecessor = _create(hive, "done first", parent=molecule)
    step = _create(hive, "ready next", parent=molecule)
    bd("dep", "add", step, "--depends-on", predecessor, cwd=hive, capture=True)
    bd("close", predecessor, "--reason", "done", cwd=hive, capture=True)

    payload = work.molecule_readiness_payload(molecule, hive)
    step_row = next(row for row in payload["steps"] if row["id"] == step)

    assert {row["id"] for row in payload["steps"]} == {predecessor, step}
    assert step_row["readiness"] == "ready"
    assert step_row["blocked_by"] == []
