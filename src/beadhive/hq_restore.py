"""``bh hq restore`` — the other half of ``hq._take_backup`` (bh-cmqp.1).

``_take_backup`` writes and VERIFIES three levels before HQ's first push, at the most
dangerous moment in HQ's lifecycle (giving a database a remote is what makes a schema
migration a one-way fleet decision). Nothing consumed any of them: three green checkmarks and
a verified multi-hundred-MB artifact, with recovery left as an exercise for a future operator
under duress, on a store that by hypothesis is already broken.

This module consumes what that writes:

``tar``    — full-fidelity, EMBEDDED-mode HQ: replace ``.beads/embeddeddolt`` from
             ``embeddeddolt.tar.gz``. Preserves branches, history, working set.
``native`` — full-fidelity, every OTHER mode (owned/shared/external, bh-areg.1): ``bd backup
             restore <dir> --force`` over the CONNECTION, consuming the ``dolt-native/``
             directory ``hq._backup_dolt_native`` writes. Same guarantee as ``tar`` (branches,
             history, working set); doesn't care where the live store's bytes physically live.
             Both surface under the SAME public ``--level tar`` (the full-fidelity level,
             whichever artifact format this backup set actually holds — see
             :func:`resolve_level`).
``jsonl``  — format-independent floor: ``bd import`` (UPSERT semantics, so it is idempotent)
             of ``issues.jsonl`` into a store, creating one via ``hub.ensure_store`` when
             none exists. Deliberately survives the case NEITHER full-fidelity artifact does
             — a Dolt store too broken to read — because that is the scenario this level
             exists for.

bh-5009a moved the sets from ``~/.beadhive/hq-backups/<date>/`` to
``$BH_HOME/backups/hq/<instant>/`` and dropped the ``hq-`` prefix from all three artifact
names. Restore reads BOTH generations — location and filename — because a backup an operator
took before the relocation is still the only pre-push copy of their HQ, and a restore path
that silently stops seeing it is worse than no relocation at all.

Safety follows the ``bh hive retire`` convention already in the codebase: ``--dry-run``
previews with zero mutation, a real run needs ``--confirm``. The ``tar`` artifact moves the
existing store aside rather than deleting it, so a failed extract is recoverable; the
``native`` artifact restores over the connection (bd's own ``--force``), which has no
comparable move-aside step to offer — that trade is inherent to going over the connection
rather than manipulating files directly, and is why ``tar`` stays the level of choice whenever
it's actually usable (see ``resolve_level``'s ``tar_usable`` gate).
"""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import config, engine, hub, registry, store_locator

# Mirrors of `hq`'s own artifact-name constants (that module is heavy and this one is on the
# recovery path, so they're copied rather than imported at module level). `test_hq_restore`
# asserts the two sets are equal, which is what keeps a rename from drifting them apart.
_JSONL_NAME = "issues.jsonl"
_TAR_NAME = "embeddeddolt.tar.gz"


def _native_dirname() -> str:
    """The connection-oriented full-fidelity level's directory name — the SAME constant
    ``hq._backup_dolt_native`` writes to, imported lazily (same circular-import precedent as
    :func:`_backup_root`) so backup and restore can never drift on the artifact name."""
    from . import hq

    return hq._DOLT_NATIVE_DIRNAME


def _legacy_names() -> dict[str, str]:
    from . import hq

    return hq._LEGACY_ARTIFACT_NAMES


def _find_artifact(d: Path, name: str, *, is_dir: bool = False) -> Path | None:
    """``d/name`` if present, else the pre-bh-5009a ``hq-``-prefixed name if THAT is present
    (bh-5009a). Current-name-first matters on a set an operator has half-relocated by hand: the
    un-prefixed name is what the current writer produces, so it wins when both somehow exist."""
    for candidate in (name, _legacy_names().get(name, name)):
        path = d / candidate
        if path.is_dir() if is_dir else path.is_file():
            return path
    return None


