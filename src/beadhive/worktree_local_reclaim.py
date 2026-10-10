"""HQ-independent, fail-closed worktree reclamation for partial degradation (bh-o01m6).

``bh worktree local-reclaim`` reclaims disk (usually
tmpfs) when HQ SQL or the verified runtime configuration is unavailable — the circular failure
where memory/tmpfs pressure takes HQ down and the HQ-backed ``rm``/``prune`` then cannot free
the space needed to bring it back.

This module is deliberately **stdlib-only** and never imports :mod:`beadhive.config`, the
registry, HQ SQL, or the Typer CLI tree: :mod:`beadhive.cli_entrypoint` dispatches here before
any of that is loaded, so neither ``--help`` nor a real run can block on HQ.

Safety is proven from local Git and process state only. A candidate is removed only when ALL of
the following hold; anything else is kept and reported with a reason code (fail closed):

* it is a registered linked worktree of its repository (never the main worktree);
* the Git worktree lock is absent;
* no tracked changes, non-ignored untracked files, or ignored precious content (only regular
  ignored files and non-followed leaf links under the disposable `.venv` cache are exempt);
* no merge/rebase/cherry-pick/revert/bisect is in progress and no ``index.lock`` exists;
* no live bh validation marker or local presenter binding names it;
* no readable local process has its cwd, root or an open file inside it (an incomplete scan
  of this user's processes keeps every target);
* its HEAD is provably landed: reachable from a local base ref (``main``/``master`` or their
  ``origin/`` twins, or ``--base``) but NOT on that base's first-parent chain — i.e. it holds
  commits that were merged; its HEAD is also reachable from a persisted origin tracking ref.
  A fresh seat whose HEAD still sits on the base line is ambiguous
  (it may be about to be used) and is kept.

Removal goes through ``git worktree remove`` without ``--force`` so Git re-checks cleanliness
itself; branches are never deleted (the branch is the durable artifact). Every run reports
per-target results, reclaimed bytes, and filesystem (tmpfs) usage before and after.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

SCHEMA = "bh.worktree.local-reclaim/v1"
GIT_TIMEOUT_SECONDS = 5
MEMORY_BACKED_FSTYPES = frozenset({"tmpfs", "ramfs"})
DEFAULT_BASE_REFS = ("main", "master", "origin/main", "origin/master")
IN_PROGRESS_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
    "index.lock",
)
VALIDATION_ACTIVE_DIR = Path("bh") / "validation" / "active"
MAX_ROOT_DEPTH = 6

# Result statuses.
REMOVED = "removed"
WOULD_REMOVE = "would_remove"
KEPT = "kept"
FAILED = "failed"


# ---- ports ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ProcessScan:
    """Paths held by local processes: ``(pid, path)`` for cwd/root/open files.

    ``complete`` is False when the process table could not be walked or a process owned by this
    user failed to read: the caller must then keep every target. ``opaque`` lists
    ``(pid, comm)`` of this user's processes the kernel refused to expose. These also
    keep every target; a process name is not evidence of inactive ownership."""

    holders: tuple[tuple[int, str], ...]
    complete: bool
    detail: str = ""
    unreadable_foreign: int = 0
    opaque: tuple[tuple[int, str], ...] = ()

    def holders_of(self, target: str) -> list[int]:
        prefix = target.rstrip(os.sep) + os.sep
        return sorted(
            {pid for pid, path in self.holders if path == target or path.startswith(prefix)}
        )


@dataclass(frozen=True)
class FsUsage:
    mount: str
    fstype: str
    memory_backed: bool
    total_bytes: int
    used_bytes: int
    available_bytes: int

    def to_dict(self) -> dict:
        pct = round(100.0 * self.used_bytes / self.total_bytes, 1) if self.total_bytes else None
        return {
            "mount": self.mount,
            "fstype": self.fstype,
            "memory_backed": self.memory_backed,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "available_bytes": self.available_bytes,
            "used_percent": pct,
        }


def _real_git(argv: Sequence[str], cwd: str | None = None) -> GitResult:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GitResult(124, "", f"git {' '.join(argv[:2])} timed out")
    except OSError as exc:
        return GitResult(127, "", f"git unavailable: {exc}")
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def _strip_deleted(link: str) -> str:
    return link[: -len(" (deleted)")] if link.endswith(" (deleted)") else link


def _real_process_scan() -> ProcessScan:
    proc = Path("/proc")
    if not (proc / "self" / "cwd").exists():
        return ProcessScan((), False, "no /proc process table on this platform")
    uid = os.getuid()
    holders: list[tuple[int, str]] = []
    unreadable_own: list[int] = []
    opaque: list[tuple[int, str]] = []
    unreadable_foreign = 0
    try:
        entries = [entry for entry in os.listdir(proc) if entry.isdigit()]
    except OSError as exc:
        return ProcessScan((), False, f"cannot list /proc: {exc}")
    for entry in entries:
        pid = int(entry)
        base = proc / entry
        try:
            owner = base.stat().st_uid
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited mid-scan
        except OSError:
            return ProcessScan(
                tuple(holders),
                False,
                f"cannot inspect process ownership for {pid}",
                unreadable_foreign,
                tuple(opaque),
            )
        links: list[str] = []
        try:
            for name in ("cwd", "root"):
                links.append(os.readlink(base / name))
            fd_dir = base / "fd"
            for fd in os.listdir(fd_dir):
                try:
                    links.append(os.readlink(fd_dir / fd))
                except FileNotFoundError:
                    continue  # fd closed mid-scan
        except PermissionError:
            if owner == uid:
                opaque.append((pid, _comm(base)))
            else:
                unreadable_foreign += 1
            continue
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited mid-scan
        except OSError:
            if owner == uid:
                unreadable_own.append(pid)
            continue
        holders.extend((pid, _strip_deleted(link)) for link in links if link.startswith("/"))
    if unreadable_own:
        shown = ", ".join(str(pid) for pid in unreadable_own[:5])
        return ProcessScan(
            tuple(holders),
            False,
            f"could not inspect own process(es) {shown}",
            unreadable_foreign,
            tuple(opaque),
        )
    return ProcessScan(tuple(holders), True, "", unreadable_foreign, tuple(opaque))


def _comm(base: Path) -> str:
    try:
        return (base / "comm").read_text().strip()
    except OSError:
        return "?"


def _real_mounts() -> list[tuple[str, str]]:
    """``(mountpoint, fstype)`` pairs; empty when the mount table is unreadable."""
    try:
        text = Path("/proc/self/mounts").read_text()
    except OSError:
        return []
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mounts.append((parts[1].replace("\\040", " "), parts[2]))
    return mounts


def _real_statvfs(path: str) -> tuple[int, int, int]:
    """``(total, used, available)`` bytes for the filesystem holding *path*."""
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    return total, used, st.f_bavail * st.f_frsize


def _real_memory_available() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_local_json(path: str) -> dict:
    """Bounded regular-file read: neither a FIFO nor a symlink can block/redirect recovery."""
    if os.path.realpath(path) != os.path.abspath(path):
        raise ValueError("lifecycle path contains a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024:
            raise ValueError("lifecycle metadata is not a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError("lifecycle metadata grew past the read bound")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("lifecycle metadata must be an object")
        return value
    finally:
        os.close(descriptor)


def _real_disk_usage(path: str) -> int | None:
    """Exclusive allocated bytes: no symlink follow, no sparse logical-size overcount.

    Hardlinked files count only if every link is inside this tree; deleting an external
    package-cache link does not free that file's allocation.
    """
    try:
        top = os.lstat(path)
    except OSError:
        return None
    allocations: dict[tuple[int, int], list[int]] = {}
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            st = os.lstat(current)
        except OSError:
            continue
        if st.st_dev != top.st_dev:
            continue
        key = (st.st_dev, st.st_ino)
        if key in allocations:
            allocations[key][2] += 1
            continue
        directory = stat.S_ISDIR(st.st_mode)
        allocations[key] = [
            st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size,
            1 if directory else st.st_nlink,
            1,
        ]
        if directory:
            try:
                with os.scandir(current) as it:
                    stack.extend(child.path for child in it)
            except OSError:
                continue
    return sum(size for size, links, observed in allocations.values() if observed >= links)


@dataclass
class LocalReclaimPorts:
    """Everything the reclaimer observes or mutates; tests substitute fakes."""

    git: Callable[[Sequence[str], str | None], GitResult] = _real_git
    process_scan: Callable[[], ProcessScan] = _real_process_scan
    mounts: Callable[[], list[tuple[str, str]]] = _real_mounts
    statvfs: Callable[[str], tuple[int, int, int]] = _real_statvfs
    disk_usage: Callable[[str], int | None] = _real_disk_usage
    exists: Callable[[str], bool] = os.path.exists
    realpath: Callable[[str], str] = os.path.realpath
    memory_available: Callable[[], int | None] = _real_memory_available


def _default_ports() -> LocalReclaimPorts:
    """The real host ports (a seam: tests substitute fakes for /proc, statvfs and du)."""
    return LocalReclaimPorts()


# ---- results ----------------------------------------------------------------------------------


@dataclass
class TargetResult:
    path: str
    status: str = KEPT
    reasons: list[str] = field(default_factory=list)
    detail: str = ""
    repo: str = ""
    branch: str = ""
    head: str = ""
    landed_on: str = ""
    bytes: int | None = None
    holders: list[int] = field(default_factory=list)

    def keep(self, reason: str, detail: str = "") -> TargetResult:
        self.status = KEPT
        self.reasons.append(reason)
        if detail and not self.detail:
            self.detail = detail
        return self

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "reasons": list(self.reasons),
            "detail": self.detail,
            "repo": self.repo,
            "branch": self.branch,
            "head": self.head,
            "landed_on": self.landed_on,
            "reclaimed_bytes": self.bytes if self.status == REMOVED else 0,
            "size_bytes": self.bytes,
            "holder_pids": list(self.holders),
        }


@dataclass(frozen=True)
class _Registered:
    path: str
    head: str
    branch: str
    locked: bool
    prunable: bool
    bare: bool


@dataclass
class _Repo:
    common_dir: str
    main_path: str
    main_bare: bool
    worktrees: dict[str, _Registered]
    first_parent: dict[str, frozenset[str] | None] = field(default_factory=dict)


# ---- the reclaimer ----------------------------------------------------------------------------


class LocalReclaimer:
    def __init__(
        self,
        ports: LocalReclaimPorts | None = None,
        *,
        base_refs: Sequence[str] = (),
        dry_run: bool = False,
        strict_process_scan: bool = False,
    ) -> None:
        self.ports = ports or LocalReclaimPorts()
        self.base_refs = tuple(base_refs) or DEFAULT_BASE_REFS
        self.dry_run = dry_run
        self.strict_process_scan = strict_process_scan
        self._repos: dict[str, _Repo | None] = {}

    # -- git helpers --

    def _git(self, argv: Sequence[str], cwd: str | None = None) -> GitResult:
        return self.ports.git(list(argv), cwd)

    def _common_dir(self, path: str) -> str | None:
        res = self._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
        out = res.stdout.strip()
        return self.ports.realpath(out) if res.returncode == 0 and out else None

    def _repo(self, common_dir: str, probe_cwd: str) -> _Repo | None:
        if common_dir in self._repos:
            return self._repos[common_dir]
        res = self._git(["worktree", "list", "--porcelain"], probe_cwd)
        repo = None
        if res.returncode == 0:
            records = _parse_porcelain(res.stdout, self.ports.realpath)
            if records:
                main = records[0]
                repo = _Repo(
                    common_dir=common_dir,
                    main_path=main.path,
                    main_bare=main.bare,
                    worktrees={record.path: record for record in records[1:]},
                )
        self._repos[common_dir] = repo
        return repo

    def _git_dir(self, path: str) -> str | None:
        res = self._git(["rev-parse", "--path-format=absolute", "--git-dir"], path)
        out = res.stdout.strip()
        return out if res.returncode == 0 and out else None

    def _first_parent(self, repo: _Repo, base: str) -> frozenset[str] | None:
        if base not in repo.first_parent:
            res = self._git(["rev-list", "--first-parent", base], repo.main_path)
            repo.first_parent[base] = frozenset(res.stdout.split()) if res.returncode == 0 else None
        return repo.first_parent[base]

    def _repo_cwd(self, repo: _Repo) -> str:
        return repo.main_path if not repo.main_bare else repo.common_dir

    # -- discovery --

    def discover_roots(self, roots: Iterable[str]) -> list[str]:
        found: list[str] = []
        for root in roots:
            real = os.path.abspath(os.path.expanduser(root))
            if self.ports.realpath(real) != real:
                found.append(real)
                continue
            if not os.path.isdir(real):
                continue
            stack = [(real, 0)]
            while stack:
                current, depth = stack.pop()
                if os.path.lexists(os.path.join(current, ".git")):
                    if current != real:
                        found.append(current)
                    continue
                if depth >= MAX_ROOT_DEPTH:
                    continue
                try:
                    with os.scandir(current) as it:
                        children = [
                            entry.path for entry in it if entry.is_dir(follow_symlinks=False)
                        ]
                except OSError:
                    continue
                stack.extend((child, depth + 1) for child in sorted(children, reverse=True))
        return found

    def discover_repos(self, repos: Iterable[str]) -> tuple[list[str], list[str]]:
        """Linked worktrees of each repo: ``(present paths, stale admin repos)``."""
        targets: list[str] = []
        stale: list[str] = []
        for repo_path in repos:
            real = self.ports.realpath(repo_path)
            if real != os.path.abspath(os.path.expanduser(repo_path)):
                targets.append(os.path.abspath(os.path.expanduser(repo_path)))
                continue
            common = self._common_dir(real)
            repo = self._repo(common, real) if common else None
            if repo is None:
                continue
            for record in repo.worktrees.values():
                if record.prunable:
                    stale.append(common)
                    continue
                if record.branch.startswith("wt/"):
                    targets.append(record.path)
        return targets, sorted(set(stale))

    # -- classification --

    def classify(self, raw_path: str, scan: ProcessScan) -> TargetResult:
        path = os.path.abspath(os.path.expanduser(raw_path))
        result = TargetResult(path=path)
        if self.ports.realpath(path) != path:
            return result.keep("unsafe_link", "target or parent is a symlink")
        if not self.ports.exists(path):
            return result.keep("missing", "target directory does not exist")
        common = self._common_dir(path)
        if common is None:
            return result.keep("not_registered", "not inside a Git worktree")
        repo = self._repo(common, path)
        if repo is None:
            return result.keep("ambiguous", "could not list the repository's worktrees")
        result.repo = repo.main_path
        if path == repo.main_path:
            return result.keep("main_worktree", "never removes a repository's main worktree")
        record = repo.worktrees.get(path)
        if record is None:
            return result.keep("not_registered", "not a registered linked worktree")
        result.branch, result.head = record.branch, record.head
        if record.locked:
            result.keep("locked", "git worktree is locked")
        self._check_clean(path, result)
        self._check_local_nodes(path, result)
        self._check_in_progress(path, result)
        self._check_bindings(path, result)
        self._check_claim(common, path, result)
        self._check_validation_marker(common, path, result)
        self._check_holders(path, scan, result)
        self._check_landed(repo, record, result)
        if not result.reasons:
            result.status = WOULD_REMOVE
        return result

    def _check_clean(self, path: str, result: TargetResult) -> None:
        if os.path.islink(os.path.join(path, ".venv")):
            result.keep("unsafe_link", "disposable cache root is a symlink")
            return
        res = self._git(
            ["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
            path,
        )
        if res.returncode != 0:
            result.keep("ambiguous", f"git status failed: {res.stderr.strip()[:200]}")
        elif res.stdout.strip():
            count = len(res.stdout.strip().splitlines())
            result.keep("dirty", f"{count} tracked/untracked change(s)")
        ignored = self._git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], path)
        if ignored.returncode != 0:
            result.keep("ambiguous", "could not inspect ignored content")
            return
        for relative in ignored.stdout.split("\0"):
            if not relative:
                continue
            parts = Path(relative).parts
            if parts and parts[0] == ".venv" and os.path.islink(os.path.join(path, ".venv")):
                result.keep("unsafe_link", "disposable cache root is a symlink")
                return
            if len(parts) < 2 or parts[0] != ".venv" or ".." in parts:
                result.keep(
                    "ignored_precious",
                    f"ignored content is not a disposable .venv cache: {relative}",
                )
                return
            if any(os.path.islink(os.path.join(path, *parts[:i])) for i in range(1, len(parts))):
                result.keep("unsafe_link", f"ignored cache contains a symlink: {relative}")
                return

    def _check_bindings(self, path: str, result: TargetResult) -> None:
        bound = self._git(["config", "--get-regexp", r"^beadhive\.binding(session)?\."], path)
        if bound.returncode == 0 and bound.stdout.strip():
            result.keep(
                "ownership_unknown",
                "presenter binding exists; local removal cannot prove it inactive",
            )
        elif bound.returncode not in (0, 1):
            result.keep(
                "ambiguous",
                f"could not inspect local ownership bindings: {bound.returncode} "
                f"{bound.stderr.strip()[:200]}",
            )

    def _check_local_nodes(self, path: str, result: TargetResult) -> None:
        """Git omits FIFOs/sockets/devices even from ignored discovery; inspect without reads."""
        mounts = self.ports.mounts()
        if not mounts:
            result.keep("ambiguous", "mount inventory unavailable")
            return
        if any(point.startswith(path.rstrip(os.sep) + os.sep) for point, _kind in mounts):
            result.keep("unsafe_link", "nested mount is not disposable worktree content")
            return
        deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
        examined = 0

        def unreadable(error):
            raise error

        try:
            device = os.lstat(path).st_dev
            for directory, dirs, files in os.walk(path, followlinks=False, onerror=unreadable):
                if directory != path and ".git" in (*dirs, *files):
                    result.keep("ownership_unknown", "nested Git repository ownership is unproven")
                    return
                for name in (*dirs, *files):
                    examined += 1
                    if examined > 100_000 or time.monotonic() > deadline:
                        result.keep("ambiguous", "local node inventory exceeded its resource bound")
                        return
                    node = os.path.join(directory, name)
                    info = os.lstat(node)
                    if info.st_dev != device:
                        result.keep("unsafe_link", "local node inventory crosses a mount boundary")
                        return
                    if not (
                        stat.S_ISDIR(info.st_mode)
                        or stat.S_ISREG(info.st_mode)
                        or stat.S_ISLNK(info.st_mode)
                    ):
                        result.keep("ignored_precious", "special filesystem node: " + node)
                        return
        except OSError:
            result.keep("ambiguous", "local node inventory unreadable")

    def _check_in_progress(self, path: str, result: TargetResult) -> None:
        git_dir = self._git_dir(path)
        if git_dir is None:
            result.keep("ambiguous", "could not resolve the worktree's git dir")
            return
        present = [m for m in IN_PROGRESS_MARKERS if self.ports.exists(os.path.join(git_dir, m))]
        if present:
            result.keep("in_progress", "in-progress git operation: " + ", ".join(present))

    def _check_claim(self, common: str, path: str, result: TargetResult) -> None:
        """Local claim evidence is an ownership veto; never expire or rewrite it."""
        token = self._git(["config", "--get", "beadhive.claimIncarnation"], path)
        if token.returncode == 0:
            result.keep(
                "ownership_unknown", "claim incarnation exists; inactive ownership is unproven"
            )
            return
        if token.returncode != 1:
            result.keep("ambiguous", "could not inspect local claim incarnation")
            return
        admin = self._git_dir(path)
        if admin is None:
            result.keep("ambiguous", "could not resolve claim administration directory")
            return
        if os.path.lexists(os.path.join(admin, "bh-claim.json")):
            result.keep("ownership_unknown", "legacy claim record exists")
            return
        # A missing token with an extant central record is torn/ambiguous local evidence.
        # The current format is bh/worktrees/<admin-leaf>-<incarnation>/claim.json.
        root = os.path.join(common, "bh", "worktrees")
        leaf = os.path.basename(admin)
        try:
            if self.ports.realpath(root) != os.path.abspath(root):
                raise OSError("claim root is a symlink")
            with os.scandir(root) as entries:
                for number, entry in enumerate(entries):
                    if number >= 1000:
                        raise OSError("claim inventory exceeds bounded local scan")
                    if entry.name == leaf or entry.name.startswith(leaf + "-"):
                        if not entry.is_dir(follow_symlinks=False):
                            raise OSError("matching claim directory is special or symlinked")
                        # Probe readability instead of converting an access refusal into absence.
                        with os.scandir(entry.path) as contents:
                            for index, _child in enumerate(contents):
                                if index >= 1000:
                                    raise OSError("claim directory exceeds bounded local scan")
                        if os.path.lexists(os.path.join(entry.path, "claim.json")):
                            result.keep(
                                "ownership_unknown",
                                "central claim record exists without an incarnation",
                            )
                            return
        except FileNotFoundError:
            pass
        except OSError:
            result.keep("ambiguous", "could not inspect central claim records")

    def _check_validation_marker(self, common: str, path: str, result: TargetResult) -> None:
        active = os.path.join(common, VALIDATION_ACTIVE_DIR)
        for marker in (
            os.path.join(active, f"{os.path.basename(path)}.json"),
            os.path.join(path, ".bh-verify.json"),
        ):
            if os.path.lexists(marker):
                result.keep("validation_active", f"bh validation marker present: {marker}")
                return
        try:
            if self.ports.realpath(active) != os.path.abspath(active):
                raise ValueError("validation root is a symlink")
            with os.scandir(active) as entries:
                for number, entry in enumerate(entries):
                    if number >= 1000:
                        raise ValueError("validation inventory exceeds bounded local scan")
                    if not entry.name.startswith("run-") or not entry.name.endswith(".json"):
                        continue
                    pointer = _read_local_json(entry.path)
                    run_id = pointer.get("run_id")
                    if (
                        not isinstance(run_id, str)
                        or not run_id.startswith("run-")
                        or len(run_id) > 128
                        or not run_id.replace("-", "").isalnum()
                        or entry.name != run_id + ".json"
                    ):
                        raise ValueError("invalid validation run identity")
                    manifest = _read_local_json(
                        os.path.join(common, "bh", "validation", "runs", run_id, "manifest.json")
                    )
                    if manifest.get("run_id") != run_id or manifest.get("lifecycle") not in {
                        "running",
                        "completed",
                        "abandoned",
                    }:
                        raise ValueError("invalid validation lifecycle")
                    worktree = manifest.get("worktree")
                    if worktree is not None and (
                        not isinstance(worktree, str)
                        or not os.path.isabs(worktree)
                        or self.ports.realpath(worktree) != worktree
                    ):
                        raise ValueError("invalid validation worktree")
                    if manifest["lifecycle"] == "running" and worktree is None:
                        raise ValueError("running validation ownership is unknown")
                    if manifest["lifecycle"] == "running" and worktree == path:
                        result.keep("validation_active", f"running validation: {run_id}")
                        return
        except FileNotFoundError:
            # An absent active directory is normal; a dangling pointer is not.
            if os.path.lexists(active):
                result.keep("ambiguous", "validation pointer/manifest disappeared during read")
        except (OSError, ValueError):
            result.keep("ambiguous", "validation lifecycle metadata is unreadable or unsafe")

    def _check_holders(self, path: str, scan: ProcessScan, result: TargetResult) -> None:
        if not scan.complete:
            result.keep("ambiguous", f"process scan incomplete: {scan.detail}")
            return
        if scan.opaque:
            shown = ", ".join(f"{pid} ({comm})" for pid, comm in scan.opaque[:5])
            result.keep("ambiguous", f"opaque own process(es) {shown}")
            return
        holders = scan.holders_of(path)
        if holders:
            result.holders = holders
            result.keep("in_use", "held by local process(es) " + ", ".join(map(str, holders)))

    def _check_landed(self, repo: _Repo, record: _Registered, result: TargetResult) -> None:
        head = record.head
        if not head:
            result.keep("ambiguous", "worktree HEAD unknown")
            return
        resolved_any = False
        for base in self.base_refs:
            check = self._git(
                ["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], repo.main_path
            )
            if check.returncode != 0:
                continue
            resolved_any = True
            ancestor = self._git(["merge-base", "--is-ancestor", head, base], repo.main_path)
            if ancestor.returncode == 1:
                continue
            if ancestor.returncode != 0:
                result.keep("ambiguous", f"ancestry check against {base} failed")
                return
            line = self._first_parent(repo, base)
            if line is None:
                result.keep("ambiguous", f"could not read {base} first-parent history")
                return
            if head in line:
                result.keep(
                    "no_landed_work",
                    f"HEAD is on {base}'s first-parent line (fresh or fast-forwarded seat)",
                )
                return
            # A local merge may still be unpushed. Persisted remote-tracking history is
            # required; this recovery command never fetches or opens HQ.
            published = base if base.startswith("origin/") else f"origin/{base}"
            publication = self._git(
                ["merge-base", "--is-ancestor", head, published], repo.main_path
            )
            if publication.returncode != 0:
                result.keep("publication_unknown", f"HEAD publication is not proven by {published}")
                return
            result.landed_on = published
            return
        if not resolved_any:
            result.keep("ambiguous", "no base ref resolves: " + ", ".join(self.base_refs))
        else:
            result.keep("not_landed", "HEAD is not reachable from " + ", ".join(self.base_refs))

    # -- effects --

    def remove(self, result: TargetResult) -> None:
        """Refresh every local proof immediately before Git's non-forced removal."""
        self._repos.clear()
        recheck = self.classify(result.path, self.ports.process_scan())
        if recheck.status != WOULD_REMOVE or recheck.head != result.head:
            result.status = KEPT
            result.reasons.extend(recheck.reasons or ["changed_head"])
            result.detail = recheck.detail or "HEAD changed since preflight"
            result.holders = recheck.holders
            return
        result.bytes = self.ports.disk_usage(result.path)
        repo = self._repos.get(self._common_dir(result.path) or "")
        if repo is None:
            result.keep("ambiguous", "repository vanished before removal")
            return
        res = self._git(["worktree", "remove", result.path], self._repo_cwd(repo))
        if res.returncode == 0 and not self.ports.exists(result.path):
            result.status = REMOVED
        else:
            result.status = FAILED
            result.reasons.append("remove_failed")
            result.detail = res.stderr.strip()[:300] or "worktree still present after remove"

    def prune_admin(self, common_dir: str) -> dict:
        repo = self._repos.get(common_dir)
        entry = {"repo": repo.main_path if repo else common_dir, "status": WOULD_REMOVE}
        if self.dry_run or repo is None:
            entry["status"] = WOULD_REMOVE if repo else KEPT
            return entry
        res = self._git(["worktree", "prune"], self._repo_cwd(repo))
        entry["status"] = "pruned" if res.returncode == 0 else FAILED
        if res.returncode != 0:
            entry["detail"] = res.stderr.strip()[:300]
        return entry

    # -- measurement --

    def measure(self, paths: Iterable[str]) -> dict[str, FsUsage]:
        mounts = sorted(self.ports.mounts(), key=lambda m: len(m[0]), reverse=True)
        usage: dict[str, FsUsage] = {}
        for path in paths:
            probe = path
            while probe and not self.ports.exists(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            mount, fstype = next(
                (
                    (point, kind)
                    for point, kind in mounts
                    if probe == point or probe.startswith(point.rstrip("/") + "/")
                ),
                (probe, "unknown"),
            )
            if mount in usage:
                continue
            try:
                total, used, avail = self.ports.statvfs(
                    mount if self.ports.exists(mount) else probe
                )
            except OSError:
                continue
            usage[mount] = FsUsage(
                mount, fstype, fstype in MEMORY_BACKED_FSTYPES, total, used, avail
            )
        return usage

    # -- the run --

    def run(
        self,
        targets: Sequence[str],
        *,
        stale_repos: Sequence[str] = (),
        mode: str,
        measure_also: Sequence[str] = (),
    ) -> dict:
        unique = list(dict.fromkeys(os.path.abspath(os.path.expanduser(t)) for t in targets))
        measured = [*unique, *(self.ports.realpath(p) for p in measure_also)]
        before = self.measure(measured)
        memory_before = self.ports.memory_available()
        scan = self.ports.process_scan()
        results = [self.classify(path, scan) for path in unique]
        for result in results:
            if result.status == WOULD_REMOVE and not self.dry_run:
                self.remove(result)
            elif result.status == WOULD_REMOVE:
                result.bytes = self.ports.disk_usage(result.path)
        admin = [self.prune_admin(common) for common in stale_repos]
        after = before if self.dry_run else self.measure(measured)
        reclaimed = sum(r.bytes or 0 for r in results if r.status == REMOVED)
        reclaimable = sum(r.bytes or 0 for r in results if r.status == WOULD_REMOVE)
        filesystems = []
        for mount, usage in before.items():
            post = after.get(mount, usage)
            filesystems.append(
                {
                    "mount": mount,
                    "fstype": usage.fstype,
                    "memory_backed": usage.memory_backed,
                    "before": usage.to_dict(),
                    "after": post.to_dict(),
                    "freed_bytes": max(0, post.available_bytes - usage.available_bytes),
                }
            )
        counts = {status: 0 for status in (REMOVED, WOULD_REMOVE, KEPT, FAILED)}
        for result in results:
            counts[result.status] += 1
        return {
            "schema": SCHEMA,
            "op": f"worktree.{mode}",
            "mode": "local-safe",
            "hq_independent": True,
            "dry_run": self.dry_run,
            "base_refs": list(self.base_refs),
            "process_scan": {
                "complete": scan.complete,
                "detail": scan.detail,
                "unreadable_foreign_processes": scan.unreadable_foreign,
                "opaque_own_processes": [{"pid": pid, "comm": comm} for pid, comm in scan.opaque],
                "strict": True,
            },
            "summary": counts,
            "reclaimed_bytes": reclaimed,
            "reclaimable_bytes": reclaimable,
            "reclaimed_bytes_basis": "conservative exclusive allocated-byte estimate",
            "memory_available_bytes": {
                "before": memory_before,
                "after": memory_before if self.dry_run else self.ports.memory_available(),
            },
            "filesystems": filesystems,
            "targets": [result.to_dict() for result in results],
            "stale_admin": admin,
        }


def _parse_porcelain(text: str, realpath: Callable[[str], str]) -> list[_Registered]:
    records: list[_Registered] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if "worktree" in current:
            branch = current.get("branch", "")
            records.append(
                _Registered(
                    path=os.path.abspath(current["worktree"]),
                    head=current.get("HEAD", ""),
                    branch=branch.removeprefix("refs/heads/"),
                    locked="locked" in current,
                    prunable="prunable" in current,
                    bare="bare" in current,
                )
            )

    for line in text.splitlines():
        if not line.strip():
            flush()
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    flush()
    return records


# ---- CLI --------------------------------------------------------------------------------------


VERB = "local-reclaim"
EPILOG = (
    "First-line partial-degradation procedure (docs/WORKTREES.md): runs without HQ SQL, "
    "fleet/runtime config, or the bh daemon. Only clean, unlocked, unused, provably landed "
    "linked worktrees are removed; everything else is kept and reported (fail closed). "
    "With no PATH/--root/--repo/--targets-from it scans the default worktree roots. "
    "Exit status: 0 ok, 1 a removal failed or a PATH named on the command line was kept, "
    "2 usage."
)


def _default_roots() -> list[str]:
    env = os.environ.get("BH_WORKTREES") or os.environ.get("WS_WORKTREES")
    home = os.environ.get("BH_HOME") or os.path.join(os.path.expanduser("~"), ".beadhive")
    candidates = [
        *([os.path.expanduser(env)] if env else []),
        os.path.join(tempfile.gettempdir(), "bh-worktrees"),
        os.path.join(home, "worktrees"),
    ]
    return list(dict.fromkeys(c for c in candidates if os.path.isdir(c)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"bh worktree {VERB}",
        description=(
            "HQ-independent, fail-closed worktree space reclamation for partial degradation: "
            "removes only locally provable safe worktrees."
        ),
        epilog=EPILOG,
    )
    parser.add_argument(
        "paths", nargs="*", help="worktree directories to remove (bead ids need HQ; use paths)"
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="scan a worktree root for leaves (repeatable; default when nothing else is "
        "given: $BH_WORKTREES plus <tmp>/bh-worktrees and ~/.beadhive/worktrees)",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="consider this repository's wt/* linked worktrees and prune its stale admin "
        "entries (repeatable)",
    )
    parser.add_argument(
        "--targets-from",
        default="",
        help="read candidate paths from FILE, one per line ('-' = stdin)",
    )
    parser.add_argument(
        "--base",
        action="append",
        default=[],
        help="landing base ref (repeatable; default: main, master, origin/main, origin/master)",
    )
    parser.add_argument(
        "--dry-run",
        "--preview",
        action="store_true",
        help="classify and measure only; remove nothing",
    )
    parser.add_argument(
        "--strict-process-scan",
        action="store_true",
        help="compatibility flag: hidden own processes always keep every target "
        "(non-dumpable session managers such as sshd-session or systemd --user)",
    )
    parser.add_argument("--json", action="store_true", help="emit the structured result as JSON")
    return parser


def _read_targets_file(name: str, stdin: TextIO) -> list[str]:
    text = stdin.read() if name == "-" else Path(name).read_text()
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _human(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def render_text(report: dict, out: TextIO) -> None:
    verb = "would remove" if report["dry_run"] else "removed"
    for target in report["targets"]:
        status = target["status"]
        if status in (REMOVED, WOULD_REMOVE):
            out.write(
                f"  ✓ {status:<12} {target['path']}  ({_human(target['size_bytes'])}, "
                f"landed on {target['landed_on']})\n"
            )
        else:
            out.write(
                f"  ✗ {status:<12} {target['path']}  [{', '.join(target['reasons'])}] "
                f"{target['detail']}\n"
            )
    for entry in report["stale_admin"]:
        out.write(f"  · stale admin {entry['status']}: {entry['repo']}\n")
    s = report["summary"]
    total = report["reclaimed_bytes"] if not report["dry_run"] else report["reclaimable_bytes"]
    out.write(
        f"local-safe {report['op']}: {verb} {s[REMOVED] + s[WOULD_REMOVE]}, kept {s[KEPT]}, "
        f"failed {s[FAILED]}; {verb} {_human(total)} (HQ not consulted)\n"
    )
    for fs in report["filesystems"]:
        b, a = fs["before"], fs["after"]
        tag = " [RAM-backed]" if fs["memory_backed"] else ""
        out.write(
            f"  {fs['mount']} ({fs['fstype']}){tag}: used {b['used_percent']}% -> "
            f"{a['used_percent']}%, available {_human(b['available_bytes'])} -> "
            f"{_human(a['available_bytes'])}\n"
        )
    scan = report["process_scan"]
    memory = report["memory_available_bytes"]
    if memory["before"] is not None:
        out.write(
            f"  MemAvailable: {_human(memory['before'])} -> {_human(memory['after'])} "
            "(host observation)\n"
        )
    if not scan["complete"]:
        out.write(f"  ! process scan incomplete ({scan['detail']}): every target kept\n")
    if scan["opaque_own_processes"]:
        shown = ", ".join(f"{p['pid']} ({p['comm']})" for p in scan["opaque_own_processes"][:8])
        verdict = "every target kept"
        out.write(f"  · kernel-hidden own processes: {shown} — {verdict}\n")


def main(
    argv: Sequence[str],
    *,
    ports: LocalReclaimPorts | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    stdin: TextIO | None = None,
    mode: str = VERB,
) -> int:
    """Parse ``bh worktree local-reclaim …`` and run it; returns the exit status."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as exc:  # --help (0) or a usage error (2)
        return int(exc.code or 0)
    try:
        listed = (
            _read_targets_file(args.targets_from, stdin or sys.stdin) if args.targets_from else []
        )
    except OSError as exc:
        err.write(f"✗ cannot read --targets-from {args.targets_from}: {exc}\n")
        return 2
    reclaimer = LocalReclaimer(
        ports or _default_ports(),
        base_refs=args.base,
        dry_run=args.dry_run,
        strict_process_scan=args.strict_process_scan,
    )
    targets = [*args.paths, *listed]
    roots = list(args.root)
    if not (targets or roots or args.repo):
        roots = _default_roots()
        if not roots:
            err.write("✗ no worktree root found — pass PATH, --root, --repo or --targets-from\n")
            return 2
    targets.extend(reclaimer.discover_roots(roots))
    repo_targets, stale = reclaimer.discover_repos(args.repo)
    targets.extend(repo_targets)
    report = reclaimer.run(targets, stale_repos=stale, mode=mode, measure_also=roots)
    if args.json:
        out.write(json.dumps(report, indent=2) + "\n")
    else:
        render_text(report, out)
    if report["summary"][FAILED]:
        return 1
    named = {reclaimer.ports.realpath(path) for path in args.paths}
    if any(t["path"] in named and t["status"] == KEPT for t in report["targets"]):
        return 1
    return 0


__all__ = [
    "SCHEMA",
    "VERB",
    "FsUsage",
    "GitResult",
    "LocalReclaimPorts",
    "LocalReclaimer",
    "ProcessScan",
    "TargetResult",
    "main",
    "render_text",
]
