"""Derived verdict pointers are never authority without their run manifests."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from beadhive import host, validation_ledger, validation_records


def _entry(tmp_path, monkeypatch):
    repo = tmp_path / "ws" / "github" / "org" / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    host.mint_if_needed()
    return {"provider": "github", "org": "org", "repo": "repo"}, repo


def _path(entry, rev="tree"):
    return validation_ledger._verdict_path(entry, rev, validation_ledger.cmd_hash("just check"))


def test_index_rebuild_is_manifest_authoritative_and_revokes_non_green(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    validation_ledger.record(entry, "tree", "just check", 0)
    pointer = _path(entry)
    assert pointer is not None and pointer.is_file()
    assert validation_ledger.green_verdict(entry, "tree", "just check") is not None

    # A later red execution removes the old green pointer; rebuilding reaches the same meaning.
    validation_ledger.record(entry, "tree", "just check", 1)
    assert validation_ledger.green_verdict(entry, "tree", "just check") is None
    assert validation_ledger.rebuild_verdict_index(entry) == 0
    assert not pointer.exists()

    # A corrupt or future-dated pointer cannot manufacture a pass either.
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("{")
    assert validation_ledger.green_verdict(entry, "tree", "just check") is None
    pointer.write_text(json.dumps({"tree": "tree", "command_hash": "wrong", "at": time.time()}))
    assert validation_ledger.green_verdict(entry, "tree", "just check") is None


def test_index_rebuild_restores_green_and_requires_referenced_completed_run(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    validation_ledger.record(entry, "tree", "just check", 0)
    pointer = _path(entry)
    assert pointer is not None
    pointer.unlink()
    assert validation_ledger.rebuild_verdict_index(entry) == 1
    payload = json.loads(pointer.read_text())
    assert payload["run_id"].startswith("run-") and payload["rc"] == 0
    # A pointer to no manifest is only a cache miss, never a green verdict.
    payload["run_id"] = "run-missing"
    pointer.write_text(json.dumps(payload))
    assert validation_ledger.green_verdict(entry, "tree", "just check") is None


def test_legacy_flat_rows_import_once_as_manifests_and_green_pointer(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    legacy = repo / ".git" / validation_ledger.LEGACY_LEDGER_FILENAME
    observed = [f"legacy-sha-{index:02d}" for index in range(22)]
    legacy.write_text(
        json.dumps(
            [
                {
                    "tree": "tree",
                    "cmd_hash": validation_ledger.cmd_hash("just check"),
                    "rc": 0,
                    "at": time.time() - 60,
                    "sha": observed[-1],
                    "shas": observed,
                    "host": host.host_id(),
                }
            ]
        )
    )

    hit = validation_ledger.green_verdict(entry, "tree", "just check")
    assert hit is not None and hit["run_id"].startswith("run-legacy-")
    assert hit["sha"] == observed[-1]
    assert hit["shas"] == observed[-validation_ledger._MAX_SHAS :]
    run = validation_records.read_run(repo, hit["run_id"])
    assert run is not None
    assert (run["lifecycle"], run["verdict"], run["reason"]) == (
        "completed",
        "green",
        "legacy_ledger_import",
    )
    assert run["shas"] == observed[-validation_ledger._MAX_SHAS :]

    # Rebuilding from the retained manifest preserves the complete capped observation window.
    pointer = _path(entry)
    assert pointer is not None
    pointer.unlink()
    assert validation_ledger.rebuild_verdict_index(entry) == 1
    assert json.loads(pointer.read_text())["shas"] == observed[-validation_ledger._MAX_SHAS :]

    # Migration completion freezes the compatibility input: later edits cannot author runs.
    legacy.write_text("[]")
    assert validation_ledger._migrate_legacy_ledger(entry) == 0
    assert validation_ledger.green_verdict(entry, "tree", "just check") is not None


def test_legacy_red_revokes_green_and_corrupt_input_stays_retryable(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    legacy = repo / ".git" / validation_ledger.LEGACY_LEDGER_FILENAME
    key = validation_ledger.cmd_hash("just check")
    legacy.write_text("{")
    assert validation_ledger._migrate_legacy_ledger(entry) == 0
    marker = repo / ".git/bh/validation/migrations/flat-ledger-v1.json"
    assert not marker.exists()

    legacy.write_text(
        json.dumps(
            [
                {"tree": "tree", "cmd_hash": key, "rc": 0, "at": time.time() - 2},
                {"tree": "tree", "cmd_hash": key, "rc": 1, "at": time.time() - 1},
            ]
        )
    )
    assert validation_ledger._migrate_legacy_ledger(entry) == 2
    assert marker.is_file()
    assert validation_ledger.verdict(entry, "tree", "just check")["rc"] == 1
    assert validation_ledger.green_verdict(entry, "tree", "just check") is None
    assert not _path(entry).exists()


def test_corrupt_green_manifest_with_nonzero_exit_cannot_rebuild_pointer(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    validation_ledger.record(entry, "tree", "just check", 0)
    pointer = _path(entry)
    assert pointer is not None and pointer.is_file()
    payload = json.loads(pointer.read_text())
    run = validation_records.read_run(repo, payload["run_id"])
    assert run is not None
    run["exit_code"] = 1  # contradictory: corrupt state, never qualifying green
    validation_records._atomic_json(
        repo / ".git/bh/validation/runs" / run["run_id"] / "manifest.json", run
    )

    assert validation_ledger.rebuild_verdict_index(entry) == 0
    assert not pointer.exists()
    assert validation_ledger.verdict(entry, "tree", "just check") is None


@pytest.mark.parametrize(
    "contradiction",
    [
        {"signal": 15},
        {"schema": True},
        {"exit_code": False},
        {"reason": "setup_failure"},
    ],
)
def test_contradictory_green_manifest_never_qualifies(tmp_path, monkeypatch, contradiction):
    entry, repo = _entry(tmp_path, monkeypatch)
    validation_ledger.record(entry, "tree", "just check", 0)
    pointer = _path(entry)
    assert pointer is not None
    payload = json.loads(pointer.read_text())
    run = validation_records.read_run(repo, payload["run_id"])
    assert run is not None
    run.update(contradiction)
    validation_records._atomic_json(
        repo / ".git/bh/validation/runs" / run["run_id"] / "manifest.json", run
    )

    assert validation_ledger.rebuild_verdict_index(entry) == 0
    assert validation_ledger.green_verdict(entry, "tree", "just check") is None
    assert not pointer.exists()


@pytest.mark.parametrize("typed_verdict", ["red", "none"])
def test_typed_non_green_with_exit_zero_never_qualifies(tmp_path, monkeypatch, typed_verdict):
    entry, repo = _entry(tmp_path, monkeypatch)
    command = "just check"
    run = validation_records.begin_run(
        repo,
        bead="bh-x",
        phase="release",
        branch="main",
        worktree=repo,
        sha="tree",
        tree="tree",
        command_hash=validation_ledger.cmd_hash(command),
        command=command,
    )
    assert run is not None
    done = validation_records.finish_run(
        repo,
        run["run_id"],
        exit_code=0,
        protocol={
            "protocol": validation_records.PROTOCOL_NAME,
            "version": 1,
            "verdict": typed_verdict,
            "reason": "runner refused",
        },
    )
    assert done is not None and done["exit_code"] == 0
    validation_ledger.record(entry, "tree", command, 0, run_id=run["run_id"])

    hit = validation_ledger.verdict(entry, "tree", command)
    assert hit is not None and hit["rc"] == 0 and hit["verdict"] == typed_verdict
    assert not validation_ledger.is_qualifying_green(hit)
    assert validation_ledger.green_verdict(entry, "tree", command) is None
