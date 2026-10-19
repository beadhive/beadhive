"""``bh backup snapshot|list|verify|restore`` (bh-ksmt0.4): the operator face of
:mod:`beadhive.native_backup`. Thin on purpose -- every decision lives in that module; this one
resolves the hive, holds the lease and migration lock, renders, and picks the exit code.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import backup as backup_mod
from . import native_backup, registry


def _target(cfg: dict, hive: str, hq: bool) -> tuple[Path, str, str]:
    """``(hive_dir, slug, prefix)`` for ``--hive`` / ``--hq`` / the cwd's hive."""
    if hq:
        entry = registry.hive_of_kind(cfg, registry.HQ_KIND)
        if entry is None:
            typer.echo("✗ no Factory HQ is registered on this host", err=True)
            raise typer.Exit(1)
    elif hive:
        entry = registry.resolve_hive(cfg, hive)
    else:
        entry = registry.current_hive(cfg)
    if entry is None:
        typer.echo("✗ not inside a managed hive; pass --hive (or --hq)", err=True)
        raise typer.Exit(1)
    prefix = str(entry.get("prefix") or entry.get("repo") or "")
    return registry.hive_dir(entry), backup_mod.entry_slug(entry), prefix


def _resolve_set(cfg: dict, slug: str, ref: str | None) -> Path:
    if ref:
        candidate = Path(ref).expanduser()
        if candidate.is_dir():
            return candidate
        named = backup_mod.migrate_hive_root(slug, cfg) / ref
        if named.is_dir():
            return named
        typer.echo(f"✗ no backup set {ref!r} (not a directory, not under {named.parent})", err=True)
        raise typer.Exit(1)
    for info in native_backup.list_sets(cfg, hive=slug):
        if info.kind == native_backup.KIND:
            return info.path
    typer.echo(f"✗ {slug} has no native snapshot set; take one with `bh backup snapshot`", err=True)
    raise typer.Exit(1)


def _emit(as_json: bool, payload: dict) -> None:
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _dropped_lines(dropped) -> list[str]:
    if dropped is None:
        return []
    if not dropped.left_copy:
        return [f"  scratch {dropped.database}: dropped; {dropped.note}"]
    where = dropped.location + (f" ({dropped.size_bytes:,} bytes)" if dropped.located else "")
    return [
        f"  scratch {dropped.database}: dropped, but Dolt kept a copy: {where}",
        f"    {dropped.note}",
    ]


def impl_snapshot(cfg: dict, hive: str, hq: bool, dry_run: bool, as_json: bool) -> None:
    from . import migration_lock

    hive_dir, slug, prefix = _target(cfg, hive, hq)
    try:
        with migration_lock.transaction_lock(cfg, slug, hive_dir):
            res = native_backup.take_snapshot(
                cfg, hive_dir, slug=slug, prefix=prefix, dry_run=dry_run
            )
    except (migration_lock.HiveLocked, migration_lock.NotLeaseHolder) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    if as_json:
        _emit(True, res.to_dict())
    else:
        if dry_run:
            typer.echo(
                f"○ would snapshot {slug}: JSONL + native backup + manifest, then restore-test"
            )
        elif res.ok:
            typer.echo(f"✓ {slug}: snapshot attested at {res.set_dir}")
            fp = res.manifest["fingerprint"]["digest"]
            typer.echo(f"  fingerprint (strong) {fp[:16]} == restored copy")
            typer.echo(f"  prior backup registration: {res.slot.detail}")
            for line in _dropped_lines(res.dropped):
                typer.echo(line)
        else:
            typer.echo(f"✗ {slug}: snapshot FAILED at {res.phase_reached or 'start'}", err=True)
            if res.set_dir:
                typer.echo(f"  set kept for inspection: {res.set_dir}", err=True)
            for line in res.errors:
                typer.echo(f"  {line}", err=True)
            for line in _dropped_lines(res.dropped):
                typer.echo(line, err=True)
    if not res.ok:
        raise typer.Exit(1)


