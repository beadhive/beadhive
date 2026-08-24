"""Characterization matrices for the worktree verification extraction."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from beadhive import worktree


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
        lambda command, **kwargs: calls.append((command, kwargs))
        or SimpleNamespace(returncode=0),
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
