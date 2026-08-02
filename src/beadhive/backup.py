"""backup.py — boundary + retention for bh's three backup roots (bh-cmqp.2).

See ``docs/design/backup-retention-boundary-adr.md`` for the design; this module implements
it, one function set per root:

- **HQ pre-push backup** (``hq._backup_root()``) — automatic keep-N prune, called right after
  ``hq._take_backup`` verifies a new one (``hq._wire_remote``). ``bh`` owns this write path
  end to end, so pruning can run synchronously with zero operator involvement.
- **bd's own per-hive Dolt-native backup** (``<hive>/.beads/backup/``) — operator-invoked
  rotate + keep-N generations (``bh backup reclaim --root hive``), using only bd's own
  sanctioned lifecycle verbs (``bd backup remove``/``init``/``sync``) plus a plain filesystem
  rename. ``bh`` doesn't own bd's sync timer, so this never runs automatically.
- **The JSONL mirror** (``bh backup export``) — a fixed per-hive default path, cwd-
  INDEPENDENT (the bh-mw97 failure class), overwritten each run. No pruning code: keep-1 is
  the policy, enforced simply by never accumulating more than one file at the default path.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config
from .bd import err_line
from .run import run

BD_TIMEOUT = 120.0  # seconds — matches hq.py's own bd-call budget

_MIRROR_FILENAME = "issues.jsonl"
_HIVE_BACKUP_DIRNAME = "backup"  # <hive>/.beads/backup — bd's own destination


# ---- shared helpers -----------------------------------------------------------


def _dir_size(path: Path) -> int:
    """Recursively sum file sizes under ``path``; 0 for a missing/unreadable path. The same
    small walk every disk-usage report in this codebase does locally (``archive._dir_size``,
    ``safety._measure_disk_usage``) — kept as its own copy here rather than a cross-module
    import of another module's private helper."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except (OSError, PermissionError):
        pass
    return total


@dataclass
class PruneResult:
    """Outcome of a keep-N prune over dated/generation directories — the shared shape for the
    HQ prune and the hive-rotated-generations prune. ``reclaimed_bytes`` is populated whether
    or not this was a dry run — the size is measured before any deletion either way, so the
    number means "reclaimed" on a real run and "would reclaim" on a preview; callers render
    the right tense off ``dry_run``."""

    removed: list[str] = field(default_factory=list)
    reclaimed_bytes: int = 0
    dry_run: bool = False


def _prune_keep_n(
    candidates: list[Path], keep: int, *, dry_run: bool, min_keep: int = 1
) -> PruneResult:
    """Keep the newest ``keep`` of ``candidates`` (already sorted newest-first); rmtree the
    rest. ``keep`` is clamped to ``min_keep`` — the HQ prune needs at least 1 (a fresh backup
    must always be left restorable); the hive-rotate generations have no such floor (the LIVE
    backup lives back at the canonical un-rotated path, so keeping zero old generations is a
    legitimate operator choice)."""
    result = PruneResult(dry_run=dry_run)
    if not candidates:
        return result
    keep = max(keep, min_keep)
    for path in candidates[keep:]:
        result.removed.append(path.name)
        result.reclaimed_bytes += _dir_size(path)
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
    return result


def _bd(args: list[str], cwd: Path):
    return run(["bd", "-C", str(cwd), *args], check=False, capture=True, timeout=BD_TIMEOUT)


# ---- root 1: HQ pre-push backup ------------------------------------------------


def _hq_backup_dirs(cfg) -> list[Path]:
    """Every dated directory under ``hq._backup_root()``, NEWEST FIRST (ISO date names sort
    chronologically with a plain reverse sort — same trick ``hq_restore.list_backups`` uses).
    Unlike that function, this counts every directory that exists on disk, restorable or not —
    an empty/partial one still occupies a slot worth pruning."""
    from . import hq  # lazy: hq imports this module to call prune_hq_backups (see hq._wire_remote)

    root = hq._backup_root(cfg)
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)


def hq_backup_usage(cfg=None) -> tuple[Path, int, int]:
    """``(root, total size bytes, dated-directory count)`` for the HQ pre-push backup root."""
    cfg = cfg if cfg is not None else config.load()
    from . import hq

    root = hq._backup_root(cfg)
    dirs = _hq_backup_dirs(cfg)
    return root, sum(_dir_size(d) for d in dirs), len(dirs)


def prune_hq_backups(cfg=None, *, keep: int | None = None, dry_run: bool = False) -> PruneResult:
    """Keep the newest ``keep`` (default ``config.backup_hq_keep``) dated directories under
    HQ's pre-push backup root; rmtree the rest. Called automatically right after
    ``hq._take_backup`` verifies a new one (``hq._wire_remote``); ``bh backup reclaim --root
    hq`` re-runs it on demand (e.g. after lowering ``backup.hq_keep``)."""
    cfg = cfg if cfg is not None else config.load()
    resolved_keep = config.backup_hq_keep(cfg) if keep is None else keep
    return _prune_keep_n(_hq_backup_dirs(cfg), resolved_keep, dry_run=dry_run)