@dataclass
class BackupSet:
    """One dated backup directory and which levels it actually holds."""

    directory: Path
    jsonl: Path | None = None
    tar: Path | None = None
    native: Path | None = None

    @property
    def label(self) -> str:
        return self.directory.name

    @property
    def full_fidelity(self) -> Path | None:
        """Whichever full-fidelity artifact this set holds — mutually exclusive with its
        sibling in practice (`hq._take_backup` writes exactly one, chosen by
        `store_locator.has_embedded_store` at backup time: `tar` for an embedded HQ, `native`
        for anything else)."""
        return self.tar or self.native

    def levels(self) -> list[str]:
        return [
            n
            for n, p in (("tar", self.tar), ("native", self.native), ("jsonl", self.jsonl))
            if p is not None
        ]


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
    """Every dated backup set, NEWEST FIRST, from the current root AND the pre-bh-5009a
    ``~/.beadhive/hq-backups/``. Set names are ISO instants (the legacy ones ISO dates, a
    strict prefix of the same format), so a plain reverse sort on the NAME — not the full path,
    which would group by root instead of by time — is chronological across both."""
    from . import backup as backup_mod

    roots = [_backup_root(cfg), backup_mod.legacy_hq_root(cfg)]
    candidates = [p for r in roots if r.is_dir() for p in r.iterdir() if p.is_dir()]
    sets: list[BackupSet] = []
    for d in sorted(candidates, key=lambda p: p.name, reverse=True):
        found = BackupSet(
            d,
            _find_artifact(d, _JSONL_NAME),
            _find_artifact(d, _TAR_NAME),
            _find_artifact(d, _native_dirname(), is_dir=True),
        )
        if found.levels():
            sets.append(found)
    return sets


def resolve_level(backup: BackupSet, requested: str, *, tar_usable: bool = True) -> str:
    """`auto` prefers the full-fidelity level — a `.tar.gz` for an embedded-mode HQ, or a
    Dolt-native connection backup for anything else (bh-areg.1) — and falls back to the
    JSONL floor. Both full-fidelity artifacts surface under the SAME `"tar"` return value
    (the public `--level tar` name); `restore()` picks which one to actually apply.

    *tar_usable* gates ONLY the `.tar.gz` artifact: it is False when HQ's dolt engine is not
    embedded, since the tar level replaces `.beads/embeddeddolt`, which such an engine never
    reads — `auto` must fall through rather than "succeed" into a directory nothing consumes
    (bh-kobw). It never gates the Dolt-native artifact, which restores over the connection and
    so works regardless of the CURRENT engine's mode."""
    if requested != "auto":
        return requested
    if (backup.tar and tar_usable) or backup.native:
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
    # A pure FILESYSTEM FACT — never a `bd dolt status` mode probe (bh-areg.1): whether the
    # CURRENT hq_dir's dolt engine is configured embedded, read straight from bd's own
    # `.beads/metadata.json` (`store_locator.is_embedded_mode`). That file is unaffected by
    # whether the store directory itself currently has any content — which is exactly the
    # case a restore recovers from, unlike `bd dolt status --json`'s live probe (whose own
    # JSON shape is ambiguous by mode anyway, bh-u562.1 finding 9). Only checked when a tar is
    # actually on the table, so the jsonl/native-only paths pay nothing extra. Unknown (no
    # readable metadata) is NOT usable — never "assume embedded".
    tar_usable = bool(backup.tar) and store_locator.is_embedded_mode(hq_dir)
    chosen = resolve_level(backup, level, tar_usable=tar_usable)
    out = RestoreResult(level=chosen, dry_run=dry_run)
    if not chosen:
        out.actions.append(f"✗ {backup.label} holds no restorable level")
        return out
    if chosen == "jsonl":
        if backup.jsonl is None:
            out.actions.append(f"✗ {backup.label} has no {_JSONL_NAME}")
            return out
        return _run_jsonl(cfg, hq_dir, backup, out, dry_run=dry_run, confirm=confirm)

    # chosen == "tar" — the full-fidelity level; which artifact it actually applies depends on
    # what this backup set holds and whether the tar is usable against the CURRENT engine.
    use_native = not (backup.tar is not None and tar_usable)
    if use_native and backup.native is None:
        if backup.tar is not None:
            # A tar exists but doesn't match the current engine — extracting it into
            # `.beads/embeddeddolt` under a live non-embedded engine writes a directory
            # nothing reads, and would report success doing it (bh-kobw's restore-side twin).
            out.actions.append(
                "✗ HQ's dolt engine does not look embedded (bd's own .beads/metadata.json "
                f"doesn't say dolt_mode: embedded) — extracting {_TAR_NAME} there would "
                "restore nothing the engine reads"
            )
        else:
            out.actions.append(f"✗ {backup.label} has no {_TAR_NAME}")
        return out

    if use_native:
        return _run_native(cfg, hq_dir, backup, out, dry_run=dry_run, confirm=confirm)
    return _run_tar(hq_dir, backup, out, dry_run=dry_run, confirm=confirm)


