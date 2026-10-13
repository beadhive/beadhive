"""One key run's directory inside its execution root (bh-mf1o4).

Under the local validation broker with ``work.validation_broker.roots``, the engine places each
key run on exactly one :class:`~beadhive_validation_broker.ExecutionRoot` and names it in
``Grant.root``. :func:`run_directory` turns that grant into a place to run: one
``bh-run-<job>-<rand>`` directory under the root holding

* ``checkout/`` — the exact tree, materialised with ``git worktree add --detach`` when the source
  worktree is not already a clean checkout of that tree under the chosen root (a bead worktree on
  an ephemeral tmpfs, say). A source that is one runs in place;
* ``tmp/`` — the run's ``TMPDIR``. ``scripts/hermetic.sh`` creates its ``bh-hermetic-*`` scratch
  under ``${TMPDIR}``, so the fence's scratch lands in the same root as the checkout; nothing in
  the fence changes;
* ``reports/`` — the run's ``BH_TEST_REPORT_DIR`` drop zone, which the fence binds writable.

so a run never straddles two filesystems. The git object store of a materialised checkout stays
in its source repository: it is read, never written, by the run (and read-only inside the fence).

**Cleanup.** The directory is removed when the ``with`` block exits, on success and on failure
alike. A client killed outright (SIGKILL) cannot clean up, so every directory carries an owner
marker and every :func:`run_directory` first sweeps its root for directories whose owner is dead
(:func:`sweep_run_directories`). That also reclaims the scratch a SIGKILLed fence leaves behind.

Nothing calls this yet: running keys through the broker is ``bh-qhrl6``. Git is a port
(:class:`GitPort`), so tests can fail it without patching a subprocess.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from beadhive_validation_broker import Owner, ProcessLiveness, current_owner, pid_alive

__all__ = [
    "OWNER_FILE",
    "RUN_PREFIX",
    "GitPort",
    "RunDirectory",
    "RunRootError",
    "SubprocessGit",
    "run_directory",
    "sweep_run_directories",
]

RUN_PREFIX = "bh-run-"
OWNER_FILE = ".bh-run-owner.json"


class RunRootError(RuntimeError):
    """The run directory could not be set up (the checkout failed or is not the exact tree)."""


class GitPort(Protocol):
    """The git operations a run directory needs. Production is :class:`SubprocessGit`."""

    def tree_of(self, repo: Path, rev: str) -> str:
        """The tree id of ``rev`` in ``repo``; raises :class:`RunRootError` when it cannot."""
        ...

    def is_clean(self, repo: Path) -> bool:
        """True when ``repo``'s working tree has no tracked or untracked change."""
        ...

    def worktree_add(self, source: Path, dest: Path, commit: str) -> None:
        """Check ``commit`` out detached at ``dest``; raises :class:`RunRootError` on failure."""
        ...

    def worktree_remove(self, source: Path, dest: Path) -> None:
        """Unregister ``dest`` from ``source`` (best effort; never raises)."""
        ...


class SubprocessGit:
    """:class:`GitPort` over the ``git`` binary."""

    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )

    def tree_of(self, repo: Path, rev: str) -> str:
        done = self._git(repo, "rev-parse", "--verify", f"{rev}^{{tree}}")
        if done.returncode != 0:
            raise RunRootError(f"cannot resolve the tree of {rev} in {repo}: {done.stderr.strip()}")
        return done.stdout.strip()

    def is_clean(self, repo: Path) -> bool:
        done = self._git(repo, "status", "--porcelain", "--untracked-files=all")
        return done.returncode == 0 and not done.stdout.strip()

    def worktree_add(self, source: Path, dest: Path, commit: str) -> None:
        done = self._git(source, "worktree", "add", "--detach", str(dest), commit)
        if done.returncode != 0:
            raise RunRootError(f"git worktree add {commit} failed: {done.stderr.strip()}")

    def worktree_remove(self, source: Path, dest: Path) -> None:
        self._git(source, "worktree", "remove", "--force", str(dest))
        self._git(source, "worktree", "prune")