# ---- root 3: JSONL mirror -------------------------------------------------------


def _hive_slug(cfg=None, cwd=None) -> str:
    """``provider/org/repo`` for cwd's hive — cwd-SUBDIRECTORY-independent (resolved via
    ``registry.current_hive``/``entry_for_dir``, the same identity resolution
    ``worktree.cwd_identity`` uses for telemetry, not raw ``Path.cwd()`` string matching).
    Falls back to the git top-level directory's own name when cwd is a repo bh doesn't manage
    at all (no git-workspace/worktree identity resolvable) — still independent of which
    subdirectory the operator is standing in, just not identity-resolved (same failure class
    bh-mw97 fixed for ``hq.remote``)."""
    from . import registry

    cfg = cfg if cfg is not None else config.load()
    entry = registry.entry_for_dir(cfg, cwd) if cwd else registry.current_hive(cfg)
    if entry is not None:
        return f"{entry['provider']}/{entry['org']}/{entry['repo']}"
    res = run(["git", "rev-parse", "--show-toplevel"], check=False, capture=True, cwd=cwd)
    top = (res.stdout or "").strip() if res.returncode == 0 else ""
    return f"_unmanaged/{Path(top).name}" if top else "_unmanaged/unknown"


def mirror_root(cfg=None, cwd=None) -> Path:
    """Default destination for ``bh backup export``'s JSONL mirror — a fixed per-hive path
    under ``~/.beadhive/backups/``, independent of which subdirectory of the hive the operator
    was standing in (bh-cmqp.2). A single fixed file is overwritten each run; see the ADR for
    why this mechanism intentionally retains no history."""
    return config.home() / "backups" / _hive_slug(cfg, cwd)


def mirror_usage(cfg=None, cwd=None) -> tuple[Path, int]:
    """``(path, size bytes)`` for the current hive's JSONL mirror export file."""
    out = mirror_root(cfg, cwd) / _MIRROR_FILENAME
    return out, (out.stat().st_size if out.is_file() else 0)


# ---- root 2: bd's own per-hive Dolt-native backup --------------------------------


def hive_backup_dir(hive_dir: Path) -> Path:
    return Path(hive_dir) / ".beads" / _HIVE_BACKUP_DIRNAME


def hive_backup_usage(hive_dir: Path) -> int:
    """Current size (bytes) of a hive's ``.beads/backup/`` — bd's own live destination."""
    return _dir_size(hive_backup_dir(hive_dir))


def _hive_rotated_dirs(hive_dir: Path) -> list[Path]:
    """Every ``<hive>/.beads/backup.<timestamp>/`` generation left by a prior
    :func:`rotate_hive_backup`, NEWEST FIRST (the timestamp format sorts chronologically as a
    plain string reverse-sort)."""
    parent = Path(hive_dir) / ".beads"
    if not parent.is_dir():
        return []
    prefix = f"{_HIVE_BACKUP_DIRNAME}."
    return sorted(
        (p for p in parent.iterdir() if p.is_dir() and p.name.startswith(prefix)), reverse=True
    )


@dataclass
class RotateResult:
    dry_run: bool = False
    actions: list[str] = field(default_factory=list)
    rotated_to: Path | None = None
    ok: bool = False


