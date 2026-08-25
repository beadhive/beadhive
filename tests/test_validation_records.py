from __future__ import annotations

import concurrent.futures
import json

from beadhive import host, validation_records


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _begin(repo, n=0):
    return validation_records.begin_run(
        repo,
        bead="bh-x",
        phase="submit",
        branch="wt/bead/issue/bh-x",
        worktree=repo,
        sha=f"sha{n}",
        tree="tree",
        command_hash="hash",
        command="check",
        owner_start="token",
    )


def test_concurrent_runs_and_repeated_uses_have_independent_identity(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda n: _begin(repo, n), range(20)))
    assert all(runs)
    assert len({run["run_id"] for run in runs}) == 20
    for run in runs:
        validation_records.finish_run(repo, run["run_id"], exit_code=0)
    uses = [
        validation_records.record_use(
            repo,
            run_id=runs[0]["run_id"],
            bead="bh-x",
            phase="merge",
            branch="b",
            worktree=repo,
            sha="sha",
            tree="tree",
            command_hash="hash",
            reused=True,
        )
        for _ in range(5)
    ]
    assert len({use["use_id"] for use in uses}) == 5
    assert len(list((repo / ".git/bh/validation/runs").iterdir())) == 20


def test_typed_outcomes_fail_closed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    run = _begin(repo)
    malformed = {"protocol": validation_records.PROTOCOL_NAME, "version": 1, "verdict": "green"}
    assert (
        validation_records.finish_run(repo, run["run_id"], exit_code=2, protocol=malformed)[
            "verdict"
        ]
        == "red"
    )
    missing = _begin(repo, 1)
    done = validation_records.finish_run(
        repo, missing["run_id"], exit_code=127, reason="missing_binary"
    )
    assert done["verdict"] == "none"
    interrupted = _begin(repo, 2)
    assert (
        validation_records.finish_run(repo, interrupted["run_id"], signal_number=15)["verdict"]
        == "none"
    )


def test_protocol_requires_exact_version_schema_and_consistency():
    valid = {
        "protocol": validation_records.PROTOCOL_NAME,
        "version": 1,
        "verdict": "none",
        "reason": "refused",
    }
    assert validation_records.parse_protocol(valid, exit_code=2) == {
        "verdict": "none",
        "reason": "refused",
    }
    assert validation_records.parse_protocol({**valid, "version": 2}, exit_code=2) is None
    assert validation_records.parse_protocol({**valid, "extra": True}, exit_code=2) is None
    assert validation_records.parse_protocol({**valid, "verdict": "green"}, exit_code=2) is None


def test_abandon_removes_active_without_touching_checkout(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    run = _begin(repo)
    sentinel = repo / "sentinel"
    sentinel.write_text("safe")
    done = validation_records.abandon_run(repo, run["run_id"])
    assert done["lifecycle"] == "abandoned" and done["verdict"] == "none"
    assert sentinel.read_text() == "safe"
    assert not (repo / ".git/bh/validation/active" / f"{run['run_id']}.json").exists()


def test_legacy_active_migration_refuses_ownership_disagreement(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    run = _begin(repo)
    active = repo / ".git/bh/validation/active"
    good = active / "verify-good.json"
    good.write_text(
        json.dumps(
            {
                "run_id": run["run_id"],
                "host": "host",
                "pid": run["owner"]["pid"],
                "pid_start": "token",
            }
        )
    )
    bad = active / "verify-bad.json"
    bad.write_text(
        json.dumps(
            {
                "run_id": run["run_id"],
                "host": "other",
                "pid": run["owner"]["pid"],
                "pid_start": "token",
            }
        )
    )
    assert validation_records.migrate_legacy_active(repo) == 1
    assert not good.exists() and bad.exists()
