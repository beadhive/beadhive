from __future__ import annotations

import concurrent.futures
import copy
import json
from pathlib import Path

import pytest

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
    assert len({run["artifacts"]["directory"] for run in runs}) == 20
    assert all(Path(run["artifacts"]["reports"]).is_dir() for run in runs)


def test_artifact_root_precedence_and_relative_rejection(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    configured = tmp_path / "configured"
    override = tmp_path / "override"

    assert validation_records.artifact_root(repo, configured) == configured
    monkeypatch.setenv("BH_VALIDATION_ARTIFACT_ROOT", str(override))
    assert validation_records.artifact_root(repo, configured) == override
    monkeypatch.delenv("BH_VALIDATION_ARTIFACT_ROOT")
    with pytest.raises(ValueError, match="absolute"):
        validation_records.artifact_root(repo, "relative/artifacts")

    run = validation_records.begin_run(
        repo,
        bead="b",
        phase="check",
        branch="b",
        worktree=repo,
        sha="s",
        tree="t",
        command_hash="h",
        artifact_root_config=configured,
    )
    assert Path(run["artifacts"]["directory"]).parent == configured
    assert Path(run["artifacts"]["reports"]).parent == Path(run["artifacts"]["directory"])
    assert Path(run["artifacts"]["gate_log"]).parent == Path(run["artifacts"]["directory"])

    # The environment-root form is the CI upload staging path, but its run layout
    # is deliberately byte-for-byte the configured external-root layout.
    monkeypatch.setenv("BH_VALIDATION_ARTIFACT_ROOT", str(override))
    overridden = _begin(repo, 1)
    assert Path(overridden["artifacts"]["directory"]).parent == override
    assert Path(overridden["artifacts"]["reports"]).parent == Path(
        overridden["artifacts"]["directory"]
    )


def test_run_manifest_references_complete_default_artifact_directory(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")

    run = _begin(repo)

    artifacts = run["artifacts"]
    assert Path(artifacts["directory"]).is_absolute()
    assert Path(artifacts["reports"]).is_dir()
    assert Path(artifacts["reports"]).parent == Path(artifacts["directory"])
    assert Path(artifacts["gate_log"]).parent == Path(artifacts["directory"])
    assert ".git" not in Path(artifacts["directory"]).parts


def test_upload_handoff_prunes_superseded_raw_but_protects_retry_history(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    first, second = _begin(repo), _begin(repo, 1)
    for run, rc in ((first, 1), (second, 0)):
        Path(run["artifacts"]["gate_log"]).write_text("gate")
        validation_records.finish_run(repo, run["run_id"], exit_code=rc)
        validation_records.mark_artifacts_uploaded(repo, run["run_id"])

    assert not Path(first["artifacts"]["directory"]).exists()
    assert Path(second["artifacts"]["directory"]).is_dir()
    assert Path(second["artifacts"]["gate_log"]).is_file()
    retired = validation_records.read_run(repo, first["run_id"])["artifacts"]
    # The control manifest remains for history, but cannot point at a removed raw
    # directory. Thus every retained manifest which *does* reference artifacts
    # protects a real directory.
    assert set(retired) == {"pruned_at"}


def test_verdict_pointer_protects_but_audit_use_does_not_pin_raw_artifacts(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    run = _begin(repo)
    validation_records.record_use(
        repo,
        run_id=run["run_id"],
        bead="bh-x",
        phase="submit",
        branch="b",
        worktree=repo,
        sha="sha",
        tree="tree",
        command_hash="hash",
        reused=False,
    )
    run = validation_records.finish_run(repo, run["run_id"], exit_code=0)
    verdict = repo / ".git/bh/validation/verdicts/tree/hash.json"
    verdict.parent.mkdir(parents=True)
    verdict.write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": run["run_id"],
                "tree": "tree",
                "command_hash": "hash",
                "rc": 0,
                "at": 1.0,
                "sha": "sha",
                "shas": ["sha", "same-tree-merge-sha"],
                "host": "host",
            }
        )
    )
    assert validation_records._verdict_run_ids(repo / ".git/bh/validation") == {run["run_id"]}
    validation_records.mark_artifacts_uploaded(repo, run["run_id"])

    assert Path(run["artifacts"]["directory"]).is_dir()
    verdict.unlink()
    assert validation_records.prune_artifacts(repo) == 1
    assert not Path(run["artifacts"]["directory"]).exists()


def test_legacy_manifest_without_artifacts_never_resolves_to_the_current_directory(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    run = _begin(repo)
    run = validation_records.finish_run(repo, run["run_id"], exit_code=0)
    run.pop("artifacts")
    run["artifacts_uploaded_at"] = validation_records._now()
    validation_records._atomic_json(
        repo / ".git/bh/validation/runs" / run["run_id"] / "manifest.json", run
    )
    removals = []
    monkeypatch.setattr(
        validation_records.shutil, "rmtree", lambda path, **_kwargs: removals.append(path)
    )

    assert validation_records.prune_artifacts(repo) == 0
    assert removals == []


def test_retained_manifest_and_running_artifacts_cannot_be_pruned(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(host, "host_id", lambda: "host")

    retained = _begin(repo)
    validation_records.finish_run(repo, retained["run_id"], exit_code=0)
    # Handoff is the explicit permission to prune raw output. Until it occurs,
    # the retained run manifest remains an artifact reference and is protected.
    assert validation_records.prune_artifacts(repo) == 0
    assert Path(retained["artifacts"]["directory"]).is_dir()

    running = _begin(repo, 1)
    validation_records.mark_artifacts_uploaded(repo, running["run_id"])
    assert Path(running["artifacts"]["directory"]).is_dir()


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


def test_qualifying_green_requires_an_exact_authentic_completed_manifest():
    valid = {
        "schema": 1,
        "run_id": "run-1",
        "lifecycle": "completed",
        "verdict": "green",
        "exit_code": 0,
        "signal": None,
        "reason": "command_exit",
    }
    assert validation_records.is_qualifying_green(valid)

    contradictions = (
        {"schema": True},
        {"exit_code": False},
        {"lifecycle": "running"},
        {"verdict": "red"},
        {"verdict": "none"},
        {"exit_code": 1},
        {"signal": 15},
        {"reason": "missing_binary"},
        {"reason": "checkout_failure"},
        {"reason": "setup_failure"},
        {"reason": "interrupted"},
        {"reason": "owner_dead"},
    )
    for contradiction in contradictions:
        candidate = copy.deepcopy(valid)
        candidate.update(contradiction)
        assert not validation_records.is_qualifying_green(candidate)


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
