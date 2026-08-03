"""`bh hive migrate-storage` — move a hive off bd's legacy embedded Dolt engine onto the
fleet's target mode, bd's shared server (`docs/design/dolt-server-mode-adr.md`:
``shared-server: true`` / ``BEADS_DOLT_SHARED_SERVER=1``). Per hive: back up (VERIFIED before
anything destructive), migrate, verify the result is readable AND complete, report. Fleet-wide:
resumable (a failure at hive 14 does not restart at hive 1) and per-hive isolated (one failure
does not strand the other 21).

NOT ``hive_migrate.py`` — that verb is the ws->bh rename (markers/hooks/skills); this is a Dolt
STORAGE-mode migration, a different axis entirely, filed separately (bh-areg.4) specifically so
the two never get overloaded onto one command.

Reuses bh-areg.1's shipped machinery rather than growing a second one: ``store_locator.py`` for
every filesystem fact (never a live ``bd dolt status`` probe), ``engine.py``'s connection-
oriented ``backup``/``backup_restore`` (the ONE backup format that survives a MODE change — a
tar of embedded's own on-disk layout does not, since nothing about a running Dolt sql-server
reads a directory called ``embeddeddolt``), and ``hq.BackupTarget``/``hq.BackupPlan`` for the
same verification discipline hq.py's pre-push backup already established (bh-kobw: never report
verified on an empty backup).

THREE CONSTRAINTS, added to this bead after filing, that shape the migrate step below:

1. **dolt_mode MUST be persisted into ``.beads/metadata.json`` itself** — not merely a config
   key or an env var. ``store_locator.is_embedded_mode()`` (bh-areg.1) reads THAT file, not a
   live probe, precisely because bd's own ``bd dolt status --json`` shape is ambiguous by mode
   (bh-u562.1 finding 9). Measured directly against a real bd binary (see the integration test):
   ``bd init --shared-server --reinit-local`` does NOT update ``dolt_mode`` in metadata.json even
   though it silently starts serving that project from the shared server — bd's own
   ``main.go:warnSharedServerEmbeddedMismatch`` documents this drift as a narrow anti-pattern; for
   this migration it is the DEFAULT FAILURE MODE unless corrected here. Left uncorrected, a
   restore later trusts a stale, orphaned ``embeddeddolt/`` tarball nothing reads — the exact
   silent-no-op bh-areg.1 was filed to kill. ``_fix_metadata_dolt_mode`` closes this, and
   ``verify_migration`` asserts it afterward: engine/metadata disagreement is a FAILED migration,
   not a warning. A pre-existing drift found on an UNMIGRATED hive (shared-server already active
   via env/config while metadata still says embedded) is surfaced as a finding — bd itself only
   warns about it.

2. **``backup.enabled=true`` is set as part of migrating.** Per ``bd backup --help``: auto-backup
   defaults ON in embedded mode when a git remote exists, and OFF in sql-server/shared-server mode
   (upstream's own reasoning: many clients on one shared server each registering a same-named
   backup remote and full-syncing would be "a self-amplifying storm"). Left alone, migrating
   silently disables automatic backups on every hive that had them — this fleet's migration must
   not leave it less durable than it found it.

3. **Serialized per hive; never two clones of one hive concurrently.** ``bd migrate`` (a
   different, unrelated bd subcommand than what this module drives) refuses in-place migration on
   a remote-backed database for exactly this reason (upstream #4259): two independent migrators
   fork the schema silently and unrecoverably. ``hive_migration_lock`` below is this module's own
   cross-process guard for the SAME hazard applied to a storage-mode move.

ROLLBACK — already settled by the ADR (Migration and rollback path), not re-derived here:
storage-mode migration (embedded -> shared, via ``bd backup``/``bd backup restore``) is
REVERSIBLE, full Dolt commit history preserved both directions. bd-BINARY schema migration
(six one-way v53->v59 upgrades bd HEAD already applied on arrival) is the one-way door, and it is
orthogonal to storage mode. ``ROLLBACK_NOTE`` below is this verb's own output stating that
distinction, printed before any real (non-dry-run) work begins — the requirement was to state it
correctly, not re-litigate it.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import bd as bd_mod
from . import config, engine, registry, store_locator
from .bd import err_line
from .hq import BackupPlan, BackupTarget  # the shared shape (bh-areg.1's precedent), not redrawn
from .run import run

BD_TIMEOUT = 120.0  # seconds — matches hq.py's BD_TIMEOUT for reinit/config/status calls
LOCK_STALE_SECONDS = 3600.0  # an hour with no PID alive behind it: reclaim, don't wait forever

SHARED_SERVER_FLAG = "--shared-server"
SHARED_SERVER_CONFIG_KEY = "dolt.shared-server"

ROLLBACK_NOTE = (
    "Storage-mode migration (embedded -> shared server) is REVERSIBLE: `bd backup`/"
    "`bd backup restore` preserve full Dolt commit history in both directions. This is "
    "SEPARATE from bd-binary schema migration (bd HEAD's one-way v53->v59 upgrades on "
    "arrival) — that door does not reopen, and it has nothing to do with storage mode."
)


def _backup_root(cfg: dict) -> Path:
    return config.home() / "storage-migrate-backups"


def _state_path(cfg: dict) -> Path:
    return config.home() / "storage-migrate-state.json"


def _lock_dir(cfg: dict) -> Path:
    return config.home() / "storage-migrate-locks"


def _sanitize_id(hive_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in hive_id)


# ---- backup: reuse engine.py's connection-oriented backup + hq.py's verification shape -----


def _issue_count(hive_dir: Path) -> int:
    """The hive's own reported issue count (`bd status`) — the cross-check every backup level
    below verifies against, never trusted from an export's line count alone. -1 when unreadable."""
    data = bd_mod.json(["status", "--no-activity"], hive_dir)
    if not isinstance(data, dict):
        return -1
    return int((data.get("summary") or {}).get("total_issues", -1))