@dataclass(frozen=True)
class RunDirectory:
    """Where one key run executes. Every path the run is given lies under ``root``."""

    root: Path
    path: Path
    checkout: Path
    tmp: Path
    reports: Path
    materialised: bool

    def env(self) -> dict[str, str]:
        """Overlay for the key subprocess: its scratch and report drop zone in this root."""
        return {"TMPDIR": str(self.tmp), "BH_TEST_REPORT_DIR": str(self.reports)}

    def paths(self) -> tuple[Path, ...]:
        """Every location this run writes to or runs in."""
        return (self.path, self.checkout, self.tmp, self.reports)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _job_slug(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", job_id).strip("-")[:48] or "job"


def _remove_tree(path: Path) -> None:
    """Remove ``path`` even when the run left read-only directories behind."""
    if not os.path.lexists(path):
        return
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    for dirpath, dirnames, _files in os.walk(path):
        for name in (dirpath, *(os.path.join(dirpath, d) for d in dirnames)):
            try:
                os.chmod(name, 0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _write_owner(path: Path, owner: Owner, source: Path, materialised: bool) -> None:
    marker = {
        "pid": owner.pid,
        "start_token": owner.start_token,
        "host": owner.host,
        "source": str(source),
        "materialised": materialised,
    }
    staging = path / (OWNER_FILE + ".tmp")
    staging.write_text(json.dumps(marker, sort_keys=True))
    staging.replace(path / OWNER_FILE)


def _read_owner(path: Path) -> tuple[Owner, Path, bool] | None:
    try:
        marker = json.loads((path / OWNER_FILE).read_text())
        owner = Owner(int(marker["pid"]), marker.get("start_token"), marker.get("host"))
        return owner, Path(marker["source"]), bool(marker.get("materialised"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _discard(path: Path, source: Path, materialised: bool, git: GitPort) -> None:
    if materialised:
        git.worktree_remove(source, path / "checkout")
    _remove_tree(path)


def sweep_run_directories(
    root: Path, *, owner_alive: ProcessLiveness = pid_alive, git: GitPort | None = None
) -> list[Path]:
    """Remove ``root``'s run directories whose owner is dead; returns what was removed.

    A directory without a readable owner marker is never removed: what cannot be attributed is
    never reclaimed. Owners on another host count as alive (:func:`pid_alive`).
    """
    git = git or SubprocessGit()
    removed: list[Path] = []
    try:
        candidates = sorted(p for p in Path(root).iterdir() if p.name.startswith(RUN_PREFIX))
    except OSError:
        return removed
    for path in candidates:
        marker = _read_owner(path)
        if marker is None or owner_alive(marker[0]):
            continue
        _owner, source, materialised = marker
        _discard(path, source, materialised, git)
        removed.append(path)
    return removed


def _runs_in_place(source: Path, root: Path, commit: str, git: GitPort) -> bool:
    """A source under the chosen root may run in place only when it provably is the exact tree."""
    if not _within(source, root):
        return False
    try:
        return git.is_clean(source) and git.tree_of(source, "HEAD") == git.tree_of(source, commit)
    except RunRootError:
        return False


@contextmanager
def run_directory(
    root: str | Path,
    *,
    source: Path,
    commit: str,
    job_id: str,
    git: GitPort | None = None,
    owner: Owner | None = None,
    owner_alive: ProcessLiveness = pid_alive,
) -> Iterator[RunDirectory]:
    """A run directory for ``job_id`` under the granted ``root``, removed when the block exits.

    ``source`` is the worktree (or clone) holding ``commit``; ``commit`` names the exact tree to
    validate. The checkout is materialised under ``root`` unless ``source`` already is a clean
    checkout of that tree inside ``root``. Raises :class:`RunRootError`, after removing anything it
    created, when the checkout fails or does not hold the exact tree.
    """
    git = git or SubprocessGit()
    root = Path(root).resolve()
    source = Path(source).resolve()
    sweep_run_directories(root, owner_alive=owner_alive, git=git)
    expected = git.tree_of(source, commit)
    in_place = _runs_in_place(source, root, commit, git)
    path = Path(tempfile.mkdtemp(prefix=f"{RUN_PREFIX}{_job_slug(job_id)}-", dir=root))
    materialised = not in_place
    try:
        _write_owner(path, owner or current_owner(), source, materialised)
        checkout = source if in_place else path / "checkout"
        if materialised:
            git.worktree_add(source, checkout, commit)
            actual = git.tree_of(checkout, "HEAD")
            if actual != expected:
                raise RunRootError(
                    f"materialised checkout {checkout} holds tree {actual}, not {expected}"
                )
        run = RunDirectory(
            root=root,
            path=path,
            checkout=checkout,
            tmp=path / "tmp",
            reports=path / "reports",
            materialised=materialised,
        )
        run.tmp.mkdir()
        run.reports.mkdir()
        yield run
    finally:
        _discard(path, source, materialised, git)
