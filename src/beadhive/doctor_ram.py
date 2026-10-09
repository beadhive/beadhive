"""``bh doctor`` RAM-backed storage and swap check (bh-01asp).

Worktrees, validation checkouts, ``TMPDIR`` and bh's own state dirs on a tmpfs/ramfs mount turn
every per-tree ``.venv`` and build cache into unreclaimable RAM, which is fatal on a host with no
swap. This module reports where each of those roots lives, how much of its RAM-backed
filesystem is in use, and whether the host has swap. Detection reuses the ``config.statfs_type``
/ ``config.is_memory_backed`` seam (bh-xzsdf) and ``validation_memory.read_meminfo`` (bh-jg7fy);
tests fake those instead of creating real memory pressure. Findings are warnings only, and
``worktrees.ram_warn_gib: 0`` turns them off.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

import typer

from . import validation_memory
from .config_paths import MEMORY_BACKED_MAGICS, RAMFS_MAGIC, TMPFS_MAGIC

#: Roles whose loss on reboot (or RAM cost) is worth flagging when they sit on RAM.
STATE_ROLES = ("bh_home", "bh_cache", "bh_hub", "bh_hq")


def _fmt(n: int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"  # pragma: no cover


def _nearest_existing(path: Path) -> Path:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _device_id(path: Path) -> str | None:
    try:
        return str(os.stat(_nearest_existing(path)).st_dev)
    except OSError:
        return None


def _fs_usage(path: Path) -> tuple[int | None, int | None]:
    """``(total, used)`` bytes of the filesystem holding ``path``; ramfs reports no size."""
    try:
        usage = shutil.disk_usage(_nearest_existing(path))
    except OSError:
        return None, None
    if not usage.total:
        return None, None
    return usage.total, usage.total - usage.free


def _fs_name(magic: int | None) -> str | None:
    return {TMPFS_MAGIC: "tmpfs", RAMFS_MAGIC: "ramfs"}.get(magic)


def swap_status(meminfo_path: Path | None = None) -> dict:
    """``SwapTotal`` / ``SwapFree`` from ``/proc/meminfo``; ``swap_total_bytes`` is ``None``
    when the host has no meminfo (not Linux), which is unknown rather than absent."""
    info = validation_memory.read_meminfo(meminfo_path)
    total = info.get("SwapTotal")
    return {
        "swap_total_bytes": total,
        "swap_free_bytes": info.get("SwapFree"),
        "swap_absent": total == 0,
        "mem_total_bytes": info.get("MemTotal"),
    }


def collect(
    roots: Sequence[tuple[str, Path]],
    *,
    statfs_type: Callable[[Path], int | None],
    threshold_bytes: int,
    allow_tmpfs: bool,
    meminfo_path: Path | None = None,
) -> dict:
    """Structured RAM-backed storage report: one row per ``(role, path)`` root plus swap and the
    aggregate RAM-backed usage (each distinct filesystem counted once). The caller resolves the
    roots, the statfs seam (``config.statfs_type``) and the config-derived knobs."""
    rows = []
    seen: dict[str, int] = {}
    for role, path in roots:
        magic = statfs_type(path)
        backed = magic in MEMORY_BACKED_MAGICS
        total, used = _fs_usage(path) if backed else (None, None)
        rows.append(
            {
                "role": role,
                "path": str(path),
                "filesystem_type": _fs_name(magic),
                "memory_backed": backed,
                "total_bytes": total,
                "used_bytes": used,
            }
        )
        if backed and used is not None:
            seen[_device_id(path) or str(path)] = used
    return {
        "roots": rows,
        "ram_backed_used_bytes": sum(seen.values()),
        "ram_backed_filesystems": len(seen),
        "threshold_bytes": threshold_bytes,
        "allow_tmpfs": allow_tmpfs,
        **swap_status(meminfo_path),
    }


def warnings(data: dict) -> list[str]:
    """Warn-severity findings for a :func:`collect` result; empty when the check is switched
    off (``worktrees.ram_warn_gib: 0``)."""
    threshold = data["threshold_bytes"]
    if not threshold:
        return []
    out: list[str] = []
    for row in data["roots"]:
        if not row["memory_backed"]:
            continue
        used = (
            f", {_fmt(row['used_bytes'])} of {_fmt(row['total_bytes'])} in use"
            if row["used_bytes"] is not None
            else ""
        )
        where = f"{row['path']} is on {row['filesystem_type']}{used}"
        if row["role"] == "worktrees" and not data["allow_tmpfs"]:
            out.append(
                f"worktree root (also validation checkouts) {where}: per-tree venvs and caches "
                "consume unreclaimable RAM — point worktrees.path at disk, or opt in with "
                "worktrees.allow_tmpfs"
            )
        elif row["role"] in STATE_ROLES:
            out.append(
                f"bh state dir {row['role']} {where}: state is lost on reboot and held in RAM — "
                "move it to a disk-backed path"
            )
    if data["swap_absent"] and data["ram_backed_used_bytes"] > threshold:
        out.append(
            f"no swap configured (SwapTotal=0) and {_fmt(data['ram_backed_used_bytes'])} is held "
            f"in RAM-backed storage (> {_fmt(threshold)}): memory pressure has no escape valve and "
            "can livelock the host — move worktrees/TMPDIR to disk, prune worktrees, or add swap "
            "(raise or zero worktrees.ram_warn_gib to tune)"
        )
    return out


def render(data: dict) -> None:
    typer.echo("\n# RAM-backed storage")
    for row in data["roots"]:
        if row["memory_backed"]:
            detail = (
                f"{row['filesystem_type']}  {_fmt(row['used_bytes'])} / {_fmt(row['total_bytes'])}"
                if row["used_bytes"] is not None
                else f"{row['filesystem_type']}  usage unknown"
            )
        else:
            detail = "disk"
        typer.echo(f"  {row['role']:<10}  {detail:<34}  {row['path']}")
    total = data["swap_total_bytes"]
    swap = "unknown" if total is None else ("none" if total == 0 else _fmt(total))
    typer.echo(
        f"  swap: {swap}; RAM-backed in use: {_fmt(data['ram_backed_used_bytes'])} across "
        f"{data['ram_backed_filesystems']} filesystem(s)"
    )
