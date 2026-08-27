"""0.15.x validation-state upgrade matrix and legacy-path tripwires."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from beadhive import (
    converge,
    doctor,
    host,
    triage_store,
    validation_ledger,
    validation_records,
)

_REPO = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _entry(tmp_path, monkeypatch):
    repo = tmp_path / "ws" / "github" / "org" / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked").write_text("subject\n")
    _git(repo, "add", "tracked")
    _git(repo, "commit", "-qm", "initial")
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    host.mint_if_needed()
    return {"provider": "github", "org": "org", "repo": "repo", "prefix": "or"}, repo


def _legacy_ledger(repo: Path, rows: list[object]) -> Path:
    path = repo / ".git" / validation_ledger.LEGACY_LEDGER_FILENAME
    path.write_text(json.dumps(rows) + "\n")
    return path


@pytest.mark.parametrize(
    ("rc", "age", "verdict", "signal_number", "confidence"),
    [
        (0, 30, "green", None, "legacy_exact_green"),
        (0, 2 * 24 * 60 * 60, "none", None, "legacy_non_attesting"),
        (1, 30, "red", None, "legacy_exit_code"),
        (143, 30, "none", 15, "legacy_shell_signal"),
    ],
)
def test_legacy_ledger_outcome_upgrade_matrix(
    tmp_path, monkeypatch, rc, age, verdict, signal_number, confidence
):
    entry, repo = _entry(tmp_path, monkeypatch)
    key = validation_ledger.cmd_hash("just check")
    _legacy_ledger(repo, [{"tree": "tree", "cmd_hash": key, "rc": rc, "at": time.time() - age}])

    assert validation_ledger._migrate_legacy_ledger(entry) == 1
    [manifest_path] = list((repo / ".git/bh/validation/runs").glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert (manifest["verdict"], manifest["signal"], manifest["verdict_confidence"]) == (
        verdict,
        signal_number,
        confidence,
    )
    provenance = manifest["provenance"]
    assert {key: provenance[key] for key in ("kind", "source", "source_schema", "ordinal")} == {
        "kind": "legacy_import",
        "source": ".git/bh-validation-ledger.json",
        "source_schema": "flat-ledger-v1",
        "ordinal": 0,
    }
    assert provenance["source_finished_at"]
    assert provenance["imported_at"]
    assert provenance["timestamp_normalized"] is False
    assert bool(validation_ledger.green_verdict(entry, "tree", "just check")) is (
        verdict == "green"
    )


def test_counts_only_and_multiple_shas_remain_metadata(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    shas = [f"sha-{number}" for number in range(25)]
    counts = {"tests": 4, "passed": 3, "failures": 1, "errors": 0, "skipped": 0}
    _legacy_ledger(
        repo,
        [
            {
                "tree": "tree",
                "cmd_hash": validation_ledger.cmd_hash("just check"),
                "rc": 1,
                "at": time.time(),
                "sha": shas[-1],
                "shas": shas,
                "report": counts,
            }
        ],
    )

    assert validation_ledger._migrate_legacy_ledger(entry) == 1
    [path] = list((repo / ".git/bh/validation/runs").glob("*/manifest.json"))
    run = json.loads(path.read_text())
    assert run["summary"] == {"counts": counts, "tree": "tree"}
    assert run["shas"] == shas[-validation_ledger._MAX_SHAS :]
    assert run["verdict"] == "red"


def test_future_non_attesting_legacy_run_cannot_shadow_a_real_green(tmp_path, monkeypatch):
    """Exact reviewer repro: preserve the future source fact, never its future authority."""
    entry, repo = _entry(tmp_path, monkeypatch)
    command = "just check"
    future = time.time() + 10 * 365 * 24 * 60 * 60
    _legacy_ledger(
        repo,
        [
            {
                "tree": "tree",
                "cmd_hash": validation_ledger.cmd_hash(command),
                "rc": 0,
                "at": future,
            }
        ],
    )

    assert validation_ledger.verdict(entry, "tree", command) is None
    [legacy] = validation_records.matching_runs(
        repo, tree="tree", command_hash=validation_ledger.cmd_hash(command)
    )
    assert legacy["verdict"] == "none"
    assert legacy["provenance"]["timestamp_normalized"] is True
    assert legacy["provenance"]["source_finished_at"] > legacy["finished_at"]
    assert legacy["finished_at"] == legacy["provenance"]["imported_at"]

    validation_ledger.record(entry, "tree", command, 0)
    runs = validation_records.matching_runs(
        repo, tree="tree", command_hash=validation_ledger.cmd_hash(command)
    )
    assert len(runs) == 2
    latest = validation_records.latest_run(
        repo, tree="tree", command_hash=validation_ledger.cmd_hash(command)
    )
    assert latest is not None and latest["phase"] == "validation"
    assert validation_ledger.green_verdict(entry, "tree", command) is not None
    assert validation_ledger.rebuild_verdict_index(entry) == 1
    assert validation_ledger.green_verdict(entry, "tree", command) is not None


def test_pre_fix_future_legacy_manifest_is_repaired_by_authority_ordering(tmp_path, monkeypatch):
    """The phase fallback repairs stores already migrated by the bounced implementation."""
    entry, repo = _entry(tmp_path, monkeypatch)
    command = "just check"
    _legacy_ledger(
        repo,
        [
            {
                "tree": "tree",
                "cmd_hash": validation_ledger.cmd_hash(command),
                "rc": 0,
                "at": time.time() + 10 * 365 * 24 * 60 * 60,
            }
        ],
    )
    assert validation_ledger._migrate_legacy_ledger(entry) == 1
    [legacy] = validation_records.matching_runs(
        repo, tree="tree", command_hash=validation_ledger.cmd_hash(command)
    )
    legacy.pop("provenance")
    legacy["finished_at"] = "2036-01-01T00:00:00+00:00"
    validation_records._atomic_json(
        repo / ".git/bh/validation/runs" / legacy["run_id"] / "manifest.json", legacy
    )

    validation_ledger.record(entry, "tree", command, 0)
    latest = validation_records.latest_run(
        repo, tree="tree", command_hash=validation_ledger.cmd_hash(command)
    )
    assert latest is not None and latest["phase"] == "validation"
    assert validation_ledger.rebuild_verdict_index(entry) == 1
    assert validation_ledger.green_verdict(entry, "tree", command) is not None


@pytest.mark.parametrize(
    ("name", "rc", "age"),
    [
        ("stale-zero", 0, -(2 * 24 * 60 * 60)),
        ("future-zero", 0, 10 * 365 * 24 * 60 * 60),
        ("future-red", 1, 10 * 365 * 24 * 60 * 60),
        ("future-143", 143, 10 * 365 * 24 * 60 * 60),
    ],
)
def test_canonical_execution_outranks_stale_future_red_and_interrupted_legacy(
    tmp_path, monkeypatch, name, rc, age
):
    entry, repo = _entry(tmp_path, monkeypatch)
    command = "just check"
    _legacy_ledger(
        repo,
        [
            {
                "tree": "tree",
                "cmd_hash": validation_ledger.cmd_hash(command),
                "rc": rc,
                "at": time.time() + age,
            }
        ],
    )
    assert validation_ledger._migrate_legacy_ledger(entry) == 1, name

    validation_ledger.record(entry, "tree", command, 0)
    latest = validation_records.latest_run(
        repo, tree="tree", command_hash=validation_ledger.cmd_hash(command)
    )
    assert latest is not None and latest["phase"] == "validation", name
    assert validation_ledger.green_verdict(entry, "tree", command) is not None, name
    assert validation_ledger.rebuild_verdict_index(entry) == 1, name


@pytest.mark.parametrize(
    "payload",
    ["{", json.dumps({"not": "a list"}), json.dumps([{"tree": "tree", "rc": False}])],
)
def test_malformed_ledger_never_marks_or_authors_a_run(tmp_path, monkeypatch, payload):
    entry, repo = _entry(tmp_path, monkeypatch)
    legacy = repo / ".git" / validation_ledger.LEGACY_LEDGER_FILENAME
    legacy.write_text(payload)

    assert validation_ledger._migrate_legacy_ledger(entry) == 0
    assert not list((repo / ".git/bh/validation/runs").glob("*/manifest.json"))
    if payload.startswith("{"):
        assert not (repo / ".git/bh/validation/migrations/flat-ledger-v1.json").exists()


def _case(name: str, status: str) -> dict:
    return {"test.case.name": name, "test.case.result.status": status}


def test_per_tree_retry_migration_preserves_flake_raw_files_and_subject(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    source = repo / triage_store.LEGACY_STORE_REL / tree
    source.mkdir(parents=True)
    flaky = "tests.test_upgrade::test_flaky"
    rows = [
        {
            "at": time.time() - 2,
            "sha": "sha-one",
            "cmd_hash": "command",
            "rc": 1,
            "counts": {"tests": 1, "passed": 0, "failures": 1, "errors": 0, "skipped": 0},
            "cases": [_case(flaky, "failed")],
        },
        {
            "at": time.time() - 1,
            "sha": "sha-two",
            "cmd_hash": "command",
            "rc": 0,
            "counts": {"tests": 1, "passed": 1, "failures": 0, "errors": 0, "skipped": 0},
            "cases": [_case(flaky, "passed")],
        },
    ]
    legacy_results = source / "results.json"
    legacy_results.write_text(json.dumps({"tree": tree, "runs": rows}) + "\n")
    (source / "junit.xml").write_text("<testsuite/>")
    (source / "gate.log").write_text("legacy gate\n")
    source_bytes = {path.name: path.read_bytes() for path in source.iterdir()}
    subject_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    tracked_diff = _git(repo, "diff", "--binary", "HEAD", "--")

    migrated = triage_store.runs(entry, tree)
    assert len(migrated) == 2
    assert converge.flakes(entry, tree) == [flaky]
    assert migrated[-1]["verdict_confidence"] == "legacy_triage_non_attesting"
    assert _git(repo, "rev-parse", "HEAD^{tree}") == subject_tree
    assert _git(repo, "diff", "--binary", "HEAD", "--") == tracked_diff
    assert {path.name: path.read_bytes() for path in source.iterdir()} == source_bytes

    canonical = repo / triage_store.STORE_REL / tree / "results.json"
    payload = json.loads(canonical.read_text())
    raw = repo / ".bh/validation/runs" / payload["latest_raw_run"]
    assert (raw / "reports/junit.xml").read_text() == "<testsuite/>"
    assert (raw / "gate.log").read_text() == "legacy gate\n"
    manifests = [validation_records.read_run(repo, row["run_id"]) for row in payload["runs"]]
    assert [(run["verdict"], run["verdict_confidence"]) for run in manifests] == [
        ("red", "legacy_exit_code"),
        ("none", "legacy_triage_non_attesting"),
    ]

    snapshot = {
        path.relative_to(repo): path.read_bytes()
        for path in raw.parent.rglob("*")
        if path.is_file()
    }
    assert triage_store.migrate_legacy_tree(entry, tree) == 0
    assert triage_store.runs(entry, tree) == migrated
    assert {
        path.relative_to(repo): path.read_bytes()
        for path in raw.parent.rglob("*")
        if path.is_file()
    } == snapshot


def test_missing_counts_reports_and_malformed_triage_degrade_independently(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    assert triage_store.runs(entry, "missing") == []

    counts_tree = "counts-tree"
    counts_source = repo / triage_store.LEGACY_STORE_REL / counts_tree
    counts_source.mkdir(parents=True)
    (counts_source / "results.json").write_text(
        json.dumps(
            {
                "tree": counts_tree,
                "runs": [
                    {
                        "at": time.time(),
                        "sha": "sha",
                        "cmd_hash": "command",
                        "rc": 1,
                        "counts": {"tests": 2, "failures": 1},
                    }
                ],
            }
        )
    )
    [run] = triage_store.runs(entry, counts_tree)
    assert run["counts"] == {"tests": 2, "failures": 1}
    assert run["cases"] == []
    assert "latest_raw_run" not in json.loads(
        (repo / triage_store.STORE_REL / counts_tree / "results.json").read_text()
    )

    malformed_tree = "malformed-tree"
    malformed = repo / triage_store.LEGACY_STORE_REL / malformed_tree
    malformed.mkdir(parents=True)
    (malformed / "results.json").write_text("{")
    assert triage_store.runs(entry, malformed_tree) == []
    assert not (repo / triage_store.STORE_REL / malformed_tree).exists()


def test_read_only_style_migration_miss_falls_back_to_exact_legacy_tree(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    tree = "fallback-tree"
    source = repo / triage_store.LEGACY_STORE_REL / tree
    source.mkdir(parents=True)
    expected = {
        "at": time.time(),
        "sha": "sha",
        "cmd_hash": "command",
        "rc": 1,
        "counts": None,
        "cases": [],
    }
    (source / "results.json").write_text(json.dumps({"tree": tree, "runs": [expected]}))
    monkeypatch.setattr(triage_store, "migrate_legacy_tree", lambda _entry, _tree: 0)

    assert triage_store.runs(entry, tree) == [expected]
    assert not (repo / triage_store.STORE_REL / tree).exists()


def test_partial_best_effort_triage_import_retries_without_duplicates(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    tree = "retry-tree"
    source = repo / triage_store.LEGACY_STORE_REL / tree
    source.mkdir(parents=True)
    rows = [
        {"at": time.time() - 1, "sha": "one", "cmd_hash": "command", "rc": 1},
        {"at": time.time(), "sha": "two", "cmd_hash": "command", "rc": 0},
    ]
    (source / "results.json").write_text(json.dumps({"tree": tree, "runs": rows}))
    real_atomic = validation_records._atomic_json
    failed = False

    def fail_second_manifest(path, value):
        nonlocal failed
        if path.name == "manifest.json" and value["sha"] == "two" and not failed:
            failed = True
            raise OSError("simulated private-store interruption")
        real_atomic(path, value)

    monkeypatch.setattr(validation_records, "_atomic_json", fail_second_manifest)
    assert triage_store.migrate_legacy_tree(entry, tree) == 1
    assert not (repo / triage_store.STORE_REL / tree / "results.json").exists()

    monkeypatch.setattr(validation_records, "_atomic_json", real_atomic)
    assert triage_store.migrate_legacy_tree(entry, tree) == 1
    imported = triage_store.runs(entry, tree)
    assert len(imported) == 2
    assert len({row["run_id"] for row in imported}) == 2


def test_legacy_tree_identity_cannot_escape_either_private_root(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    for unsafe in ("../outside", "..", "nested/tree", r"nested\tree"):
        assert triage_store.tree_dir(entry, unsafe) is None
        assert triage_store.legacy_tree_dir(entry, unsafe) is None
        assert triage_store.migrate_legacy_tree(entry, unsafe) == 0
    assert not (repo / ".bh/validation/outside").exists()


def test_ledger_completion_marker_waits_for_atomic_verdict_sync(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    _legacy_ledger(
        repo,
        [
            {
                "tree": "tree",
                "cmd_hash": validation_ledger.cmd_hash("just check"),
                "rc": 0,
                "at": time.time(),
            }
        ],
    )
    marker = repo / ".git/bh/validation/migrations/flat-ledger-v1.json"
    real_sync = validation_ledger._sync_index
    monkeypatch.setattr(validation_ledger, "_sync_index", lambda *_args: False)

    assert validation_ledger._migrate_legacy_ledger(entry) == 1
    assert not marker.exists()

    monkeypatch.setattr(validation_ledger, "_sync_index", real_sync)
    assert validation_ledger._migrate_legacy_ledger(entry) == 0
    assert marker.is_file()
    assert validation_ledger.green_verdict(entry, "tree", "just check") is not None


def test_canonical_paths_win_and_writers_never_recreate_legacy_paths(tmp_path, monkeypatch):
    entry, repo = _entry(tmp_path, monkeypatch)
    validation_ledger.record(entry, "tree", "just check", 0)
    assert not (repo / ".git" / validation_ledger.LEGACY_LEDGER_FILENAME).exists()
    triage_store.store(entry, "tree", "just check", 1, None, None, None)
    assert not (repo / triage_store.LEGACY_STORE_REL).exists()
    assert (repo / triage_store.STORE_REL / "tree/results.json").is_file()

    # Once canonical truth exists, a newly appearing legacy input is not even consulted.
    _legacy_ledger(repo, [{"tree": "tree", "cmd_hash": "wrong", "rc": 0, "at": time.time()}])
    monkeypatch.setattr(
        validation_ledger,
        "_migrate_legacy_ledger",
        lambda _entry: pytest.fail("canonical verdict must win before fallback"),
    )
    assert validation_ledger.green_verdict(entry, "tree", "just check") is not None


def test_doctor_warns_for_exact_two_minor_legacy_window(tmp_path, monkeypatch):
    entry, path = _entry(tmp_path, monkeypatch)
    (path / ".beads").mkdir()
    (path / ".git/bh-validation-ledger.json").write_text("[]")
    (path / ".bh/testreport").mkdir(parents=True)

    [warning] = doctor._legacy_validation_warnings(entry, path)
    assert "0.16.x and 0.17.x" in warning
    assert "removed in 0.18.0" in warning
    assert "docs/UPGRADING.md" in warning
    warnings = doctor._data_warnings({}, tmp_path / "ws", [entry], set(), set(), set(), set())
    assert warning in warnings


def test_compatibility_removal_tripwire_keeps_code_and_docs_in_lockstep(tmp_path, monkeypatch):
    # xdist workers can execute this after tests that temporarily run from another checkout.
    # Repository-shape assertions must not inherit the worker's process-global cwd.
    monkeypatch.chdir(tmp_path)
    source = (_REPO / "src/beadhive/validation_ledger.py").read_text()
    triage = (_REPO / "src/beadhive/triage_store.py").read_text()
    upgrading = (_REPO / "docs/UPGRADING.md").read_text()
    upgrading_words = " ".join(upgrading.split())

    assert 'LEGACY_LEDGER_FILENAME = "bh-validation-ledger.json"' in source
    assert 'LEGACY_STORE_REL = ".bh/testreport"' in triage
    assert "0.16.x and 0.17.x" in upgrading_words
    assert "0.18.0 or later only" in upgrading_words
