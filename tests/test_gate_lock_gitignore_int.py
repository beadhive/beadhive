"""Keep bd's gate-lock ignore check from dirtying a furnished hive clone."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.beads import embedded_env, skip_if_no_bd

pytestmark = [pytest.mark.integration, skip_if_no_bd]

ROOT = Path(__file__).resolve().parents[1]


def _run(repo: Path, *args: str, env=None) -> str:
    result = subprocess.run(
        args, cwd=repo, env=env, check=True, capture_output=True, text=True, timeout=120
    )
    return result.stdout.strip()


def test_bd_read_leaves_furnished_hive_clone_clean(tmp_path: Path) -> None:
    repo = tmp_path / "hive"
    repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.name", "Test")
    _run(repo, "git", "config", "user.email", "test@example.com")

    # Initialize a real local store, then furnish it with this hive's tracked ignore files.
    env = {**embedded_env(), "BD_NON_INTERACTIVE": "1"}
    _run(
        repo,
        "bd",
        "init",
        "--prefix",
        "gate",
        "--non-interactive",
        "--skip-agents",
        "--skip-hooks",
        env=env,
    )
    shutil.copyfile(ROOT / ".gitignore", repo / ".gitignore")
    shutil.copyfile(ROOT / ".beads/.gitignore", repo / ".beads/.gitignore")
    assert "*.gate.lock*" in (repo / ".beads/.gitignore").read_text()
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-qm", "chore: furnish hive")
    assert _run(repo, "git", "status", "--porcelain") == ""

    # The real lock lives at the repo root; the nested pattern cannot cover it.
    ignored_by = _run(repo, "git", "check-ignore", "-v", ".beads.gate.lock")
    assert ignored_by.startswith(".gitignore:")
    assert ":.beads.gate.lock\t.beads.gate.lock" in ignored_by

    # bd used to append its own *.gate.lock* block here on an ordinary read.
    _run(repo, "bd", "-C", str(repo), "ready", "--json", env=env)
    assert _run(repo, "git", "status", "--porcelain") == ""
