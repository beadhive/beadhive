"""``bh hq restore`` — the other half of ``hq._take_backup`` (bh-cmqp.1).

``_take_backup`` writes and VERIFIES three levels before HQ's first push, at the most
dangerous moment in HQ's lifecycle (giving a database a remote is what makes a schema
migration a one-way fleet decision). Nothing consumed any of them: three green checkmarks and
a verified multi-hundred-MB artifact, with recovery left as an exercise for a future operator
under duress, on a store that by hypothesis is already broken.

This module consumes what that writes:

``tar``    — full-fidelity: replace ``.beads/embeddeddolt`` from ``hq-embeddeddolt.tar.gz``.
             Preserves branches, history, working set. Needs the tarball to be intact.
``jsonl``  — format-independent floor: ``bd import`` (UPSERT semantics, so it is idempotent)
             of ``hq-issues.jsonl`` into a store, creating one via ``hub.ensure_store`` when
             none exists. Deliberately survives the case the tarball does not — a Dolt store
             too broken to read — because that is the scenario the JSONL level exists for.

Safety follows the ``bh hive retire`` convention already in the codebase: ``--dry-run``
previews with zero mutation, a real run needs ``--confirm``, and the tar level moves the
existing store aside rather than deleting it, so a failed extract is recoverable.
"""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import config, engine, hub, registry, safety

_JSONL_NAME = "hq-issues.jsonl"
_TAR_NAME = "hq-embeddeddolt.tar.gz"
_STORE_REL = Path(".beads") / "embeddeddolt"


@dataclass
class BackupSet:
    """One dated backup directory and which levels it actually holds."""

    directory: Path
    jsonl: Path | None = None
    tar: Path | None = None

    @property
    def label(self) -> str:
        return self.directory.name

    def levels(self) -> list[str]:
        return [n for n, p in (("tar", self.tar), ("jsonl", self.jsonl)) if p is not None]


@dataclass
class RestoreResult:
    level: str = ""
    dry_run: bool = False
    actions: list[str] = field(default_factory=list)
    moved_aside: str | None = None
    issue_count: int = -1
    ok: bool = False


def _backup_root(cfg: dict) -> Path:
    # Same root hq._take_backup writes to — imported lazily to avoid a circular import
    # (hq imports nothing from here, but keep the dependency one-directional).
    from . import hq

    return hq._backup_root(cfg)


def list_backups(cfg: dict) -> list[BackupSet]:
    """Every dated backup directory, NEWEST FIRST. Directory names are ISO dates, so a plain
    reverse sort is chronological — no stat() and no clock read needed."""
    root = _backup_root(cfg)
    if not root.is_dir():
        return []
    sets: list[BackupSet] = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        jsonl = d / _JSONL_NAME
        tar = d / _TAR_NAME
        found = BackupSet(d, jsonl if jsonl.is_file() else None, tar if tar.is_file() else None)
        if found.levels():
            sets.append(found)
    return sets


def resolve_level(backup: BackupSet, requested: str, *, tar_usable: bool = True) -> str:
    """`auto` prefers the full-fidelity tar and falls back to the JSONL floor.

    *tar_usable* is False when HQ's dolt engine is not embedded: the tar level replaces
    ``.beads/embeddeddolt``, which such an engine never reads, so `auto` must fall through to
    the JSONL floor rather than "succeed" into a directory nothing consumes (bh-kobw)."""
    if requested != "auto":
        return requested
    if backup.tar and tar_usable:
        return "tar"
    return "jsonl" if backup.jsonl else ""


def restore(
    cfg: dict,
    backup: BackupSet,
    *,
    level: str = "auto",
    dry_run: bool = True,
    confirm: bool = False,
) -> RestoreResult:
    """Restore HQ from *backup*. Returns a :class:`RestoreResult`; never raises on a normal
    failure path (the caller renders and picks the exit code)."""
    hq_dir = config.hq_dir()
    # Probed only when a tar is actually on the table, so the JSONL-only path pays nothing.
    mode = safety._bd_dolt_mode(str(hq_dir)) if backup.tar else None
    tar_usable = mode is None or mode == "embedded"
    chosen = resolve_level(backup, level, tar_usable=tar_usable)
    out = RestoreResult(level=chosen, dry_run=dry_run)
    if not chosen:
        out.actions.append(f"✗ {backup.label} holds no restorable level")
        return out
    if chosen == "tar" and backup.tar is None:
        out.actions.append(f"✗ {backup.label} has no {_TAR_NAME}")
        return out
    if chosen == "tar" and not tar_usable:
        # Extracting into `.beads/embeddeddolt` under a server-mode engine writes a directory
        # nothing reads, and would report success doing it — the restore-side twin of the
        # backup-side hole this bead fixes (bh-kobw).
        out.actions.append(
            f"✗ HQ's dolt engine is in {mode!r} mode — its store is not at {_STORE_REL}, so "
            f"extracting {_TAR_NAME} there would restore nothing the engine reads"
        )
        return out
    if chosen == "jsonl" and backup.jsonl is None:
        out.actions.append(f"✗ {backup.label} has no {_JSONL_NAME}")
        return out

    if chosen == "tar":
        _plan_tar(hq_dir, backup, out)
    else:
        _plan_jsonl(hq_dir, backup, out)

    if dry_run:
        out.ok = True
        return out
    if not confirm:
        out.actions.append("✗ refusing to overwrite live HQ data without --confirm")
        return out
    if chosen == "tar":
        return _apply_tar(hq_dir, backup, out)
    return _apply_jsonl(cfg, hq_dir, backup, out)


