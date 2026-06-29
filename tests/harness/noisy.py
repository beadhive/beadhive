"""Build noisy bead branches for the `ws work show`/`refine` integration tests.

A real `wt/bead/<id>` worktree off the rig's HEAD, with local checkpoint noise laid down:
real commits + a `git commit --fixup=` marker + a file-subset "wip" checkpoint. Folds are
contiguous so the canonical branch refines conflict-free; the conflict case is built inline
in the test from `provision` + `commit`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ws import config, worktree

from .world import git


@dataclass
class Noisy:
    target: Path
    branch: str
    base: str  # the merge-base (== main tip at fork); base..branch is the local history
    shas: dict  # name -> full sha


def provision(rig, bead_id: str):
    """Create (or reattach) the bead worktree; return (entry, target, branch, base)."""
    cfg = config.load()
    entry, target, branch = worktree.ensure(cfg, rig.repo, bead_id)
    base = git("rev-parse", "HEAD", cwd=target).stdout.strip()
    return entry, target, branch, base


def commit(
    target: Path, fname: str, content: str, msg: str | None = None, fixup: str = "", date: str = ""
) -> str:
    """Write `content` to `fname`, stage, and commit (a `--fixup=<sha>` marker if `fixup` set).
    `date` sets the author date (so tests can prove refine retains a *spread* of dates, not the
    wall-clock second). Returns the new commit's full sha."""
    (Path(target) / fname).write_text(content)
    git("add", "-A", cwd=target)
    args = ["commit", "-q", f"--fixup={fixup}"] if fixup else ["commit", "-qm", msg]
    if date:
        args.append(f"--date={date}")
    git(*args, cwd=target)
    return git("rev-parse", "HEAD", cwd=target).stdout.strip()


def make_noisy_branch(rig, bead_id: str = "mr-noisy") -> Noisy:
    """Canonical noisy branch (oldest→newest), all folds contiguous:
      1 feat: core feature     core.py     (fold target)
      2 fixup! …               core.py     (marker; --fixup of #1)
      3 feat: helper           helper.py   (fold target)
      4 wip checkpoint         helper.py   (file-subset of #3 → fixup? flag)"""
    _entry, target, branch, base = provision(rig, bead_id)
    # Distinct back-dated author dates on the keeps so a digest's retained date is provably the
    # keep's original (not the refine moment, and not all-the-same-second from fast test commits).
    s = {
        "core": commit(target, "core.py", "v1\n", "feat: core feature", date="2026-06-01T10:00:00"),
    }
    s["fix1"] = commit(target, "core.py", "v2\n", fixup=s["core"])
    s["helper"] = commit(target, "helper.py", "h1\n", "feat: helper", date="2026-06-02T10:00:00")
    s["wip"] = commit(target, "helper.py", "h2\n", "wip checkpoint")
    return Noisy(target, branch, base, s)


def author_date(target: Path, ref: str) -> str:
    """The author date (iso-strict) of `ref` — to prove refine retains per-digest dates."""
    return git("show", "-s", "--format=%ad", "--date=iso-strict", ref, cwd=target).stdout.strip()


def branches(main: Path) -> list[str]:
    return git("branch", "--format=%(refname:short)", cwd=main).stdout.split()