def rotate_hive_backup(
    hive_dir: Path,
    cfg=None,
    *,
    dry_run: bool = True,
    confirm: bool = False,
    force: bool = False,
) -> RotateResult:
    """Rotate ``<hive>/.beads/backup/`` (bd's own Dolt-native backup) once it exceeds
    ``backup.hive_cap_mb`` (or always, with ``force=True``): unregister via ``bd backup
    remove`` (data untouched — that command's own ``--help`` documents this), rename the
    destination directory aside (never deleted outright — the same "move aside, don't delete"
    idiom ``hq_restore._apply_tar`` uses for the store it replaces), then ``bd backup
    init``/``sync`` a fresh, small destination at the SAME canonical path — the new sync
    writes only the current live state, not the accumulated backup history, so the
    destination shrinks back down. Never raises on a normal failure path; the caller renders
    ``actions`` and picks the exit code. ``dry_run`` (default) previews with zero mutation;
    a real rotate additionally needs ``confirm=True``."""
    cfg = cfg if cfg is not None else config.load()
    hive_dir = Path(hive_dir)
    src = hive_backup_dir(hive_dir)
    out = RotateResult(dry_run=dry_run)
    if not src.is_dir():
        out.actions.append(f"{src} does not exist — nothing to rotate")
        out.ok = True
        return out
    size_mb = _dir_size(src) / (1024 * 1024)
    cap_mb = config.backup_hive_cap_mb(cfg)
    if not force and size_mb < cap_mb:
        out.actions.append(
            f"{src} is {size_mb:.0f} MB, under the {cap_mb} MB cap (backup.hive_cap_mb) — "
            "nothing to do (pass --force to rotate anyway)"
        )
        out.ok = True
        return out
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = src.with_name(f"{src.name}.{stamp}")
    if dry_run:
        out.actions.append(
            f"would `bd backup remove`, move {src} -> {dest.name}, then `bd backup init "
            f"{src}` + `bd backup sync`"
        )
        return out
    if not confirm:
        out.actions.append("✗ refusing to rotate the live backup destination without --confirm")
        return out
    removed = _bd(["backup", "remove"], hive_dir)
    if removed.returncode:
        out.actions.append(f"✗ bd backup remove failed: {err_line(removed)}")
        return out
    src.rename(dest)
    out.actions.append(f"moved {src.name} -> {dest.name} (kept, not deleted)")

    def _roll_back(action_error: str) -> RotateResult:
        # init/sync failing after the rename would otherwise leave NO destination at the
        # canonical path at all — roll the rename back (same "restore what we moved aside on
        # failure" idiom hq_restore._apply_tar uses) so a failed rotate is a no-op, not a
        # silent loss of the live backup destination.
        dest.rename(src)
        out.actions.append(f"{action_error} — rolled the rename back, {src} unchanged")
        return out

    inited = _bd(["backup", "init", str(src)], hive_dir)
    if inited.returncode:
        return _roll_back(f"✗ bd backup init failed: {err_line(inited)}")
    synced = _bd(["backup", "sync"], hive_dir)
    if synced.returncode:
        return _roll_back(f"✗ bd backup sync failed: {err_line(synced)}")
    out.rotated_to = dest
    out.ok = True
    out.actions.append(f"✓ rotated — fresh backup destination at {src}")
    return out


def prune_hive_rotated(
    hive_dir: Path, cfg=None, *, keep: int | None = None, dry_run: bool = False
) -> PruneResult:
    """Keep the newest ``keep`` (default ``config.backup_hive_rotate_keep``) rotated
    generations left by :func:`rotate_hive_backup`; rmtree the rest. These are already
    detached from bd (rotation unregisters + re-points the live destination elsewhere before
    ever renaming), so deleting an old one is a plain filesystem removal, not chunk-store
    surgery."""
    cfg = cfg if cfg is not None else config.load()
    resolved_keep = config.backup_hive_rotate_keep(cfg) if keep is None else keep
    return _prune_keep_n(_hive_rotated_dirs(hive_dir), resolved_keep, dry_run=dry_run, min_keep=0)


# ---- usage report (all three roots) ----------------------------------------------


@dataclass
class RootUsage:
    """One line of ``bh backup usage`` — a root's current size + its retention policy."""

    root: str  # "hq" | "hive" | "mirror"
    label: str
    path: Path
    size_bytes: int
    detail: str = ""


def usage_report(cfg=None) -> list[RootUsage]:
    """Every backup root's current size: HQ's dated directories (one summed entry), every
    registered + locally-checked-out hive's ``.beads/backup/`` (bd's own), and the CURRENT
    hive's mirror export slot. Read-only — never mutates anything on disk."""
    from . import registry

    cfg = cfg if cfg is not None else config.load()
    out: list[RootUsage] = []

    hq_root, hq_bytes, hq_count = hq_backup_usage(cfg)
    out.append(
        RootUsage(
            root="hq",
            label=f"HQ pre-push backup ({hq_count} dated set{'' if hq_count == 1 else 's'})",
            path=hq_root,
            size_bytes=hq_bytes,
            detail=f"keep newest {config.backup_hq_keep(cfg)} (backup.hq_keep)",
        )
    )

    for entry in registry.hives(cfg):
        hive_dir = registry.hive_dir(entry)
        if not hive_dir.is_dir():
            continue
        size = hive_backup_usage(hive_dir)
        if size == 0 and not hive_backup_dir(hive_dir).is_dir():
            continue  # bd's backup: never configured for this hive — nothing to report
        prefix = entry.get("prefix") or entry.get("repo") or str(hive_dir)
        out.append(
            RootUsage(
                root="hive",
                label=f"{prefix}  .beads/backup (bd's own)",
                path=hive_backup_dir(hive_dir),
                size_bytes=size,
                detail=f"cap {config.backup_hive_cap_mb(cfg)} MB (backup.hive_cap_mb)",
            )
        )

    mirror_path, mirror_bytes = mirror_usage(cfg)
    out.append(
        RootUsage(
            root="mirror",
            label="JSONL mirror (bh backup export, current hive)",
            path=mirror_path,
            size_bytes=mirror_bytes,
            detail="overwritten each run — no history retained by design",
        )
    )
    return out
