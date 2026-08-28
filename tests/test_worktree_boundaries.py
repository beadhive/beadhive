"""Characterization matrices for the worktree verification extraction."""

from __future__ import annotations

import ast
import inspect
import json
import os
from types import SimpleNamespace

import pytest

from beadhive import worktree, worktree_git, worktree_verify

VERIFY_OPERATIONS = (
    "_rules",
    "run_init",
    "_pid_alive",
    "_pid_state",
    "_pid_start",
    "_pid_starts",
    "_verify_marker_root",
    "_verify_marker_path",
    "_write_verify_marker",
    "_read_verify_marker",
    "_remove_verify_marker",
    "_verify_dir_is_orphan",
    "_verify_dir_candidates",
    "_live_marker_pids",
    "sweep_verify_dirs",
    "_branch_sha",
    "_create_verify_dir",
    "_color_neutral_env",
    "_reuse_verdict_hit",
    "_prepare_verify_worktree",
    "clean_checkout",
)

GIT_OPERATIONS = (
    "_run_git",
    "history",
    "signature_status",
    "commit_messages",
    "commit_shas",
    "push_branch",
    "is_clean",
    "dirty_paths",
    "current_branch",
    "head_sha",
    "head_full_sha",
    "base_of",
    "commit_rows",
    "backup_branch",
    "_rebase_env",
    "rebase_squash",
    "rebase_autosquash",
    "rebase_onto",
    "rebase_abort",
    "reset_hard",
    "safe_to_rewrite",
    "same_tree",
    "is_merged",
    "on_first_parent_chain",
    "landed_via_merge",
    "_all_cherry_landed",
    "is_landed",
    "bead_and_parent",
    "diff_range",
    "log_range",
)


@pytest.mark.parametrize(
    ("module", "facade_attr", "operation"),
    [
        *((worktree_verify, "_worktree_verify", name) for name in VERIFY_OPERATIONS),
        *((worktree_git, "_worktree_git", name) for name in GIT_OPERATIONS),
    ],
)
def test_extracted_operations_have_one_implementation_behind_the_facade(
    module, facade_attr, operation
):
    implementation = getattr(module, f"impl_{operation}")
    facade = getattr(worktree, operation)

    assert implementation.__module__ == module.__name__
    facade_source = inspect.getsource(facade)
    assert f"{facade_attr}.impl_{operation}" in facade_source
    facade_node = ast.parse(facade_source).body[0]
    assert len(facade_node.body) == 2
    assert isinstance(facade_node.body[-1], ast.Return)


def test_creation_ensure_and_prune_ownership_stays_in_the_facade():
    for operation in ("add", "ensure", "prune"):
        assert getattr(worktree, operation).__module__ == "beadhive.worktree"
        assert not hasattr(worktree_verify, operation)
        assert not hasattr(worktree_git, operation)


@pytest.mark.parametrize(
    ("verify_only", "present", "expected"),
    [
        (False, False, ["always", "verify-only"]),
        (True, False, ["verify-only"]),
        (False, True, ["always", "matched", "verify-only"]),
        (True, True, ["verify-only"]),
    ],
)
def test_run_init_rule_filtering_matrix(tmp_path, monkeypatch, verify_only, present, expected):
    if present:
        (tmp_path / "sentinel.txt").write_text("present")
    cfg = {
        "worktrees": {
            "init": [
                {"run": "always"},
                {"run": "matched", "if_exists": "sentinel.txt"},
                {"run": "verify-only", "verify": True},
                {},
            ]
        }
    }
    calls = []
    monkeypatch.setattr(
        worktree,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(returncode=0),
    )

    worktree.run_init(cfg, {}, tmp_path, verify_only=verify_only)

    assert [command[0] for command, _ in calls] == expected
    assert all(kwargs == {"cwd": str(tmp_path), "check": False} for _, kwargs in calls)


def test_run_init_failure_summary_matrix(tmp_path, monkeypatch, capsys):
    cfg = {
        "worktrees": {
            "init": [
                {"run": "missing --flag"},
                {"run": "red --flag"},
                {"run": "green --flag"},
            ]
        }
    }

    def fake_run(command, **_kwargs):
        if command[0] == "missing":
            raise FileNotFoundError(command[0])
        return SimpleNamespace(returncode=7 if command[0] == "red" else 0)

    monkeypatch.setattr(worktree, "run", fake_run)
    worktree.run_init(cfg, {}, tmp_path)

    stderr = capsys.readouterr().err
    assert "command not found: missing --flag" in stderr
    assert "'red --flag' exited 7" in stderr
    assert "2 optional provisioning rule(s) failed" in stderr
    assert "missing --flag; red --flag" in stderr
    assert "green --flag" not in stderr


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), OSError()])
def test_pid_start_degrades_when_the_process_table_is_unavailable(monkeypatch, error):
    monkeypatch.setattr(
        worktree.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    assert worktree._pid_start(os.getpid()) == ""


def test_pid_start_degrades_on_nonzero_probe(monkeypatch):
    monkeypatch.setattr(
        worktree.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="ignored"),
    )
    assert worktree._pid_start(os.getpid()) == ""


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("Z+\n", "Z+"), ("Ssl\n", "Ssl"), ("\n", "")],
)
def test_pid_state_reads_the_process_state(monkeypatch, stdout, expected):
    monkeypatch.setattr(
        worktree_verify.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=stdout),
    )
    assert worktree._pid_state(os.getpid()) == expected


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), OSError()])
def test_pid_state_degrades_when_the_process_table_is_unavailable(monkeypatch, error):
    monkeypatch.setattr(
        worktree_verify.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    assert worktree._pid_state(os.getpid()) == ""


def test_verify_marker_regular_symlink_missing_matrix(tmp_path):
    regular = tmp_path / "verify-regular"
    regular.mkdir()
    marker = {"pid": os.getpid(), "host": "host", "branch": "main"}
    (regular / worktree.VERIFY_MARKER).write_text(json.dumps(marker))

    linked = tmp_path / "verify-linked"
    linked.mkdir()
    (linked / worktree.VERIFY_MARKER).symlink_to(regular / worktree.VERIFY_MARKER)

    missing = tmp_path / "verify-missing"
    missing.mkdir()

    assert worktree._read_verify_marker(regular) == marker
    assert worktree._read_verify_marker(linked) == marker
    assert worktree._read_verify_marker(missing) is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is POSIX-only")
def test_verify_marker_fifo_degrades_without_opening_it(tmp_path):
    directory = tmp_path / "verify-fifo"
    directory.mkdir()
    os.mkfifo(directory / worktree.VERIFY_MARKER)
    assert worktree._read_verify_marker(directory) is None