def impl_list(cfg: dict, hive: str, as_json: bool) -> None:
    from . import hq_restore

    slug = ""
    if hive:
        entry = registry.resolve_hive(cfg, hive)
        slug = backup_mod.entry_slug(entry) if entry else hive
    sets = native_backup.list_sets(cfg, hive=slug)
    hq_sets = [] if slug else hq_restore.list_backups(cfg)
    if as_json:
        _emit(
            True,
            {
                "sets": [s.to_dict() for s in sets],
                "hq": [
                    {"name": s.label, "path": str(s.directory), "levels": s.levels()}
                    for s in hq_sets
                ],
            },
        )
        return
    if not sets and not hq_sets:
        typer.echo("no backup sets found")
        return
    for s in sets:
        state = {True: "verified", False: "UNVERIFIED", None: "no manifest"}[s.verified]
        extra = f"  ({'; '.join(s.problems)})" if s.problems else ""
        typer.echo(f"{s.hive}  {s.name}  [{s.kind}]  {state}  {'|'.join(s.levels)}{extra}")
    for s in hq_sets:
        typer.echo(
            f"hq  {s.label}  [hq pre-push]  {'|'.join(s.levels())}  "
            f"(restore with `bh hq restore --from {s.directory}`)"
        )


def impl_verify(
    cfg: dict, set_ref: str | None, hive: str, hq: bool, restore_test: bool, as_json: bool
) -> None:
    hive_dir, slug, _prefix = _target(cfg, hive, hq)
    set_dir = _resolve_set(cfg, slug, set_ref)
    rep = native_backup.verify_set(set_dir, cfg=cfg, hive_dir=hive_dir, restore_test=restore_test)
    if as_json:
        _emit(True, rep.to_dict())
    else:
        mark = "✓" if rep.ok else "✗"
        typer.echo(f"{mark} {set_dir}: {'verified' if rep.ok else 'NOT verified'}")
        for item in rep.checked:
            typer.echo(f"  checked {item}")
        if rep.attested is not None:
            typer.echo(
                f"  restore test: {'fingerprint matches' if rep.attested else 'FAILED'} "
                f"(scratch {rep.scratch_database})"
            )
        for line in _dropped_lines(rep.dropped):
            typer.echo(line)
        for problem in rep.problems:
            typer.echo(f"  ✗ {problem}", err=True)
    if not rep.ok:
        raise typer.Exit(1)


def impl_restore(
    cfg: dict,
    set_ref: str | None,
    hive: str,
    hq: bool,
    dry_run: bool,
    confirm: str,
    as_json: bool,
    no_pre_restore_snapshot: bool = False,
) -> None:
    from . import migration_lock

    hive_dir, slug, _prefix = _target(cfg, hive, hq)
    set_dir = _resolve_set(cfg, slug, set_ref)
    preview = dry_run
    try:
        if preview:
            out = native_backup.restore_set(
                cfg,
                hive_dir,
                set_dir,
                slug=slug,
                dry_run=True,
                skip_pre_restore_snapshot=no_pre_restore_snapshot,
            )
        else:
            with migration_lock.transaction_lock(cfg, slug, hive_dir):
                out = native_backup.restore_set(
                    cfg,
                    hive_dir,
                    set_dir,
                    slug=slug,
                    dry_run=False,
                    confirm=confirm,
                    skip_pre_restore_snapshot=no_pre_restore_snapshot,
                )
    except (migration_lock.HiveLocked, migration_lock.NotLeaseHolder) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    if as_json:
        _emit(True, out.to_dict())
    else:
        for line in out.actions:
            typer.echo(f"  {'○' if out.dry_run else '·'} {line}")
        for problem in out.problems:
            typer.echo(f"  ✗ {problem}", err=True)
        if out.ok and out.dry_run:
            typer.echo(f"○ preview only; pass --confirm {slug} to overwrite the live database")
        elif out.ok:
            typer.echo(f"✓ {slug} restored from {set_dir}")
            if out.safety_set:
                typer.echo(f"  pre-restore snapshot of the replaced store: {out.safety_set}")
    if not out.ok:
        raise typer.Exit(1)