def _run_jsonl(cfg, hq_dir, backup, out, *, dry_run, confirm):
    _plan_jsonl(hq_dir, backup, out)
    if dry_run:
        out.ok = True
        return out
    if not confirm:
        out.actions.append("✗ refusing to overwrite live HQ data without --confirm")
        return out
    return _apply_jsonl(cfg, hq_dir, backup, out)


def _run_tar(hq_dir, backup, out, *, dry_run, confirm):
    _plan_tar(hq_dir, backup, out)
    if dry_run:
        out.ok = True
        return out
    if not confirm:
        out.actions.append("✗ refusing to overwrite live HQ data without --confirm")
        return out
    return _apply_tar(hq_dir, backup, out)


def _run_native(cfg, hq_dir, backup, out, *, dry_run, confirm):
    _plan_native(hq_dir, backup, out)
    if dry_run:
        out.ok = True
        return out
    if not confirm:
        out.actions.append("✗ refusing to overwrite live HQ data without --confirm")
        return out
    return _apply_native(cfg, hq_dir, backup, out)


# ---- tar level: full-fidelity store replacement (embedded HQ) -----------------


def _plan_tar(hq_dir: Path, backup: BackupSet, out: RestoreResult) -> None:
    store = store_locator.embedded_store_dir(hq_dir)
    if store.is_dir():
        out.actions.append(f"move aside existing store {store} (kept, not deleted)")
    out.actions.append(f"extract {backup.tar} -> {store.parent}")


def _apply_tar(hq_dir: Path, backup: BackupSet, out: RestoreResult) -> RestoreResult:
    store = store_locator.embedded_store_dir(hq_dir)
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


# ---- native level: full-fidelity restore over the connection (non-embedded HQ) -


def _plan_native(hq_dir: Path, backup: BackupSet, out: RestoreResult) -> None:
    out.actions.append(
        f"bd backup restore {backup.native} --force (Dolt-native, over the connection — no "
        "move-aside step: unlike the tar level, this overwrites the live store in place)"
    )


def _apply_native(cfg: dict, hq_dir: Path, backup: BackupSet, out: RestoreResult) -> RestoreResult:
    res = engine.get_engine(cfg).backup_restore(hq_dir, backup.native)
    if res.returncode:
        from .bd import err_line

        out.actions.append(f"✗ bd backup restore failed: {err_line(res)}")
        return out
    out.ok = True
    out.actions.append(f"✓ restored store from {backup.native}")
    return out


# ---- jsonl level: the format-independent floor --------------------------------


def _plan_jsonl(hq_dir: Path, backup: BackupSet, out: RestoreResult) -> None:
    # Mirror `hub.ensure_store`'s own guard (`.beads/`), not the embedded store dir it happens
    # to contain. A non-embedded HQ has `.beads/` with its data elsewhere (bh-areg.1), so
    # testing the embedded store dir here would promise "create an HQ store" for a call that
    # then no-ops — and this is the level a non-embedded HQ now falls back to, so it is the
    # plan operators read (bh-kobw).
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
