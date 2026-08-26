"""Canonical locations for Beadhive-owned private hive state.

There are two deliberately separate private roots:

* :func:`git_private_root` is ``<git-common-dir>/bh``.  It holds control
  metadata shared by a main clone and all of its linked worktrees.
* :func:`repo_private_root` is ``<hive>/.bh`` in the primary checkout.  It
  holds artifacts and caches shared by every worktree of that hive.
* :func:`worktree_private_root` is ``<worktree>/.bh`` only for data that is
  explicitly local to one checkout, such as its observability overlay.

Resolvers never create a directory.  Writers must opt into creation with the
matching ``ensure_*`` function.  A non-repository path, a bare repository, or
malformed Git output resolves to ``None``; callers can consequently take their
existing no-cache / fresh-work path without risking a filesystem mutation.

Git-native administration is intentionally outside this API: refs, hooks,
``info/exclude``, ``info/attributes``, and Git extensions remain Git's paths.
``LEGACY_PRIVATE_PATHS`` records the old Beadhive-specific locations so later
migration beads have one mapping rather than rediscovering path strings.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .run import run

GIT_PRIVATE_DIRNAME = "bh"
REPO_PRIVATE_DIRNAME = ".bh"

PrivateRoot = Literal["git", "repo", "worktree"]


@dataclass(frozen=True)
class PrivatePathTarget:
    """One path relative to a canonical private root."""

    root: PrivateRoot
    relative: Path


@dataclass(frozen=True)
class LegacyPathMigration:
    """Canonical target(s) and semantics for one legacy Beadhive path."""

    targets: tuple[PrivatePathTarget, ...]
    semantics: str


@dataclass(frozen=True)
class PrivateStateFinding:
    """One read-only cleanup candidate from :func:`inventory_private_state`.

    Live state is omitted. Unmeasurable ownership becomes an explicit ``protected_*`` finding,
    never a cleanup candidate. The inventory itself never creates, renames, or removes a path.
    """

    kind: str
    path: Path
    detail: str


# This is an inventory contract for later migration beads, not a live migration.
# Multiple targets are intentional: the flat validation ledger is decomposed into
# authoritative run history and a reconstructable green-verdict lookup.
LEGACY_PRIVATE_PATHS: dict[str, LegacyPathMigration] = {
    ".git/bh-validation-ledger.json": LegacyPathMigration(
        targets=(
            PrivatePathTarget("git", Path("validation/runs/<run-id>/manifest.json")),
            PrivatePathTarget("git", Path("validation/verdicts/<tree>/<command-hash>.json")),
        ),
        semantics=(
            "import rows as authoritative run manifests; only qualifying completed-green "
            "runs seed the reconstructable verdict index"
        ),
    ),
    ".git/bh-release-bump-gate.json": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("release/bump-gate.json")),),
        semantics="release lifecycle and ownership control record",
    ),
    ".git/bh-release-bump-gate.log": LegacyPathMigration(
        targets=(PrivatePathTarget("repo", Path("release/bump-gates/<tree>/gate.log")),),
        semantics="shared diagnostic artifact referenced by the control record",
    ),
    ".git/bh/validation/active/<verify-worktree-id>.json": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("validation/active/<run-id>.json")),),
        semantics=(
            "0.15.1 compatibility input for a reconstructable liveness index, never run truth"
        ),
    ),
    ".git/bh-build": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("build")),),
        semantics="clone-local build scratch state",
    ),
    ".git/worktrees/<worktree>/bh-claim.json": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("worktrees/<worktree-id>/claim.json")),),
        semantics="centralized per-worktree claim record",
    ),
    ".bh/testreport/<tree>": LegacyPathMigration(
        targets=(
            PrivatePathTarget("repo", Path("validation/runs/<run-id>/reports")),
            PrivatePathTarget("repo", Path("validation/runs/.summary/<tree>/results.json")),
        ),
        semantics=(
            "latest legacy raw reports become artifacts of a deterministic imported run; "
            "bounded per-tree retry history becomes the canonical derived summary"
        ),
    ),
    ".bh/otel.env": LegacyPathMigration(
        targets=(PrivatePathTarget("worktree", Path("observability/otel.env")),),
        semantics="explicitly worktree-local observability overlay",
    ),
    ".bh-verify.json": LegacyPathMigration(
        targets=(PrivatePathTarget("git", Path("validation/active/<run-id>.json")),),
        semantics=(
            "read-only orphan-compatibility input for the derived active index; never write "
            "a marker into a verification checkout"
        ),
    ),
}


def _metadata(hive: Path) -> tuple[Path, Path, Path] | None:
    """Return absolute ``(common_dir, toplevel, primary_toplevel)`` for a worktree.

    A ``rev-parse`` probe resolves the common directory and current top level;
    a second ``worktree list`` probe identifies the primary checkout.  Malformed
    or partial output is a failure rather than an invitation to guess a path.
    ``Path.is_dir`` also makes a deleted metadata target a safe miss.  No
    directory is created here.
    """
    hive = Path(hive).expanduser().absolute()
    if not hive.is_dir():
        return None
    try:
        result = run(
            [
                "git",
                "-C",
                str(hive),
                "rev-parse",
                "--git-common-dir",
                "--is-bare-repository",
                "--show-toplevel",
            ],
            check=False,
            capture=True,
        )
    except (OSError, ValueError):
        return None
    lines = (getattr(result, "stdout", "") or "").splitlines()
    if getattr(result, "returncode", 1) != 0 or len(lines) != 3:
        return None
    common_raw, bare, top_raw = (line.strip() for line in lines)
    if bare != "false" or not common_raw or not top_raw:
        return None
    common = Path(common_raw)
    if not common.is_absolute():
        common = hive / common
    top = Path(top_raw)
    if not top.is_absolute():  # defensive: Git normally returns an absolute top level
        return None
    try:
        common, top = common.resolve(), top.resolve()
    except OSError:
        return None
    if not common.is_dir() or not top.is_dir():
        return None
    try:
        listing = run(
            ["git", "-C", str(hive), "worktree", "list", "--porcelain"],
            check=False,
            capture=True,
        )
    except (OSError, ValueError):
        return None
    worktree_lines = (getattr(listing, "stdout", "") or "").splitlines()
    primary_raw = next(
        (
            line.removeprefix("worktree ").strip()
            for line in worktree_lines
            if line.startswith("worktree ")
        ),
        "",
    )
    if getattr(listing, "returncode", 1) != 0 or not primary_raw:
        return None
    try:
        primary = Path(primary_raw).resolve()
    except OSError:
        return None
    return (common, top, primary) if primary.is_dir() else None


def git_private_root(hive: str | Path) -> Path | None:
    """Read-only ``<git-common-dir>/bh`` resolver, or ``None`` on a safe miss."""
    metadata = _metadata(Path(hive))
    return metadata[0] / GIT_PRIVATE_DIRNAME if metadata else None


def repo_private_root(hive: str | Path) -> Path | None:
    """Read-only shared ``<hive>/.bh`` resolver, or ``None`` on a safe miss.

    ``hive`` may name the main clone or any linked worktree; both resolve to the
    primary checkout's one shared artifact/cache root.
    """
    metadata = _metadata(Path(hive))
    return metadata[2] / REPO_PRIVATE_DIRNAME if metadata else None


def worktree_private_root(hive: str | Path) -> Path | None:
    """Read-only ``<worktree>/.bh`` overlay for explicitly local state only."""
    metadata = _metadata(Path(hive))
    return metadata[1] / REPO_PRIVATE_DIRNAME if metadata else None


def _child(root: Path | None, parts: tuple[str | Path, ...]) -> Path | None:
    if root is None:
        return None
    child = Path(*parts)
    if child.is_absolute() or ".." in child.parts:
        raise ValueError("private-path children must stay below their canonical root")
    return root / child


def git_private_path(hive: str | Path, *parts: str | Path) -> Path | None:
    """A validated child of :func:`git_private_root`; this does not create it."""
    return _child(git_private_root(hive), parts)


def repo_private_path(hive: str | Path, *parts: str | Path) -> Path | None:
    """A validated child of :func:`repo_private_root`; this does not create it."""
    return _child(repo_private_root(hive), parts)


def worktree_private_path(hive: str | Path, *parts: str | Path) -> Path | None:
    """A validated child of :func:`worktree_private_root`; this does not create it."""
    return _child(worktree_private_root(hive), parts)


def ensure_git_private_root(hive: str | Path) -> Path | None:
    """Create and return the git-private root, or safely return ``None`` on failure."""
    root = git_private_root(hive)
    try:
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root


def ensure_repo_private_root(hive: str | Path) -> Path | None:
    """Create and return the repo-private root, or safely return ``None`` on failure."""
    root = repo_private_root(hive)
    try:
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root


def ensure_worktree_private_root(hive: str | Path) -> Path | None:
    """Create a local overlay root only where a caller explicitly requires one."""
    root = worktree_private_root(hive)
    try:
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root


def _worktree_paths(hive: Path) -> tuple[Path, ...]:
    """Registered checkout paths, or an empty tuple when Git cannot provide an inventory."""
    try:
        result = run(
            ["git", "-C", str(hive), "worktree", "list", "--porcelain"],
            check=False,
            capture=True,
        )
    except (OSError, ValueError):
        return ()
    if getattr(result, "returncode", 1) != 0:
        return ()
    paths = []
    for line in (getattr(result, "stdout", "") or "").splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            paths.append(Path(line.removeprefix("worktree ").strip()).resolve())
        except OSError:
            continue
    return tuple(paths)


def _path_kind(path: Path) -> str:
    """Classify one directory entry without following symlinks."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def _children(path: Path) -> tuple[Path, ...] | None:
    """Return direct children only for a plain directory; ``None`` fails inspection closed."""
    if _path_kind(path) != "directory":
        return None
    try:
        return tuple(path.iterdir())
    except OSError:
        return None


