#!/usr/bin/env python3
"""Bounded, fail-closed Pants cache topology for independent worktrees."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_MIN_FREE_BYTES = 2 * 1024**3
DEFAULT_MIN_FREE_INODES = 50_000
SHARED_MODE = 0o775
PRIVATE_MODE = 0o700
FALLBACK = "just check"


class CacheSafetyError(RuntimeError):
    """The cache cannot be used without weakening validation safety."""


class CacheActiveError(CacheSafetyError):
    """Maintenance was requested while a Pants process holds the cache lease."""


@dataclass(frozen=True)
class CacheLayout:
    repository: Path
    cache_root: Path
    local_store: Path
    worktree_root: Path
    named_caches: Path
    pants_workdir: Path
    pants_subprocessdir: Path
    uv_cache: Path
    pex_root: Path
    sandbox_root: Path
    maintenance_lock: Path

    def environment(self) -> dict[str, str]:
        return {
            "PANTS_LOCAL_STORE_DIR": str(self.local_store),
            "PANTS_NAMED_CACHES_DIR": str(self.named_caches),
            "PANTS_WORKDIR": str(self.pants_workdir),
            "PANTS_SUBPROCESSDIR": str(self.pants_subprocessdir),
            "UV_CACHE_DIR": str(self.uv_cache),
            "PEX_ROOT": str(self.pex_root),
            "PANTS_LOCAL_EXECUTION_ROOT_DIR": str(self.sandbox_root),
        }


def cache_layout(repository: Path, environ: Mapping[str, str] | None = None) -> CacheLayout:
    env = os.environ if environ is None else environ
    repository = repository.resolve()
    configured = env.get("BH_PANTS_CACHE_ROOT")
    if configured:
        cache_root = Path(configured).expanduser().resolve()
    else:
        xdg = Path(env.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser()
        cache_root = (xdg / "beadhive" / "pants").resolve()
    key = hashlib.sha256(os.fsencode(repository)).hexdigest()[:20]
    worktree_root = cache_root / "worktrees" / key
    configured_sandbox = env.get("BH_PANTS_SANDBOX_ROOT")
    sandbox_root = (
        Path(configured_sandbox).expanduser().resolve()
        if configured_sandbox
        else worktree_root / "sandboxes"
    )
    return CacheLayout(
        repository=repository,
        cache_root=cache_root,
        local_store=cache_root / "local-store",
        worktree_root=worktree_root,
        named_caches=worktree_root / "named-caches",
        pants_workdir=worktree_root / "workdir",
        pants_subprocessdir=worktree_root / "pantsd",
        uv_cache=worktree_root / "uv",
        pex_root=worktree_root / "pex",
        sandbox_root=sandbox_root,
        maintenance_lock=cache_root / ".maintenance.lock",
    )


def _ensure_directory(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CacheSafetyError(f"managed cache path is not a real directory: {path}")
    if info.st_uid != os.getuid():
        raise CacheSafetyError(
            f"managed cache path is owned by uid {info.st_uid}, expected {os.getuid()}: {path}"
        )
    actual = stat.S_IMODE(info.st_mode)
    if actual != mode:
        path.chmod(mode)
        actual = stat.S_IMODE(path.stat().st_mode)
    if actual != mode:
        raise CacheSafetyError(f"managed cache mode is {actual:o}, expected {mode:o}: {path}")


def capacity(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path)
    filesystem = os.statvfs(path)
    return {
        "free_bytes": usage.free,
        "free_inodes": filesystem.f_favail,
        "total_bytes": usage.total,
        "total_inodes": filesystem.f_files,
    }


def _device_id(path: Path) -> int:
    return path.stat().st_dev


def capacities(layout: CacheLayout) -> tuple[dict[str, object], ...]:
    """Report capacity once per device used by Pants' high-volume write roots."""
    devices: dict[int, dict[str, object]] = {}
    for role, path in (
        ("local_store", layout.local_store),
        ("worktree_root", layout.worktree_root),
        ("sandbox_root", layout.sandbox_root),
    ):
        device = _device_id(path)
        if device not in devices:
            devices[device] = {
                "device": device,
                "roots": {},
                **capacity(path),
            }
        roots = devices[device]["roots"]
        assert isinstance(roots, dict)
        roots[role] = str(path)
    return tuple(devices[device] for device in sorted(devices))


def prepare(
    layout: CacheLayout,
    *,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    min_free_inodes: int = DEFAULT_MIN_FREE_INODES,
) -> tuple[dict[str, object], ...]:
    _ensure_directory(layout.cache_root, SHARED_MODE)
    _ensure_directory(layout.local_store, SHARED_MODE)
    _ensure_directory(layout.worktree_root, PRIVATE_MODE)
    for path in (
        layout.named_caches,
        layout.pants_workdir,
        layout.pants_subprocessdir,
        layout.uv_cache,
        layout.pex_root,
        layout.sandbox_root,
    ):
        _ensure_directory(path, PRIVATE_MODE)
    observed = capacities(layout)
    for device in observed:
        roots = device["roots"]
        free_bytes = device["free_bytes"]
        free_inodes = device["free_inodes"]
        if not isinstance(roots, dict) or not isinstance(free_bytes, int):
            raise CacheSafetyError("invalid filesystem capacity observation")
        if not isinstance(free_inodes, int):
            raise CacheSafetyError("invalid filesystem inode observation")
        description = f"device {device['device']} ({', '.join(sorted(roots))})"
        if free_bytes < min_free_bytes:
            raise CacheSafetyError(
                f"{description} has {free_bytes} free bytes; reserve is {min_free_bytes}"
            )
        if free_inodes < min_free_inodes:
            raise CacheSafetyError(
                f"{description} has {free_inodes} free inodes; reserve is {min_free_inodes}"
            )
    return observed