# ---- tar level: full-fidelity store replacement -------------------------------


def _plan_tar(hq_dir: Path, backup: BackupSet, out: RestoreResult) -> None:
    store = hq_dir / _STORE_REL
    if store.is_dir():
        out.actions.append(f"move aside existing store {store} (kept, not deleted)")
    out.actions.append(f"extract {backup.tar} -> {store.parent}")


def _apply_tar(hq_dir: Path, backup: BackupSet, out: RestoreResult) -> RestoreResult:
    store = hq_dir / _STORE_REL
    aside: Path | None = None
    if store.is_dir():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        aside = store.with_name(f"{store.name}.pre-restore-{stamp}")
        store.rename(aside)
        out.moved_aside = str(aside)
    try:
        with tarfile.open(backup.tar, "r:gz") as tf:
            # arcname is "embeddeddolt" (see hq._backup_tar), so extracting into .beads/
            # recreates the store at its own path.
            tf.extractall(store.parent, filter="data")
    except Exception as exc:  # noqa: BLE001 — any extract failure must roll back
        if aside is not None:
            if store.exists():
                shutil.rmtree(store, ignore_errors=True)
            aside.rename(store)
            out.moved_aside = None
            out.actions.append(f"✗ extract failed ({exc}) — rolled the previous store back")
        else:
            out.actions.append(f"✗ extract failed: {exc}")
        return out
    out.ok = store.is_dir()
    out.actions.append(f"✓ restored store from {backup.tar}")
    return out


# ---- jsonl level: the format-independent floor --------------------------------


def _plan_jsonl(hq_dir: Path, backup: BackupSet, out: RestoreResult) -> None:
    # Mirror `hub.ensure_store`'s own guard (`.beads/`), not the embedded store dir it happens
    # to contain. A server-mode HQ has `.beads/` with its data in the server, so testing
    # `_STORE_REL` here would promise "create an HQ store" for a call that then no-ops — and
    # this is the level a non-embedded HQ now falls back to, so it is the plan operators read
    # (bh-kobw).
    if not (hq_dir / ".beads").is_dir():
        out.actions.append(f"create an HQ store at {hq_dir} (prefix '{registry.HQ_PREFIX}')")
    out.actions.append(f"bd import {backup.jsonl} (upsert)")


def _apply_jsonl(cfg: dict, hq_dir: Path, backup: BackupSet, out: RestoreResult) -> RestoreResult:
    # The whole point of this level: it must work when there is no readable Dolt store, so
    # stand one up first rather than assuming the store survived.
    hub.ensure_store(hq_dir, registry.HQ_PREFIX)
    res = engine.get_engine(cfg).import_jsonl(hq_dir, [str(backup.jsonl)])
    if res.returncode:
        from .bd import err_line

        out.actions.append(f"✗ bd import failed: {err_line(res)}")
        return out
    out.ok = True
    out.actions.append(f"✓ imported {backup.jsonl}")
    return out


# ---- rendering ----------------------------------------------------------------


def echo_backups(sets: list[BackupSet]) -> None:
    if not sets:
        typer.echo("no HQ backups found — `bh hq init` writes one before its first push")
        return
    typer.echo("HQ backups (newest first):")
    for s in sets:
        sizes = ", ".join(
            f"{n} {p.stat().st_size:,}B" for n, p in (("tar", s.tar), ("jsonl", s.jsonl)) if p
        )
        typer.echo(f"  {s.label}  [{'|'.join(s.levels())}]  {sizes}")


def echo_result(out: RestoreResult) -> None:
    tag = "DRY-RUN " if out.dry_run else ""
    typer.echo(f"  {tag}restore level: {out.level or '(none)'}")
    for line in out.actions:
        typer.echo(f"    {line}")
    if out.moved_aside:
        typer.echo(f"    previous store kept at {out.moved_aside}")