def _descendant_kind(root: Path, *parts: str) -> str:
    """Classify an in-root descendant only when every existing ancestor is a plain directory."""
    root_kind = _path_kind(root)
    if root_kind == "missing":
        return "missing"
    if root_kind != "directory":
        return f"{root_kind}_ancestor"
    current = root
    for index, part in enumerate(parts):
        current /= part
        kind = _path_kind(current)
        if index == len(parts) - 1:
            return kind
        if kind == "missing":
            return "missing"
        if kind != "directory":
            return f"{kind}_ancestor"
    return root_kind


def _descendant_children(root: Path, *parts: str) -> tuple[Path, ...] | None:
    path = root.joinpath(*parts)
    return _children(path) if _descendant_kind(root, *parts) == "directory" else None


def _json_dict(path: Path) -> dict | None:
    """Read a plain regular JSON object without following a symlink or special file."""
    if _path_kind(path) != "file":
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _pid_status(pid: object) -> str:
    """``live``/``dead``/``unknown`` for one local pid; uncertainty is never cleanup proof."""
    if type(pid) is not int or pid <= 0:
        return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "live"
    except OSError:
        return "unknown"
    return "live"


def _claim_incarnation(config_path: Path) -> str:
    """Read Git's per-worktree claim token without invoking a mutating config seam."""
    if _path_kind(config_path) != "file":
        return ""
    try:
        raw = config_path.read_text()
    except OSError:
        return ""
    match = re.search(r"(?mi)^\s*claimIncarnation\s*=\s*(\S+)\s*$", raw)
    return match.group(1) if match else ""


