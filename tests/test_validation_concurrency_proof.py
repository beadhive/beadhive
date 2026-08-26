"""Incident-shaped proof for the unified validation gate.

These tests use real git worktrees, real OS processes, production flocks, durable validation
records, and a tiny sentinel command.  They stop at submit's clean-checkout boundary so no Dolt
fixture is needed: ``phase=submit`` and the bead/branch identities are the same inputs the CLI
passes after its lifecycle preflight.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beadhive import host, validation_admission, validation_ledger, validation_records, worktree


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def _read_state(root: Path) -> dict:
    path = root / "state.json"
    if not path.is_file():
        return {"active": 0, "peak": 0, "children": {}}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"active": 0, "peak": 0, "children": {}}


def _run_submit_gate(entry, branch, command, cfg, bead, results) -> None:
    rc = worktree.clean_checkout(
        entry,
        branch,
        command,
        cfg=cfg,
        reuse=True,
        bead=bead,
        phase="submit",
    )
    results.put((bead, rc))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return False


@pytest.fixture
def proof_hive(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    main = workspace / "github" / "proof" / "hive"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    _git(main, "config", "user.name", "proof")
    _git(main, "config", "user.email", "proof@example.test")
    (main / "identity.txt").write_text("base\n")
    _git(main, "add", "identity.txt")
    _git(main, "commit", "-qm", "chore: seed proof")
    for bead, identity in (("proof-a", "A"), ("proof-b", "B")):
        branch = f"wt/bead/issue/{bead}"
        _git(main, "checkout", "-qb", branch, "main")
        (main / "identity.txt").write_text(f"{identity}\n")
        _git(main, "commit", "-qam", f"feat: identity {identity}")
    _git(main, "checkout", "-q", "main")

    sentinel = tmp_path / "validation_sentinel.py"
    sentinel.write_text(
        """\
import atexit, fcntl, json, os, signal, subprocess, sys, time
from pathlib import Path

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
identity = Path('identity.txt').read_text().strip()
lock_path = root / 'state.lock'
state_path = root / 'state.json'
left = False
signalled = False
worker = subprocess.Popen([
    sys.executable,
    '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)',
])
(root / f'pid-worker-{identity}-{worker.pid}').write_text(str(worker.pid))

def change(delta):
    with lock_path.open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {
                'active': 0, 'peak': 0, 'children': {}}
            state['active'] += delta
            if delta > 0:
                state['peak'] = max(state['peak'], state['active'])
                state['children'][identity] = state['children'].get(identity, 0) + 1
            state_path.write_text(json.dumps(state, sort_keys=True))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def leave(*_args):
    global left, signalled
    signalled = signalled or bool(_args)
    if not left:
        left = True
        change(-1)
    if not signalled and worker.poll() is None:
        worker.kill()
        worker.wait()
    if _args:
        raise SystemExit(128 + signal.SIGTERM)

change(1)
(root / f'pid-{identity}-{os.getpid()}').write_text(str(os.getpid()))
atexit.register(leave)
signal.signal(signal.SIGTERM, leave)
while not (root / 'release').exists():
    time.sleep(0.02)