@contextlib.contextmanager
def cache_lease(layout: CacheLayout, *, exclusive: bool, blocking: bool = True) -> Iterator[None]:
    _ensure_directory(layout.cache_root, SHARED_MODE)
    descriptor = os.open(layout.maintenance_lock, os.O_CREAT | os.O_RDWR, SHARED_MODE)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise CacheActiveError("Pants cache is active; maintenance refused") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def status(layout: CacheLayout) -> dict[str, object]:
    observed = prepare(layout, min_free_bytes=0, min_free_inodes=0)
    paths = asdict(layout)
    return {
        "paths": {key: str(value) for key, value in paths.items()},
        "environment": layout.environment(),
        "capacity_by_device": observed,
        "modes": {
            "local_store": f"{stat.S_IMODE(layout.local_store.stat().st_mode):04o}",
            "worktree_root": f"{stat.S_IMODE(layout.worktree_root.stat().st_mode):04o}",
        },
        "owner_uid": layout.local_store.stat().st_uid,
        "fallback": FALLBACK,
    }


def recover_local_entry(layout: CacheLayout, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or len(candidate.parts) < 2:
        raise CacheSafetyError("recovery requires one exact nested local-store entry")
    target = (layout.local_store / candidate).resolve()
    try:
        target.relative_to(layout.local_store.resolve())
    except ValueError as exc:
        raise CacheSafetyError("recovery entry escapes the shared local store") from exc
    if target == layout.local_store or not target.exists():
        raise CacheSafetyError(f"exact recovery entry does not exist: {target}")
    with cache_lease(layout, exclusive=True, blocking=False):
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    return target


def reset_worktree_cache(layout: CacheLayout, name: str) -> Path:
    allowed = {
        "named-caches": layout.named_caches,
        "workdir": layout.pants_workdir,
        "pantsd": layout.pants_subprocessdir,
        "uv": layout.uv_cache,
        "pex": layout.pex_root,
        "sandboxes": layout.sandbox_root,
    }
    if name not in allowed:
        raise CacheSafetyError(f"unknown mutable cache {name!r}; choose one of {sorted(allowed)}")
    target = allowed[name]
    with cache_lease(layout, exclusive=True, blocking=False):
        if target.exists():
            shutil.rmtree(target)
        _ensure_directory(target, PRIVATE_MODE)
    return target


def cleanup_stale_worktrees(layout: CacheLayout, max_age_days: float) -> list[Path]:
    if max_age_days < 0:
        raise CacheSafetyError("cleanup age must be non-negative")
    removed: list[Path] = []
    cutoff = time.time() - max_age_days * 86400
    root = layout.cache_root / "worktrees"
    with cache_lease(layout, exclusive=True, blocking=False):
        if not root.exists():
            return removed
        for candidate in sorted(root.iterdir()):
            if (
                candidate == layout.worktree_root
                or candidate.is_symlink()
                or not candidate.is_dir()
            ):
                continue
            if candidate.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(candidate)
            removed.append(candidate)
    return removed


def run_pants(layout: CacheLayout, command: Sequence[str]) -> int:
    if not command:
        raise CacheSafetyError("no Pants launcher command was supplied")
    minimum_bytes = int(os.environ.get("BH_PANTS_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES))
    minimum_inodes = int(os.environ.get("BH_PANTS_MIN_FREE_INODES", DEFAULT_MIN_FREE_INODES))
    observed = prepare(
        layout,
        min_free_bytes=minimum_bytes,
        min_free_inodes=minimum_inodes,
    )
    env = os.environ.copy()
    env.update(layout.environment())
    print(
        json.dumps(
            {
                "event": "pants-cache-preflight",
                "devices": observed,
                "local_store": str(layout.local_store),
                "worktree_root": str(layout.worktree_root),
                "sandbox_root": str(layout.sandbox_root),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    with cache_lease(layout, exclusive=False):
        result = subprocess.run(command, cwd=layout.repository, env=env, check=False)
    if result.returncode:
        print(
            f"pants-cache: command failed closed with exit {result.returncode}; reset only the "
            f"named corrupt entry, retry, or run authoritative fallback `{FALLBACK}`",
            file=sys.stderr,
        )
    return result.returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("check")
    run = subparsers.add_parser("run")
    run.add_argument("command", nargs=argparse.REMAINDER)
    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--max-age-days", type=float, default=30)
    recover = subparsers.add_parser("recover-local")
    recover.add_argument("entry")
    reset = subparsers.add_parser("reset-worktree")
    reset.add_argument("cache")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    layout = cache_layout(options.repository)
    try:
        if options.action == "status":
            print(json.dumps(status(layout), indent=2, sort_keys=True))
        elif options.action == "check":
            observed = prepare(layout)
            print(json.dumps({"status": "ok", "devices": observed}, sort_keys=True))
        elif options.action == "run":
            command = options.command[1:] if options.command[:1] == ["--"] else options.command
            return run_pants(layout, command)
        elif options.action == "cleanup":
            removed = cleanup_stale_worktrees(layout, options.max_age_days)
            print(json.dumps({"removed": [str(path) for path in removed]}, sort_keys=True))
        elif options.action == "recover-local":
            print(recover_local_entry(layout, options.entry))
        elif options.action == "reset-worktree":
            print(reset_worktree_cache(layout, options.cache))
    except (CacheSafetyError, OSError, ValueError) as exc:
        print(
            f"pants-cache: {exc}; refusing cache use; authoritative fallback: `{FALLBACK}`",
            file=sys.stderr,
        )
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
