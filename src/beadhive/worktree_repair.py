"""Detect and repair a *gutted* worktree: its directory survives but the ``.git`` link file
(and usually many tracked files) were purged, e.g. by macOS periodic cleanup of ``$TMPDIR``
(bh-uwlsw). ``git worktree list`` then shows the registration ``prunable``.

Repair is deliberately narrow. It only re-creates the ``.git`` file and restores *missing*
tracked files for a worktree that is still registered in the main clone and whose directory
still exists. Every proof happens BEFORE any write, using the registration's admin dir as
``GIT_DIR``; if any proof fails the worktree is left byte-for-byte untouched and a refusal
reason is returned. Nothing here moves, prunes or deletes a worktree, and nothing overwrites a
file that exists on disk.

Known limitations (documented in docs/WORKTREES.md):

* A *missing* tracked file is indistinguishable from a purged one. The dirty check uses
  ``--diff-filter=MTU`` (modified / type-changed / unmerged) and deliberately ignores deletions,
  so a deliberate uncommitted ``rm`` of a tracked file made while the ``.git`` file is also gone
  is resurrected by the repair. Only an unstaged deletion alongside real edits is refused (the
  edits make the whole repair refuse).
* Sparse-checkout worktrees (``core.sparseCheckout``, an ``info/sparse-checkout`` file or
  skip-worktree index entries) and worktrees whose HEAD carries submodules (``.gitmodules`` or
  gitlink entries) are refused: rebuilding an index from HEAD drops skip-worktree bits and would
  materialise files outside the cone, and submodule checkouts are not restorable from the index.
* If restoring files fails after the ``.git`` file was written, repair rolls back what it wrote
  (the ``.git`` file and any index it created) so the directory is left exactly as found, still
  gutted, with a refusal. Files that were already restored stay: they are tracked, byte-equal to
  HEAD, and were absent before.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Admin-dir files that mean an operation is mid-flight (uncommitted state we cannot reason about).
_IN_PROGRESS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
)


@dataclass(frozen=True)
class RepairResult:
    """``state``: ``healthy``, ``repaired`` or ``refused`` (``reason`` says why)."""

    state: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.state != "refused"


def is_gutted(target: Path) -> bool:
    """A worktree directory that exists but has lost its ``.git`` link file."""
    return target.is_dir() and not (target / ".git").exists()


def _git(args, env_extra, cwd=None) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, env=env, cwd=cwd, check=False
    )


def _admin_dir(main: Path, target: Path) -> Path | None:
    """The registration (``<common>/worktrees/<name>``) whose ``gitdir`` names ``target/.git``."""
    proc = _git(["rev-parse", "--git-common-dir"], {}, cwd=str(main))
    if proc.returncode != 0:
        return None
    common = Path(proc.stdout.strip())
    common = common if common.is_absolute() else (main / common)
    want = os.path.realpath(target / ".git")
    try:
        entries = sorted((common / "worktrees").iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            recorded = (entry / "gitdir").read_text().strip()
        except OSError:
            continue
        if os.path.realpath(recorded) == want:
            return entry
    return None


def _refuse(reason: str) -> RepairResult:
    return RepairResult("refused", reason)


def _roll_back(target: Path, index: Path, *, created_index: bool) -> None:
    """Undo what repair wrote: the ``.git`` file and (only if repair created it) the index."""
    (target / ".git").unlink(missing_ok=True)
    if created_index:
        index.unlink(missing_ok=True)


def _unsupported_layout(admin: Path) -> str:
    """Why this worktree must not be rebuilt from HEAD (sparse checkout / submodules), else ''."""
    env = {"GIT_DIR": str(admin)}
    sparse = _git(["config", "--get", "core.sparseCheckout"], env).stdout.strip().lower()
    if sparse in ("true", "1", "yes", "on") or (admin / "info" / "sparse-checkout").exists():
        return "is a sparse-checkout worktree"
    if (admin / "index").exists():
        flags = _git(["ls-files", "-v"], env)
        if any(line.startswith("S ") for line in flags.stdout.splitlines()):
            return "has skip-worktree entries (sparse checkout)"
    tree = _git(["ls-tree", "-r", "HEAD"], env)
    if any(line.startswith("160000 ") for line in tree.stdout.splitlines()):
        return "has submodules (gitlink entries in HEAD)"
    if _git(["cat-file", "-e", "HEAD:.gitmodules"], env).returncode == 0:
        return "has submodules (.gitmodules in HEAD)"
    return ""


def repair_gutted(main: Path, target: Path, branch: str = "") -> RepairResult:
    """Repair ``target`` if (and only if) it can be proven safe; see the module docstring."""
    if not is_gutted(target):
        return RepairResult("healthy")
    where = f"worktree {target} is missing its .git file"
    admin = _admin_dir(main, target)
    if admin is None:
        return _refuse(
            f"{where} and is not registered in {main} (no matching worktrees/<name>/gitdir); "
            "refusing to guess — remove or re-create it by hand"
        )
    for marker in _IN_PROGRESS:
        if (admin / marker).exists():
            return _refuse(f"{where}, but an operation is in progress ({marker}); refusing")
    head = _git(["symbolic-ref", "-q", "HEAD"], {"GIT_DIR": str(admin)})
    ref = head.stdout.strip()
    if head.returncode != 0 or not ref.startswith("refs/heads/"):
        return _refuse(f"{where}, but its HEAD is detached or unreadable; refusing")
    if branch and ref != f"refs/heads/{branch}":
        return _refuse(f"{where}, but it is registered on {ref}, not {branch}; refusing")
    if _git(["rev-parse", "--verify", "-q", "HEAD^{commit}"], {"GIT_DIR": str(admin)}).returncode:
        return _refuse(f"{where}, but its branch has no readable commit; refusing")

    unsupported = _unsupported_layout(admin)
    if unsupported:
        return _refuse(f"{where}, but it {unsupported}; refusing — repair it by hand")

    base = {"GIT_DIR": str(admin), "GIT_WORK_TREE": str(target)}
    real_index = admin / "index"
    with tempfile.TemporaryDirectory(prefix="bh-repair-") as tmp:
        index_missing = not real_index.exists()
        env = dict(base)
        if index_missing:
            env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
            if _git(["read-tree", "HEAD"], env).returncode:
                return _refuse(f"{where}, but HEAD could not be read into an index; refusing")
        staged = _git(["diff", "--cached", "--name-only", "HEAD"], env)
        if staged.returncode or staged.stdout.strip():
            return _refuse(
                f"{where}, but it has staged changes (uncommitted work); refusing — "
                "stage/commit state would not survive a repair"
            )
        dirty = _git(["diff", "--name-only", "--diff-filter=MTU"], env)
        if dirty.returncode or dirty.stdout.strip():
            first = dirty.stdout.split()[:3]
            return _refuse(
                f"{where}, but tracked files differ from HEAD (uncommitted edits: "
                f"{', '.join(first) or 'unknown'}); refusing"
            )
        # Proven: nothing on disk is modified or staged. Only missing files will be written.
        (target / ".git").write_text(f"gitdir: {admin}\n")
        if index_missing:
            # Rebuild the real index from HEAD; mixed reset never touches the working tree.
            if _git(["reset", "-q"], {}, cwd=str(target)).returncode:
                _roll_back(target, real_index, created_index=True)
                return _refuse(f"{where}, but rebuilding the index failed; left unchanged")
        # checkout-index without -f/--force never overwrites an existing file.
        proc = _git(["checkout-index", "-a", "-q"], {}, cwd=str(target))
        if proc.returncode:
            _roll_back(target, real_index, created_index=index_missing)
            return _refuse(
                f"{where}: restoring tracked files failed ({proc.stderr.strip()}); "
                "rolled back the .git file and index repair wrote, worktree left gutted"
            )
    return RepairResult("repaired", f"repaired gutted worktree {target} (.git + missing files)")