def _backup_jsonl(
    hive_dir: Path, backup_dir: Path, cfg: dict, prefix: str, *, dry_run: bool
) -> BackupTarget:
    out = backup_dir / f"{prefix}-issues.jsonl"
    if dry_run:
        return BackupTarget(
            name="jsonl-export",
            path=str(out),
            detail="would `bd export` + verify line count == reported issue count",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    res = engine.get_engine(cfg).export_jsonl(hive_dir, out)
    if res.returncode or not out.exists():
        return BackupTarget(
            name="jsonl-export", path=str(out), detail=f"bd export failed: {err_line(res)}"
        )
    lines = sum(1 for _ in out.open())
    count = _issue_count(hive_dir)
    verified = count >= 0 and lines == count
    detail = (
        f"{lines} lines (bd reports {count} issues)"
        if count >= 0
        else f"{lines} lines (issue count unverifiable)"
    )
    return BackupTarget(
        name="jsonl-export",
        path=str(out),
        size_bytes=out.stat().st_size,
        verified=verified,
        detail=detail,
    )


def _backup_native(hive_dir: Path, backup_dir: Path, cfg: dict, *, dry_run: bool) -> BackupTarget:
    """The full-fidelity level — over the CONNECTION (`bd backup add` + `bd backup sync`), never
    a tar of embedded's own on-disk layout. This is deliberate and load-bearing: this backup must
    be restorable into a DIFFERENT engine mode (shared server) than it was taken from (embedded),
    and only the Dolt-native backup format crosses that boundary — a tar extracted into a live
    SQL server's data directory restores nothing the server reads (the same shape bh-areg.1's
    `_backup_dolt_native` exists for, applied here to a source that happens to be embedded)."""
    out = backup_dir / "dolt-native"
    if dry_run:
        return BackupTarget(
            name="dolt-native-backup",
            path=str(out),
            detail="would `bd backup add` + `bd backup sync` (Dolt-native, over the connection — "
            "the only format restorable into a DIFFERENT engine mode)",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    res = engine.get_engine(cfg).backup(hive_dir, out)
    if res.returncode:
        return BackupTarget(
            name="dolt-native-backup", path=str(out), detail=f"bd backup failed: {err_line(res)}"
        )
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.is_dir() else 0
    verified = size > 0
    detail = f"{size:,}B" if verified else "bd backup reported success but wrote nothing"
    return BackupTarget(
        name="dolt-native-backup", path=str(out), size_bytes=size, verified=verified, detail=detail
    )


def take_backup(
    hive_dir: Path, backup_dir: Path, cfg: dict, prefix: str, *, dry_run: bool
) -> BackupPlan:
    """Two levels, both VERIFIED (bh-kobw discipline — a green checkmark on an empty backup is
    the failure this exists to prevent): the portable JSONL floor, and the connection-oriented
    Dolt-native full-fidelity level `_apply_native` below actually restores from."""
    plan = BackupPlan(dry_run=dry_run)
    plan.targets.append(_backup_jsonl(hive_dir, backup_dir, cfg, prefix, dry_run=dry_run))
    plan.targets.append(_backup_native(hive_dir, backup_dir, cfg, dry_run=dry_run))
    return plan


def echo_backup_plan(plan: BackupPlan) -> None:
    tag = "DRY-RUN " if plan.dry_run else ""
    typer.echo(f"    {tag}backup:")
    for t in plan.targets:
        mark = "○" if plan.dry_run else ("✓" if t.verified else "✗")
        size = f" ({t.size_bytes:,}B)" if t.size_bytes else ""
        where = f" {t.path}" if t.path else ""
        typer.echo(f"      {mark} {t.name}{where}{size} — {t.detail}")


# ---- filesystem-fact helpers (never a live `bd dolt status` probe) --------------------------


def _metadata_path(hive_dir: Path) -> Path:
    return hive_dir / ".beads" / "metadata.json"


def _read_metadata(hive_dir: Path) -> dict:
    try:
        data = json.loads(_metadata_path(hive_dir).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dolt_database_name(hive_dir: Path, fallback: str) -> str:
    return str(_read_metadata(hive_dir).get("dolt_database") or fallback)


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def shared_server_target_dir(db_name: str) -> Path:
    """Where bd's shared server would keep `db_name`'s data (bh-u562.1 finding 8) — a pure
    filesystem-fact preview for `--dry-run`, honoring the same env override bd itself reads."""
    root = os.environ.get("BEADS_SHARED_SERVER_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".beads" / "shared-server"
    return base / "dolt" / db_name


def _config_get_bool(hive_dir: Path, key: str) -> bool:
    data = bd_mod.json(["config", "get", key], hive_dir)
    if not isinstance(data, dict):
        return False
    val = data.get("value", data.get("effective"))
    return val is True or str(val).strip().lower() == "true"


def detect_pre_existing_drift(hive_dir: Path) -> str | None:
    """A pre-existing engine/metadata mismatch on a hive THIS VERB HAS NOT YET TOUCHED: shared-
    server mode already active (via `dolt.shared-server` in its own config.yaml — the persisted,
    per-hive form; the process-wide `BEADS_DOLT_SHARED_SERVER` env var is the same hazard but
    isn't a per-hive fact worth attributing to one hive) while `.beads/metadata.json` still says
    embedded. bd's own `main.go:warnSharedServerEmbeddedMismatch` only warns about this; the
    fleet driver surfaces it as a real finding (this bead's notes, constraint 1's last clause)."""
    if store_locator.dolt_mode(hive_dir) != "embedded":
        return None
    if not _config_get_bool(hive_dir, SHARED_SERVER_CONFIG_KEY):
        return None
    return (
        f"{hive_dir}: dolt.shared-server=true in .beads/config.yaml but .beads/metadata.json "
        'still pins dolt_mode="embedded" — bd is silently serving this hive from the shared '
        "server already; metadata has not caught up (pre-existing drift, not caused by this run)"
    )


# ---- the migrate step: reinit in place, then persist what bd leaves un-persisted ------------


def _bd(args: list[str], cwd: Path, *, actor: str = "", timeout: float = BD_TIMEOUT):
    """Run a `bd` subcommand scoped to `cwd`. Passes BOTH `-C <cwd>` AND the subprocess's own
    `cwd=` kwarg, deliberately redundant: measured against a real bd binary, `bd init
    --reinit-local`'s "remote already has Dolt history" pre-flight check does NOT consistently
    honor `-C` alone — invoked from a process whose OWN working directory happens to sit inside
    an unrelated git repo that has a real `origin` with `refs/dolt/data` (exactly this repo's
    own dev worktree, which dogfoods bd), it refused citing THAT ambient repo's remote, not
    `cwd`'s. Setting the subprocess's actual OS-level working directory to `cwd` too closes the
    gap regardless of which one bd's check ends up trusting. Escalated (bh escalate) as a bd bug
    rather than routed around less directly."""
    cmd = ["bd", "-C", str(cwd)]
    if actor:
        cmd += ["--actor", actor]
    cmd += args
    return run(cmd, check=False, capture=True, timeout=timeout, cwd=str(cwd))


def _reinit_shared_server(hive_dir: Path, prefix: str, db_name: str, actor: str):
    args = ["init", "--prefix", prefix, SHARED_SERVER_FLAG, "--reinit-local", "--non-interactive"]
    if db_name and db_name != prefix:
        args += ["--database", db_name]
    return _bd(args, hive_dir, actor=actor, timeout=BD_TIMEOUT)


def _fix_metadata_dolt_mode(hive_dir: Path, mode: str = "server") -> None:
    """Constraint 1: persist `dolt_mode` into `.beads/metadata.json` ITSELF — `bd init
    --shared-server --reinit-local` does not do this on its own (measured against a real bd
    binary; see the module docstring and the integration test), leaving the exact drift this
    migration exists to close if skipped."""
    path = _metadata_path(hive_dir)
    data = _read_metadata(hive_dir)
    data["dolt_mode"] = mode
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _persist_shared_server_config(hive_dir: Path, actor: str) -> None:
    """Make shared-server mode durable across FUTURE invocations without relying on
    `BEADS_DOLT_SHARED_SERVER` being exported forever — `dolt.shared-server: true` in
    `.beads/config.yaml` is what bd itself reads absent the env var (measured)."""
    _bd(["config", "set", SHARED_SERVER_CONFIG_KEY, "true"], hive_dir, actor=actor)


def _persist_backup_enabled(hive_dir: Path, actor: str) -> None:
    """Constraint 2: `backup.enabled` defaults OFF in server/shared-server mode even when it was
    ON in embedded — set it explicitly so migrating never leaves a hive less durable than it
    found it (docs/design/dolt-server-mode-adr.md Consequence 1)."""
    _bd(["config", "set", "backup.enabled", "true"], hive_dir, actor=actor)


def _move_aside_embedded_store(hive_dir: Path) -> str | None:
    """Never delete the old store outright (hq_restore.py's own `_apply_tar` precedent: move
    aside, keep it recoverable) — and doing so also keeps `store_locator.has_embedded_store()`
    correct for every other consumer (hq.py's own backup dispatch included) once this hive is no
    longer embedded."""
    store = store_locator.embedded_store_dir(hive_dir)
    if not store.is_dir():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    aside = store.with_name(f"{store.name}.pre-migrate-{stamp}")
    store.rename(aside)
    return str(aside)


# ---- verify: readable AND complete ------------------------------------------------------------


@dataclass
class VerifyOutcome:
    ok: bool = True
    issue_count: int = -1
    schema_version: str = ""
    dolt_mode: str = ""
    problems: list[str] = field(default_factory=list)


def verify_migration(hive_dir: Path, pre_count: int, cfg: dict) -> VerifyOutcome:
    """Readable AND complete, not merely "the command exited 0" (bh-00cq's own lesson — a clone
    that completes and then cannot be opened): a real open (`bd status`) succeeds, the issue
    count matches pre-migration, a schema version is recorded, AND (constraint 1) the persisted
    `dolt_mode` actually says server — an engine/metadata disagreement here is a FAILED
    migration, not a warning."""
    out = VerifyOutcome()
    data = bd_mod.json(["status", "--no-activity"], hive_dir)
    if not isinstance(data, dict):
        out.ok = False
        out.problems.append("bd status failed against the migrated store — it did not open")
        return out
    out.issue_count = int((data.get("summary") or {}).get("total_issues", -1))
    if pre_count >= 0 and out.issue_count != pre_count:
        out.ok = False
        out.problems.append(
            f"issue count mismatch: {pre_count} before migration, {out.issue_count} after"
        )

    status = bd_mod.json(["dolt", "status"], hive_dir)
    out.schema_version = str((status or {}).get("schema_version", "unknown"))

    out.dolt_mode = store_locator.dolt_mode(hive_dir) or "unknown"
    if out.dolt_mode != "server":
        out.ok = False
        out.problems.append(
            f"persisted dolt_mode is {out.dolt_mode!r}, expected 'server' — engine/metadata "
            "disagreement is a failed migration, not a warning"
        )
    return out


# ---- per-hive serialization (constraint 3) -----------------------------------------------------


class HiveLocked(Exception):
    """Another migrator already holds this hive's lock."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True  # exists, just not ours to signal — treat as alive
    return True


@contextlib.contextmanager
def hive_migration_lock(cfg: dict, hive_id: str):
    """One migrator per hive at a time (constraint 3): `bd migrate` refuses two concurrent
    migrators against the same remote-backed database for exactly this reason (upstream #4259,
    silent unrecoverable schema fork) — this is the same hazard applied to a storage-mode move,
    guarded here rather than assumed away. A stale lock (holder PID no longer alive, or older
    than LOCK_STALE_SECONDS) is reclaimed rather than wedging the fleet driver forever."""
    lock_dir = _lock_dir(cfg)
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{_sanitize_id(hive_id)}.lock"
    acquired = False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        acquired = True
    except FileExistsError:
        stale = False
        try:
            holder = int(path.read_text().strip() or "0")
            age = datetime.now(UTC).timestamp() - path.stat().st_mtime
            stale = age > LOCK_STALE_SECONDS or not _pid_alive(holder)
        except (OSError, ValueError):
            stale = True
        if stale:
            with contextlib.suppress(OSError):
                path.unlink()
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            acquired = True
        else:
            raise HiveLocked(
                f"{hive_id} is locked by another in-progress migration ({path})"
            ) from None
    try:
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                path.unlink()


# ---- single-hive orchestration: backup -> migrate -> verify -> report -----------------------


@dataclass
class HiveMigrationResult:
    hive_id: str
    hive_dir: Path
    prefix: str = ""
    status: str = "pending"  # migrated|already-migrated|would-migrate|skipped|failed
    detail: str = ""
    backup_plan: BackupPlan | None = None
    pre_issue_count: int = -1
    post_issue_count: int = -1
    schema_version: str = ""
    dolt_mode: str = ""
    size_bytes: int = 0
    target_path: str = ""
    findings: list[str] = field(default_factory=list)


def _effective_prefix(hive_dir: Path, entry: dict) -> str:
    data = bd_mod.json(["config", "get", "issue_prefix"], hive_dir)
    if isinstance(data, dict) and data.get("value"):
        return str(data["value"])
    return str(entry.get("prefix") or entry.get("repo") or "")


def migrate_hive(
    entry: dict, cfg: dict, *, dry_run: bool = False, actor: str = ""
) -> HiveMigrationResult:
    """One hive's full lifecycle: back up (VERIFIED before anything destructive) -> migrate ->
    verify -> report. Idempotent — a hive already off embedded is a no-op (but still heals a
    partially-applied prior run: `backup.enabled`/`dolt.shared-server` get re-asserted, cheap and
    safe either way)."""
    hive_id = registry.hive_key(entry)
    hive_dir = registry.hive_dir(entry)
    result = HiveMigrationResult(hive_id=hive_id, hive_dir=hive_dir)

    if not (hive_dir / ".beads").is_dir():
        result.status = "skipped"
        result.detail = "no local .beads/ checkout — nothing to migrate"
        return result

    drift = detect_pre_existing_drift(hive_dir)
    if drift:
        result.findings.append(drift)

    mode = store_locator.dolt_mode(hive_dir)
    prefix = _effective_prefix(hive_dir, entry)
    result.prefix = prefix

    if mode != "embedded":
        result.status = "already-migrated"
        result.dolt_mode = mode or "unknown"
        if not dry_run:
            _persist_backup_enabled(hive_dir, actor)
            _persist_shared_server_config(hive_dir, actor)
        return result

    embedded_dir = store_locator.embedded_store_dir(hive_dir)
    result.size_bytes = _dir_size(embedded_dir)
    db_name = _dolt_database_name(hive_dir, prefix)
    result.target_path = str(shared_server_target_dir(db_name))

    if dry_run:
        result.status = "would-migrate"
        return result

    try:
        with hive_migration_lock(cfg, hive_id):
            pre_count = _issue_count(hive_dir)
            backup_dir = (
                _backup_root(cfg)
                / _sanitize_id(hive_id)
                / datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
            )
            plan = take_backup(hive_dir, backup_dir, cfg, prefix, dry_run=False)
            result.backup_plan = plan
            if not plan.ok:
                result.status = "failed"
                result.detail = "backup could not be verified — refusing to migrate"
                return result

            reinit = _reinit_shared_server(hive_dir, prefix, db_name, actor)
            if reinit.returncode:
                result.status = "failed"
                result.detail = f"bd init --reinit-local refused: {err_line(reinit)}"
                return result

            _fix_metadata_dolt_mode(hive_dir, "server")
            _persist_shared_server_config(hive_dir, actor)
            _persist_backup_enabled(hive_dir, actor)

            native_dir = next(
                (Path(t.path) for t in plan.targets if t.name == "dolt-native-backup"), None
            )
            restored = engine.get_engine(cfg).backup_restore(hive_dir, native_dir, actor=actor)
            if restored.returncode:
                result.status = "failed"
                result.detail = f"bd backup restore failed: {err_line(restored)}"
                return result

            verify = verify_migration(hive_dir, pre_count, cfg)
            result.pre_issue_count = pre_count
            result.post_issue_count = verify.issue_count
            result.schema_version = verify.schema_version
            result.dolt_mode = verify.dolt_mode
            if not verify.ok:
                result.status = "failed"
                result.detail = "; ".join(verify.problems)
                return result

            _move_aside_embedded_store(hive_dir)
            result.status = "migrated"
            return result
    except HiveLocked as exc:
        result.status = "skipped"
        result.detail = str(exc)
        return result


# ---- fleet mode: resumable, per-hive isolated, HQ last (the hard case) ----------------------


def _load_state(cfg: dict) -> dict:
    path = _state_path(cfg)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(cfg: dict, state: dict) -> None:
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def fleet_order(cfg: dict) -> list[dict]:
    """Every registered hive, ordinary hives first (sorted for determinism), Factory HQ LAST —
    the hard case (1.6 GB; its backup gate has to already work, bh-areg.1) that this bead's own
    notes say should be the last one attempted, not the first."""
    entries = sorted(registry.hives(cfg), key=lambda e: (str(e["org"]), str(e["repo"])))
    hq = registry.hive_of_kind(cfg, registry.HQ_KIND)
    return [*entries, hq] if hq is not None else entries


def migrate_fleet(
    cfg: dict, *, dry_run: bool = False, actor: str = ""
) -> list[HiveMigrationResult]:
    """Resumable (a state file records each hive's outcome as it finishes, so a killed run picks
    up where it left off instead of restarting at hive 1) and per-hive isolated (a `migrate_hive`
    failure never raises past this loop — the next hive still gets attempted)."""
    state = {} if dry_run else _load_state(cfg)
    results: list[HiveMigrationResult] = []
    for entry in fleet_order(cfg):
        hive_id = registry.hive_key(entry)
        if not dry_run and state.get(hive_id, {}).get("status") == "migrated":
            results.append(
                HiveMigrationResult(
                    hive_id=hive_id,
                    hive_dir=registry.hive_dir(entry),
                    status="already-migrated",
                    detail="resumed: recorded migrated in a prior run",
                )
            )
            continue
        result = migrate_hive(entry, cfg, dry_run=dry_run, actor=actor)
        results.append(result)
        if not dry_run:
            state[hive_id] = {
                "status": result.status,
                "detail": result.detail,
                "attempted_at": datetime.now(UTC).isoformat(),
            }
            _save_state(cfg, state)
    return results


# ---- CLI-facing entry point + rendering ------------------------------------------------------


def _echo_result(r: HiveMigrationResult) -> None:
    icon = {
        "migrated": "✓",
        "already-migrated": "•",
        "would-migrate": "○",
        "skipped": "⚠",
        "failed": "✗",
    }.get(r.status, "?")
    typer.echo(f"{icon} {r.hive_id}  [{r.status}]" + (f" — {r.detail}" if r.detail else ""))
    if r.status == "would-migrate":
        typer.echo(f"    size: {r.size_bytes:,}B  target: {r.target_path}")
    if r.backup_plan is not None:
        echo_backup_plan(r.backup_plan)
    if r.status == "migrated":
        typer.echo(
            f"    issues: {r.pre_issue_count} -> {r.post_issue_count}  "
            f"schema_version: {r.schema_version}  dolt_mode: {r.dolt_mode}"
        )
    for f in r.findings:
        typer.echo(f"    finding: {f}")


def migrate(hive_id: str = "", *, dry_run: bool = False, confirm: bool = False) -> None:
    """`bh hive migrate-storage [HIVE_ID]`: one hive (HIVE_ID given) or the whole fleet (empty —
    HQ last), backup -> migrate -> verify -> report."""
    from .identity import resolve_actor

    cfg = config.load()
    if not dry_run:
        typer.echo(ROLLBACK_NOTE)
        if not confirm:
            typer.echo(
                "✗ refusing to migrate without --confirm (pass --dry-run to preview first)",
                err=True,
            )
            raise typer.Exit(1)

    if hive_id:
        entry = registry.resolve_hive(cfg, hive_id)
        actor = resolve_actor("", "", cwd=registry.hive_dir(entry))
        result = migrate_hive(entry, cfg, dry_run=dry_run, actor=actor)
        _echo_result(result)
        if result.status == "failed":
            raise typer.Exit(1)
        return

    actor = resolve_actor("", "")
    results = migrate_fleet(cfg, dry_run=dry_run, actor=actor)
    for r in results:
        _echo_result(r)
    failed = [r for r in results if r.status == "failed"]
    migrated = sum(1 for r in results if r.status in ("migrated", "already-migrated"))
    typer.echo(
        f"\n{migrated} migrated/up-to-date, {len(failed)} failed, {len(results)} total"
        + (" (dry-run — nothing changed)" if dry_run else "")
    )
    if failed:
        raise typer.Exit(1)