def inventory_private_state(hive: str | Path) -> tuple[PrivateStateFinding, ...]:
    """Inventory Beadhive legacy and reclaimable private state without changing it.

    Findings cover the complete migration tail: legacy spellings, abandoned validation
    executions and orphaned artifacts, central claims whose Git worktree incarnation is gone,
    dead local-build scratch worktrees, and release logs/control records no longer backed by a
    live local gate. A running validation, registered claim, or live build pid is absent.
    Unreadable, corrupt, special, symlinked, outside-root, or remotely owned state is returned as
    an explicit ``protected_*`` finding and is never presented as a deletion candidate.
    """
    hive_path = Path(hive).expanduser().absolute()
    metadata = _metadata(hive_path)
    if metadata is None:
        return ()
    common, _top, primary = metadata
    git_root = common / GIT_PRIVATE_DIRNAME
    repo_root = primary / REPO_PRIVATE_DIRNAME
    worktrees = _worktree_paths(hive_path)
    findings: list[PrivateStateFinding] = []
    finding_keys: set[tuple[str, Path]] = set()

    def add(kind: str, path: Path, detail: str) -> None:
        key = (kind, path)
        if key in finding_keys:
            return
        finding_keys.add(key)
        findings.append(PrivateStateFinding(kind, path, detail))

    def protected(kind: str, path: Path, detail: str) -> None:
        add(f"protected_{kind}", path, detail)

    git_root_kind = _path_kind(git_root)
    repo_root_kind = _path_kind(repo_root)
    if git_root_kind not in {"missing", "directory"}:
        protected(
            "private_root",
            git_root,
            f"git-private root is {git_root_kind}; nothing below it is safe to inventory",
        )
    if repo_root_kind not in {"missing", "directory"}:
        protected(
            "private_root",
            repo_root,
            f"repo-private root is {repo_root_kind}; nothing below it is safe to inventory",
        )

    # Concrete pre-two-root spellings.  Pattern-shaped entries in LEGACY_PRIVATE_PATHS are
    # represented by their actual roots here; no compatibility read creates a path.
    for path in (
        common / "bh-validation-ledger.json",
        common / "bh-release-bump-gate.json",
        common / "bh-release-bump-gate.log",
        common / "bh-build",
    ):
        if _path_kind(path) != "missing":
            add("legacy_path", path, "Beadhive compatibility path remains after migration")
    legacy_triage = repo_root / "testreport"
    if _descendant_kind(repo_root, "testreport") != "missing":
        add("legacy_path", legacy_triage, "Beadhive compatibility path remains after migration")
    admins = _children(common / "worktrees") or ()
    for admin in admins:
        legacy_claim = admin / "bh-claim.json"
        if _path_kind(legacy_claim) != "missing":
            add("legacy_path", legacy_claim, "legacy per-worktree claim record")
    for worktree_path in worktrees:
        overlay_root = worktree_path / REPO_PRIVATE_DIRNAME
        overlay_kind = _path_kind(overlay_root)
        if overlay_kind not in {"missing", "directory"}:
            protected(
                "private_root",
                overlay_root,
                f"worktree-private root is {overlay_kind}; overlay state is protected",
            )
            continue
        for legacy in (worktree_path / ".bh/otel.env", worktree_path / ".bh-verify.json"):
            if _path_kind(legacy) != "missing":
                add("legacy_path", legacy, "legacy worktree-local Beadhive state")
    active_children = _descendant_children(git_root, "validation", "active")
    active_kind = _descendant_kind(git_root, "validation", "active")
    if active_kind not in {"missing", "directory"} or (
        active_kind == "directory" and active_children is None
    ):
        protected(
            "validation_state",
            git_root / "validation" / "active",
            "validation liveness root is special, symlinked, or unreadable",
        )
    if active_children is not None:
        for pointer in (path for path in active_children if path.suffix == ".json"):
            if _path_kind(pointer) != "file":
                protected(
                    "validation_state",
                    pointer,
                    "validation liveness pointer is not a plain regular file",
                )
                continue
            value = _json_dict(pointer)
            run_id = value.get("run_id") if value is not None else None
            if isinstance(run_id, str) and run_id and pointer.name != f"{run_id}.json":
                add(
                    "legacy_path",
                    pointer,
                    "legacy worktree-keyed validation liveness pointer",
                )

    manifests: dict[str, dict] = {}
    protected_raw_ids: set[str] = set()
    runs = git_root / "validation" / "runs"
    artifacts_root = repo_root / "validation" / "runs"
    runs_kind = _descendant_kind(git_root, "validation", "runs")
    run_children = _descendant_children(git_root, "validation", "runs")
    validation_control_unmeasurable = runs_kind not in {"missing", "directory"} or (
        runs_kind == "directory" and run_children is None
    )
    if validation_control_unmeasurable:
        protected(
            "validation_state",
            runs,
            "validation control root is special, symlinked, or unreadable",
        )
    if run_children is not None:
        for directory in sorted(run_children, key=lambda path: path.name):
            run_id = directory.name
            manifest_path = directory / "manifest.json"
            if _path_kind(directory) != "directory":
                protected_raw_ids.add(run_id)
                protected(
                    "validation_state",
                    directory,
                    "validation run entry is symlinked, special, or unreadable",
                )
                continue
            manifest = _json_dict(manifest_path)
            if manifest is None or manifest.get("run_id") != run_id:
                protected_raw_ids.add(run_id)
                protected(
                    "validation_state",
                    manifest_path,
                    "manifest is missing, unreadable, corrupt, or mismatched; matching raw "
                    "artifacts are protected",
                )
                continue
            lifecycle = manifest.get("lifecycle")
            if lifecycle not in {"running", "completed", "abandoned"}:
                protected_raw_ids.add(run_id)
                protected(
                    "validation_state",
                    manifest_path,
                    "manifest lifecycle is unrecognized; matching raw artifacts are protected",
                )
                continue
            manifests[run_id] = manifest
            artifacts = manifest.get("artifacts")
            raw = artifacts.get("directory") if isinstance(artifacts, dict) else None
            expected = artifacts_root / run_id
            raw_kind = _descendant_kind(repo_root, "validation", "runs", run_id)
            raw_matches = isinstance(raw, str) and Path(os.path.abspath(raw)) == Path(
                os.path.abspath(expected)
            )
            if isinstance(raw, str) and raw and not raw_matches:
                protected_raw_ids.add(run_id)
                protected(
                    "validation_state",
                    manifest_path,
                    "manifest artifact directory escapes or mismatches the canonical run root; "
                    "the recorded external path is ignored",
                )
                continue
            if raw_kind not in {"missing", "directory"}:
                protected_raw_ids.add(run_id)
                protected(
                    "validation_artifact",
                    expected,
                    f"matching raw artifact is {raw_kind}, not a plain in-root directory",
                )
                continue
            if lifecycle != "abandoned":
                continue
            add("abandoned_validation_run", directory, f"validation run {run_id} is abandoned")
            if raw_matches and raw_kind == "directory":
                add(
                    "abandoned_validation_artifact",
                    expected,
                    f"raw artifacts for abandoned validation run {run_id}",
                )
    artifacts_root_kind = _descendant_kind(repo_root, "validation", "runs")
    artifact_children = _descendant_children(repo_root, "validation", "runs")
    if artifacts_root_kind not in {"missing", "directory"} or (
        artifacts_root_kind == "directory" and artifact_children is None
    ):
        protected(
            "validation_artifact",
            artifacts_root,
            "validation artifact root is not a plain directory",
        )
    if artifact_children is not None:
        for directory in sorted(artifact_children, key=lambda path: path.name):
            if directory.name.startswith("."):
                continue
            kind = _path_kind(directory)
            if validation_control_unmeasurable or directory.name in protected_raw_ids:
                protected(
                    "validation_artifact",
                    directory,
                    "matching validation control state is unmeasurable; raw artifact protected",
                )
            elif directory.name not in manifests and kind == "directory":
                add(
                    "orphaned_validation_artifact",
                    directory,
                    "artifact directory has no canonical validation manifest",
                )
            elif kind != "directory":
                protected(
                    "validation_artifact",
                    directory,
                    f"artifact entry is {kind}, not a plain directory",
                )

    # The set of claim keys backed by a registered Git worktree incarnation.  Reading
    # config.worktree directly avoids claim_authority's intentional migration/write seam.
    live_claim_keys = {"main"}
    admin_root = common / "worktrees"
    admin_kind = _path_kind(admin_root)
    raw_admin_children = _children(admin_root)
    claim_registry_unmeasurable = admin_kind not in {"missing", "directory"} or (
        admin_kind == "directory" and raw_admin_children is None
    )
    admin_children = raw_admin_children or ()
    registered_admin_prefixes: set[str] = set()
    for admin in admin_children:
        # A child name is already a bounded Git-admin identity even when the entry itself
        # cannot be inspected. Protect only claims carrying that exact leaf prefix; the
        # registry-wide fallback above remains for cases where no child identity is knowable.
        registered_admin_prefixes.add(f"{admin.name}-")
        if _path_kind(admin) != "directory" or _children(admin) is None:
            continue
        token = _claim_incarnation(admin / "config.worktree")
        if token:
            live_claim_keys.add(f"{admin.name}-{token}")
    claim_children = _descendant_children(git_root, "worktrees")
    claims_kind = _descendant_kind(git_root, "worktrees")
    if claims_kind not in {"missing", "directory"} or (
        claims_kind == "directory" and claim_children is None
    ):
        protected("claim", git_root / "worktrees", "claim root is special or unreadable")
    if claim_children is not None:
        for directory in sorted(claim_children, key=lambda path: path.name):
            if _path_kind(directory) != "directory":
                protected("claim", directory, "claim entry is not a plain directory")
                continue
            record = directory / "claim.json"
            record_kind = _path_kind(record)
            if record_kind not in {"missing", "file"}:
                protected("claim", record, f"claim record is {record_kind}")
            elif record_kind == "file" and directory.name not in live_claim_keys:
                could_be_registered = claim_registry_unmeasurable or any(
                    directory.name.startswith(prefix) for prefix in registered_admin_prefixes
                )
                if could_be_registered:
                    protected(
                        "claim",
                        record,
                        "Git worktree incarnation is unmeasurable; claim remains protected",
                    )
                else:
                    add("stale_claim", record, "claim has no registered Git worktree incarnation")

    build_children = _descendant_children(git_root, "build")
    build_kind = _descendant_kind(git_root, "build")
    if build_kind not in {"missing", "directory"} or (
        build_kind == "directory" and build_children is None
    ):
        protected("build_state", git_root / "build", "build root is special or unreadable")
    if build_children is not None:
        for directory in sorted(build_children, key=lambda path: path.name):
            if not directory.name.startswith("build-"):
                continue
            if _path_kind(directory) != "directory":
                protected("build_state", directory, "build entry is not a plain directory")
                continue
            fields = directory.name.split("-", 2)
            pid = int(fields[1]) if len(fields) == 3 and fields[1].isdigit() else None
            status = _pid_status(pid)
            if status == "dead":
                add("build_leftover", directory, f"local-build owner pid {pid} is dead")
            elif status == "unknown":
                protected("build_state", directory, "build owner pid is unmeasurable")

    marker = git_root / "release" / "bump-gate.json"
    marker_kind = _descendant_kind(git_root, "release", "bump-gate.json")
    marker_value = _json_dict(marker) if marker_kind == "file" else None
    marker_live = False
    marker_protected = False
    if marker_kind != "missing" and marker_value is None:
        marker_protected = True
        protected(
            "release_state",
            marker,
            f"release marker is {marker_kind}, unreadable, or corrupt; all gate logs protected",
        )
    elif marker_value is not None:
        marker_host = marker_value.get("host", "")
        marker_tree = marker_value.get("tree")
        if not isinstance(marker_tree, str) or not marker_tree or not isinstance(marker_host, str):
            marker_protected = True
        elif marker_host:
            try:
                from . import host

                if marker_host != host.host_id():
                    marker_protected = True
            except Exception:
                marker_protected = True
        status = _pid_status(marker_value.get("pid")) if not marker_protected else "unknown"
        if marker_protected or status == "unknown":
            marker_protected = True
            protected(
                "release_state",
                marker,
                "release marker ownership is invalid, remote, or unmeasurable; all gate logs "
                "protected",
            )
        elif status == "dead":
            add("release_leftover", marker, "background release gate owner is dead")
        else:
            marker_live = True
    active_tree = str(marker_value.get("tree") or "") if marker_live and marker_value else ""
    log_roots = _descendant_children(repo_root, "release", "bump-gates")
    logs_kind = _descendant_kind(repo_root, "release", "bump-gates")
    if logs_kind not in {"missing", "directory"} or (
        logs_kind == "directory" and log_roots is None
    ):
        protected(
            "release_artifact",
            repo_root / "release" / "bump-gates",
            "release log root is special, symlinked, or unreadable",
        )
    if log_roots is not None and not marker_protected:
        for tree_dir in sorted(log_roots, key=lambda path: path.name):
            if _path_kind(tree_dir) != "directory":
                protected("release_artifact", tree_dir, "release log entry is not a directory")
                continue
            log = tree_dir / "gate.log"
            log_kind = _path_kind(log)
            if log_kind == "missing":
                continue
            if log_kind != "file":
                protected("release_artifact", log, f"release gate log is {log_kind}")
            elif tree_dir.name != active_tree:
                add("release_artifact_leftover", log, "release gate log has no live control owner")

    return tuple(sorted(findings, key=lambda item: (item.kind, str(item.path))))
