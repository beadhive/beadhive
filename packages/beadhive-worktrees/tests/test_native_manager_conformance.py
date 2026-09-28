"""The native Git ``worktree.manager`` passes the provider conformance kit against real git.

This is the reference run of :data:`beadhive_worktrees.testing.MANAGER_CASES`: a real repository
in ``tmp_path``, the package's own :class:`NativeGitWorktreeManager`, and oracles that read the
repository with plain ``git`` rather than through the manager under test. No root distribution,
root fixture, or stateful fixture plugin is involved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from beadhive_worktrees import NativeGitWorktreeManager
from beadhive_worktrees.testing import (
    MANAGER_CASES,
    ConformanceCase,
    ConformanceFailure,
    ManagerHarness,
    assert_manager_conforms,
    run_manager_case,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")

_ENV = {
    **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "conformance",
    "GIT_AUTHOR_EMAIL": "conformance@example.invalid",
    "GIT_COMMITTER_NAME": "conformance",
    "GIT_COMMITTER_EMAIL": "conformance@example.invalid",
}


def _run_git(args, *, check=True, capture=False):
    del capture  # the manager and inspector read stdout/stderr either way
    return subprocess.run(args, check=check, capture_output=True, text=True, env=_ENV)


class NativeHarness:
    """A real one-commit repository driven by the native manager."""

    def __init__(self, tmp_path: Path) -> None:
        self.main = tmp_path / "main"
        self.root = tmp_path / "worktrees"
        self.root.mkdir()
        self.main.mkdir()
        self.base = "main"
        self._git("init", "-q", "-b", self.base)
        self._git("config", "commit.gpgsign", "false")
        (self.main / "README").write_text("seed\n")
        self._git("add", "README")
        self._git("commit", "-q", "-m", "seed")
        self.manager = NativeGitWorktreeManager(_run_git)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = _run_git(["git", "-C", str(cwd or self.main), *args])
        return result.stdout.strip()

    def seed_branch(self, branch: str) -> str:
        self._git("branch", branch, self.base)
        scratch = self.root.parent / "seed-scratch"
        self._git("worktree", "add", "-q", str(scratch), branch)
        (scratch / "diverged").write_text(branch)
        self._git("add", "diverged", cwd=scratch)
        self._git("commit", "-q", "-m", f"diverge {branch}", cwd=scratch)
        self._git("worktree", "remove", str(scratch))
        return self.branch_tip(branch)

    def branch_tip(self, branch: str) -> str:
        ref = f"refs/heads/{branch}"
        command = ["git", "-C", str(self.main), "rev-parse", "--verify", "--quiet", ref]
        result = _run_git(command, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def checked_out(self, path: Path) -> str:
        if not path.is_dir():
            return ""
        result = _run_git(["git", "-C", str(path), "symbolic-ref", "--short", "HEAD"], check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def registered(self, path: Path) -> bool:
        listing = self._git("worktree", "list", "--porcelain")
        wanted = str(path.resolve())
        return any(
            line.removeprefix("worktree ") == wanted
            for line in listing.splitlines()
            if line.startswith("worktree ")
        )

    def make_dirty(self, path: Path) -> None:
        (path / "README").write_text("uncommitted\n")


def test_native_harness_satisfies_the_harness_protocol(tmp_path) -> None:
    harness: ManagerHarness = NativeHarness(tmp_path)
    assert harness.branch_tip(harness.base)


@pytest.mark.parametrize("case", MANAGER_CASES, ids=lambda case: case.case_id)
def test_native_manager_passes_each_conformance_case(
    case: ConformanceCase[ManagerHarness], tmp_path
) -> None:
    run_manager_case(case, NativeHarness(tmp_path))


def test_the_whole_kit_runs_on_a_fresh_harness_per_case(tmp_path) -> None:
    built: list[Path] = []

    def factory() -> NativeHarness:
        scratch = tmp_path / f"case-{len(built)}"
        scratch.mkdir()
        built.append(scratch)
        return NativeHarness(scratch)

    assert_manager_conforms(factory)
    assert len(built) == len(MANAGER_CASES)


class _ChoosesItsOwnPath(NativeGitWorktreeManager):
    """A deliberately broken provider: it ignores the spec's path."""

    def create(self, spec):
        from dataclasses import replace

        return super().create(replace(spec, path=spec.path.with_name(spec.path.name + "-mine")))


class _DeletesTheBranch(NativeGitWorktreeManager):
    """A deliberately broken provider: its remove also deletes the branch."""

    def remove(self, handle, force):
        removed = super().remove(handle, force)
        _run_git(["git", "-C", str(handle.main), "branch", "-D", handle.branch], check=False)
        return removed


@pytest.mark.parametrize(
    ("broken", "case_id"),
    [(_ChoosesItsOwnPath, "create.exact-path"), (_DeletesTheBranch, "remove.keeps-branch")],
)
def test_the_kit_rejects_a_provider_that_breaks_the_contract(tmp_path, broken, case_id) -> None:
    harness = NativeHarness(tmp_path)
    harness.manager = broken(_run_git)
    case = next(case for case in MANAGER_CASES if case.case_id == case_id)

    with pytest.raises(ConformanceFailure) as failure:
        run_manager_case(case, harness)

    assert failure.value.case_id == case_id
    assert failure.value.suite == "worktree.manager"
