"""A key run's directory lives wholly inside its execution root (bh-mf1o4).

Real git in throwaway repositories for the happy paths (the exact tree is the point); a fake
:class:`GitPort` only where git must fail. Killed clients are simulated by a liveness probe that
reports the owner dead, never by timing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from beadhive.validation_reservations import grant_environ
from beadhive.validation_run_root import (
    OWNER_FILE,
    RUN_PREFIX,
    RunRootError,
    SubprocessGit,
    run_directory,
    sweep_run_directories,
)
from beadhive_validation_broker import ROOT_POOL_PREFIX, Capacity, Grant, Owner

REPO = Path(__file__).resolve().parent.parent
FENCE = REPO / "scripts" / "hermetic.sh"
DEAD = Owner(pid=999_999, start_token="dead")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def source(tmp_path) -> Path:
    """A bead worktree outside every root, with two commits (the run validates the first)."""
    repo = tmp_path / "elsewhere" / "bead"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "scripts").mkdir()
    shutil.copy2(FENCE, repo / "scripts" / "hermetic.sh")
    (repo / "file.txt").write_text("exact\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    (repo / "file.txt").write_text("later\n")
    _git(repo, "commit", "-qam", "second")
    return repo


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path.resolve()


def _worktrees(repo: Path) -> str:
    return _git(repo, "worktree", "list", "--porcelain")


def _inside(path: Path, root: Path) -> bool:
    resolved = Path(path).resolve()
    return resolved == root or root in resolved.parents


def test_a_worktree_outside_the_root_is_materialised_there_at_the_exact_tree(source, root):
    first = _git(source, "rev-parse", "HEAD~1")
    with run_directory(root, source=source, commit=first, job_id="attest/unit") as run:
        assert run.materialised
        assert (run.checkout / "file.txt").read_text() == "exact\n"
        assert _git(run.checkout, "rev-parse", "HEAD^{tree}") == _git(
            source, "rev-parse", "HEAD~1^{tree}"
        )
        assert all(_inside(path, root) for path in run.paths())
        assert all(_inside(Path(value), root) for value in run.env().values())
        assert run.env()["TMPDIR"] == str(run.tmp) and run.tmp.is_dir()
        assert run.path.name.startswith(RUN_PREFIX + "attest-unit-")
        assert str(run.checkout) in _worktrees(source)
        path = run.path
    assert not path.exists(), "removed on success"
    assert str(path) not in _worktrees(source), "the materialised worktree is unregistered"


def test_the_run_directory_is_removed_when_the_run_fails(source, root):
    head = _git(source, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="red key"):
        with run_directory(root, source=source, commit=head, job_id="k") as run:
            (run.tmp / "bh-hermetic-abc").mkdir()
            path = run.path
            raise RuntimeError("red key")
    assert not path.exists()
    assert list(root.iterdir()) == []
    assert str(path) not in _worktrees(source)


def test_a_killed_clients_run_directory_is_reclaimed_by_the_next_run(source, root):
    head = _git(source, "rev-parse", "HEAD")
    killed = run_directory(root, source=source, commit=head, job_id="killed", owner=DEAD)
    abandoned = killed.__enter__()  # SIGKILL: the with block never exits
    (abandoned.tmp / "bh-hermetic-leaked").mkdir()
    (abandoned.tmp / "bh-hermetic-leaked" / "store").write_text("x" * 1024)
    os.chmod(abandoned.tmp / "bh-hermetic-leaked", 0o500)  # read-only debris must still go
    assert abandoned.path.exists()

    def alive(owner: Owner) -> bool:
        return owner != DEAD

    with run_directory(root, source=source, commit=head, job_id="next", owner_alive=alive) as run:
        assert not abandoned.path.exists(), "the dead owner's directory and scratch are gone"
        assert str(abandoned.checkout) not in _worktrees(source)
        assert [p.name for p in root.iterdir()] == [run.path.name]


def test_a_live_owners_or_unattributable_directory_is_never_swept(source, root):
    head = _git(source, "rev-parse", "HEAD")
    in_flight = run_directory(root, source=source, commit=head, job_id="live")
    live = in_flight.__enter__()  # held open: an in-flight run of this (live) process
    stray = root / (RUN_PREFIX + "no-marker")
    stray.mkdir()
    removed = sweep_run_directories(root, owner_alive=lambda owner: owner.pid == os.getpid())
    assert removed == []
    assert live.path.exists() and stray.exists()
    assert (live.path / OWNER_FILE).is_file()
    in_flight.__exit__(None, None, None)
    assert not live.path.exists() and stray.exists()


def test_a_clean_exact_checkout_already_inside_the_root_runs_in_place(source, tmp_path):
    root = source.parent.resolve()
    head = _git(source, "rev-parse", "HEAD")
    with run_directory(root, source=source, commit=head, job_id="k") as run:
        assert not run.materialised
        assert run.checkout == source.resolve()
        assert all(_inside(path, root) for path in run.paths())
    assert source.exists(), "an in-place source is never removed"


@pytest.mark.parametrize("why", ["dirty", "other-commit"])
def test_an_inexact_source_inside_the_root_is_materialised_instead(source, why):
    root = source.parent.resolve()
    commit = _git(source, "rev-parse", "HEAD")
    if why == "dirty":
        (source / "untracked.txt").write_text("not committed\n")
    else:
        commit = _git(source, "rev-parse", "HEAD~1")
    with run_directory(root, source=source, commit=commit, job_id="k") as run:
        assert run.materialised and run.checkout != source.resolve()
        assert not (run.checkout / "untracked.txt").exists()
        assert _git(run.checkout, "rev-parse", "HEAD^{tree}") == _git(
            source, "rev-parse", f"{commit}^{{tree}}"
        )


class _WrongTreeGit(SubprocessGit):
    """Checks out fine but reports a different tree for the checkout (a corrupt checkout)."""

    def tree_of(self, repo: Path, rev: str) -> str:
        real = super().tree_of(repo, rev)
        return "0" * 40 if repo.name == "checkout" else real


class _FailingAddGit(SubprocessGit):
    def worktree_add(self, source: Path, dest: Path, commit: str) -> None:
        raise RunRootError("no space left on device")


@pytest.mark.parametrize("git", [_WrongTreeGit(), _FailingAddGit()], ids=["wrong-tree", "no-add"])
def test_a_checkout_that_is_not_the_exact_tree_is_refused_and_cleaned(source, root, git):
    head = _git(source, "rev-parse", "HEAD")
    with pytest.raises(RunRootError):
        with run_directory(root, source=source, commit=head, job_id="k", git=git):
            pytest.fail("the run must not start")
    assert list(root.iterdir()) == []
    assert _worktrees(source).count("worktree ") == 1, "only the source itself is registered"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is Linux-only")
def test_the_fence_scratch_and_checkout_resolve_under_the_chosen_root(source, root):
    """The real fence, run from a materialised checkout with the run's env: its TMPDIR scratch
    and working directory are both inside the chosen root, and both are gone afterwards."""
    head = _git(source, "rev-parse", "HEAD")
    with run_directory(root, source=source, commit=head, job_id="fenced") as run:
        result = subprocess.run(
            [
                str(run.checkout / "scripts" / "hermetic.sh"),
                "sh",
                "-c",
                'touch "$TMPDIR/probe" && printf "%s\\n%s\\n" "$TMPDIR" "$(pwd -P)"',
            ],
            env={**os.environ, **run.env()},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        scratch, cwd = result.stdout.splitlines()[-2:]
        assert Path(scratch).parent == run.tmp and Path(scratch).name.startswith("bh-hermetic-")
        assert Path(cwd) == run.checkout
        assert all(_inside(Path(p), root) for p in (scratch, cwd))
        assert list(run.tmp.iterdir()) == [], "the fence still removes its own scratch"
        path = run.path
    assert not path.exists()


def test_a_rooted_grants_environ_keeps_the_root_and_the_run_overlay(source, root):
    """The key environment (bh-9oc32's allow-list) carries the root grant through, and the run
    directory's TMPDIR / report zone overlay survives the same allow-list."""
    grant = Grant(
        "lease-1",
        "k",
        Capacity(2.0, 1024, {ROOT_POOL_PREFIX + str(root): 4096}),
        root=str(root),
    )
    head = _git(source, "rev-parse", "HEAD")
    with run_directory(root, source=source, commit=head, job_id="k") as run:
        env = grant_environ(grant, {"PATH": "/bin", "BH_GRANT_ROOT": "/stale", **run.env()})
        assert env["BH_GRANT_ROOT"] == str(root)
        assert env["BH_GRANT_DISK_BYTES"] == "4096"
        assert env["TMPDIR"] == str(run.tmp)
        assert env["BH_TEST_REPORT_DIR"] == str(run.reports)
        assert all(_inside(Path(env[name]), root) for name in ("TMPDIR", "BH_TEST_REPORT_DIR"))