"""
    )
    entry = {
        "provider": "github",
        "org": "proof",
        "repo": "hive",
        "prefix": "proof",
        "kind": "personal",
    }
    monkeypatch.setenv("GIT_WORKSPACE", str(workspace))
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "worktrees"))
    monkeypatch.setenv("BH_VALIDATION_SLOT_ROOT", str(tmp_path / "host-slots"))
    monkeypatch.setattr(host, "host_id", lambda: "proof-host")
    command = f"{sys.executable} {sentinel} {tmp_path / 'sentinel-state'}"
    return entry, main, command, tmp_path / "sentinel-state", tmp_path / "worktrees"


@pytest.mark.parametrize("slots", [1, 2])
def test_two_submits_and_duplicate_retry_obey_capacity_and_coalesce(proof_hive, slots):
    entry, main, command, state_root, worktrees_root = proof_hive
    cfg = {"work": {"validation_slots": slots}}
    ctx = mp.get_context("fork")
    results = ctx.Queue()
    branch_a = "wt/bead/issue/proof-a"
    branch_b = "wt/bead/issue/proof-b"

    leader = ctx.Process(
        target=_run_submit_gate,
        args=(entry, branch_a, command, cfg, "proof-a", results),
    )
    leader.start()
    _wait_for(lambda: _read_state(state_root)["children"].get("A") == 1)
    other = ctx.Process(
        target=_run_submit_gate,
        args=(entry, branch_b, command, cfg, "proof-b", results),
    )
    duplicate = ctx.Process(
        target=_run_submit_gate,
        args=(entry, branch_a, command, cfg, "proof-a", results),
    )
    other.start()
    duplicate.start()
    if slots == 2:
        _wait_for(lambda: _read_state(state_root)["peak"] == 2)
    else:
        time.sleep(0.2)
        assert _read_state(state_root)["peak"] == 1
    (state_root / "release").touch()

    for process in (leader, other, duplicate):
        process.join(15)
        assert process.exitcode == 0
    assert sorted(results.get(timeout=1) for _ in range(3)) == [
        ("proof-a", 0),
        ("proof-a", 0),
        ("proof-b", 0),
    ]
    state = _read_state(state_root)
    assert state["peak"] == slots
    assert state["active"] == 0
    assert state["children"] == {"A": 1, "B": 1}

    validation_root = main / ".git" / "bh" / "validation"
    uses = [json.loads(path.read_text()) for path in (validation_root / "uses").glob("*.json")]
    assert sum(use.get("coalesced") is True for use in uses) == 1
    assert not list((validation_root / "active").glob("*.json"))
    verify_parent = worktrees_root / "github" / "proof" / "hive"
    assert not list(verify_parent.glob("verify-*"))

    tree = validation_ledger.tree_of(entry, _git(main, "rev-parse", branch_a))
    with validation_admission.identity_lock(main, tree, validation_ledger.cmd_hash(command)):
        pass
    with validation_admission.host_slot(
        cfg, entry, phase="submit", root=Path(os.environ["BH_VALIDATION_SLOT_ROOT"])
    ):
        pass


@pytest.mark.skipif(sys.platform != "linux", reason="owner-death containment uses PDEATHSIG")
def test_forced_submit_owner_death_reaps_child_checkout_record_and_locks(proof_hive):
    entry, main, command, state_root, worktrees_root = proof_hive
    cfg = {"work": {"validation_slots": 1}}
    ctx = mp.get_context("fork")
    discarded = ctx.Queue()
    branch = "wt/bead/issue/proof-a"
    owner = ctx.Process(
        target=_run_submit_gate,
        args=(entry, branch, command, cfg, "proof-a", discarded),
    )
    owner.start()
    _wait_for(lambda: _read_state(state_root)["children"].get("A") == 1)
    pid_marker = next(state_root.glob("pid-A-*"))
    child_pid = int(pid_marker.read_text())
    worker_marker = next(state_root.glob("pid-worker-A-*"))
    worker_pid = int(worker_marker.read_text())
    assert _alive(child_pid)
    assert _alive(worker_pid)

    os.kill(owner.pid, signal.SIGKILL)
    owner.join(10)
    assert owner.exitcode == -signal.SIGKILL
    _wait_for(lambda: not _alive(child_pid))
    _wait_for(lambda: not _alive(worker_pid))
    _wait_for(lambda: _read_state(state_root)["active"] == 0)

    replacement_results = ctx.Queue()
    replacement = ctx.Process(
        target=_run_submit_gate,
        args=(entry, branch, command, cfg, "proof-a", replacement_results),
    )
    replacement.start()
    _wait_for(lambda: _read_state(state_root)["children"].get("A") == 2)
    (state_root / "release").touch()
    replacement.join(15)
    assert replacement.exitcode == 0
    assert replacement_results.get(timeout=1) == ("proof-a", 0)

    tree = validation_ledger.tree_of(entry, _git(main, "rev-parse", branch))
    runs = validation_records.matching_runs(
        main, tree=tree, command_hash=validation_ledger.cmd_hash(command)
    )
    assert {run["lifecycle"] for run in runs} == {"abandoned", "completed"}
    abandoned = next(run for run in runs if run["lifecycle"] == "abandoned")
    completed = next(run for run in runs if run["lifecycle"] == "completed")
    assert abandoned["reason"] == "owner_dead"
    assert completed["verdict"] == "green"
    assert not validation_records.running_runs(main)
    verify_parent = worktrees_root / "github" / "proof" / "hive"
    assert not list(verify_parent.glob("verify-*"))
    active = main / ".git" / "bh" / "validation" / "active"
    assert not list(active.glob("*.json"))
    with validation_admission.identity_lock(main, tree, validation_ledger.cmd_hash(command)):
        pass
    with validation_admission.host_slot(
        cfg, entry, phase="submit", root=Path(os.environ["BH_VALIDATION_SLOT_ROOT"])
    ):
        pass
