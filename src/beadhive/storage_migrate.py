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

MECHANISM SELECTION (bh-oa225) — ``_reinit_shared_server``'s ``bd init --reinit-local`` refuses
outright whenever the remote already carries ``refs/dolt/data`` (proven on this fleet: every hive
that has ever pushed bead state, which durability practice already recommends). Remote Dolt
history is ``bd bootstrap``'s PRECONDITION, not its blocker — exactly the branch ``onboard.py``'s
own bd-mint step already takes (``_act_bd_init``'s ``elif _origin_has_dolt_data(ctx)``). So the
migrate step SELECTS its mechanism the same way, on the same lifted probe
(:func:`origin_has_dolt_data`, shared with ``onboard.py`` so the two can never drift into two
different answers): bootstrap-from-origin when the remote has it, reinit-in-place otherwise.

Measured for THIS bead against a real bd binary, against a hive with a LIVE (non-empty) embedded
store — the case ``onboard.py``'s own bootstrap branch never exercises, since a zero-footprint
onboard's ``.beads/`` always arrives from git with no database: ``bd bootstrap`` does NOT refuse
and does NOT error against a live embedded store. It clones/syncs the remote's Dolt data into a
fresh shared-server-mode database and does NOT delete the old ``embeddeddolt/`` directory (left
orphaned on disk, physically intact — ``_retire_embedded_store`` tidies it up afterward). What it
DOES do, silently and with exit 0, is DISCARD anything in the live
embedded store that was never pushed to that remote — confirmed by creating an unpushed issue,
bootstrapping, and watching it vanish from ``bd list``. This is exactly the hazard
``migrate_hive``'s EXISTING post-mechanism step already exists to close: the pre-migration
Dolt-native backup (taken from the live embedded store BEFORE the mechanism runs at all) is
restored (``bd backup restore --force``) on top of the freshly-bootstrapped store immediately
after, and that restore brings the dropped, unpushed issue right back — verified end to end
against a real bd binary (``test_storage_migrate_int.py``). No separate "move the embedded store
aside first" step turned out to be needed: the backup/restore pairing already in place for the
reinit path (which starts from an empty store for the same reason) covers bootstrap's data-loss
window for free, and ``verify_migration``'s pre/post issue-count parity check is the backstop if
it somehow didn't.

ALSO measured, and load-bearing for ``_bootstrap_shared_server``'s own shape: unlike ``bd init
--shared-server``, ``bd bootstrap`` does NOT auto-start bd's own managed shared dolt server when
one isn't already reachable — it fails outright (``connection refused``) rather than spawning
one the way ``bd init --shared-server --reinit-local`` demonstrably does. Harmless on the real
fleet (the shared server is already a long-running process there), but not something a fresh
host's very first migration may assume. bh-l90xk (revising the original "runs first,
unconditionally" plan here): probe first via ``dolt_health.probe_shared_server()`` — a real
endpoint connect, no subprocess — and only fall back to ``bd dolt start --global`` when nothing
answers. Measured on a real fleet host: the shared server was already listening on 3308 (started
outside bd's own bookkeeping), and unconditionally running ``bd dolt start --global`` anyway
returned ``rc=1`` — "port 3308 is busy but cannot identify the process" (bh-hqmcl's own
territory) — aborting every migration before bootstrap was ever reached, even though the server
bootstrap actually needs was reachable the whole time.

bh-l90xk ALSO measured a second, independent defect in how that failure was reported: the
caller wrapped ``bd dolt start --global``'s ``CompletedProcess`` in the exact two-phase-output
trap this module's own "MECHANISM SELECTION" section originally warned about for a DIFFERENT
call (``hub.ensure_store``'s docstring documents the canonical case, ``bd init
--shared-server``) — ``err_line`` reads stdout + stderr and returns the FIRST non-empty line,
which was ``bd``'s own informational ``Notice: shared-server mode is enabled ...`` printed
before the real ``Error: cannot start dolt server on port 3308: ...`` beneath it, AND the
message hardcoded "bd bootstrap" as the failing command when the invocation that actually
returned non-zero was ``bd dolt start --global``. Two independent fixes, both in
:func:`migrate_hive`'s mechanism dispatch: :func:`_significant_err_line` skips a leading
``Notice:``/``Hint:`` block (and its indented continuation lines) and prefers an ``Error:``-
prefixed line when one exists; :class:`MechanismOutcome` carries the command label alongside
its result so it always names the invocation that actually failed, never a hardcoded guess.

bh-8g6cj (re-opening bh-oa225's "no separate move-aside-first step needed" finding just above,
under closer measurement — see that finding: it stands) narrows WHY ``bd bootstrap`` sometimes
declines outright (``✓ Database already exists ... Nothing to do``, ``rc=0``, nothing migrated)
even though a live embedded store bootstraps and migrates cleanly on its own. Measured directly,
repeatedly, against a real bd binary and a real shared dolt server: the deciding factor is NOT
whether a local embedded store exists — it is whether the SERVER-SIDE database name ``bd
bootstrap`` targets already exists on the shared server, for ANY reason. And that target name is
resolved from ``.beads/metadata.json``'s plain, bd-owned ``dolt_database`` key — the SAME
generic-default field bh-g5ujg already found colliding across hives ("beads" for almost every
one), NOT this module's own collision-free ``dolt_server_database``/prefix-derived name
(:func:`store_locator.server_database`), which ``bd bootstrap`` has no way to know about (it has
no ``--database`` override that works in shared-server mode — measured: the global
``--database`` flag is documented, and confirmed, "proxied-server mode only"; passing it here is
silently ignored). So on a real hive whose ``dolt_database`` is still bd's generic default, the
migration driver's own already-correct collision-free name (``db_name``, computed once already
for the reinit path) was simply never handed to bootstrap at all — bootstrap kept resolving
"beads", which the fleet's shared server almost always already hosts for some OTHER hive, and
declined.

THE FIX, measured end-to-end (clone succeeds, ``dolt_mode`` persists to ``server``, the
previously-unpushed issue is dropped exactly as bh-oa225 already documented and
``backup_restore`` already recovers): repoint ``dolt_database`` to ``db_name`` in
``metadata.json`` immediately before calling bootstrap, and restore the ORIGINAL value right
back if bootstrap doesn't actually migrate (declines or errors) — the still-embedded hive must
stay exactly as readable as it was, and its on-disk ``embeddeddolt/<original-name>/``
subdirectory name still matches the restored metadata. Do NOT restore it after a SUCCESSFUL
bootstrap: measured directly that doing so breaks the migrated hive outright (``PROJECT IDENTITY
MISMATCH — refusing to connect``) — unlike this module's own additive ``dolt_server_database``
key, bd has no notion of a "one-time bootstrap target" separate from "the database this project
connects to"; ``dolt_database`` is BOTH, for bd itself, forever after. No reordering of
``_move_aside_embedded_store`` was needed for any of this (bh-oa225's finding already covers
why); it stays last.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import bd as bd_mod
from . import config, dolt_health, engine, registry, store_locator
from .bd import err_line
from .hq import BackupPlan, BackupTarget  # the shared shape (bh-areg.1's precedent), not redrawn
from .run import run

BD_TIMEOUT = 120.0  # seconds — matches hq.py's BD_TIMEOUT for reinit/config/status calls
GIT_TIMEOUT = 30.0  # a single `git ls-remote` round trip — matches gitref.py/hq.py's convention
LOCK_STALE_SECONDS = 3600.0  # an hour with no PID alive behind it: reclaim, don't wait forever

SHARED_SERVER_FLAG = "--shared-server"
SHARED_SERVER_CONFIG_KEY = "dolt.shared-server"
SHARED_SERVER_ENV_VAR = "BEADS_DOLT_SHARED_SERVER"

# bh-xsv3: `.beads/.gitignore`'s exact-name `embeddeddolt/` entry does NOT match
# `_retire_embedded_store`'s own renamed directory (`embeddeddolt.pre-migrate-<stamp>/` — a
# genuinely different name), so on a FURNISHED hive (tracked `.beads/`) a KEPT moved-aside store
# surfaces as hundreds of MB of untracked files, one `git add -A` away from being committed.
# A positive glob pattern, not a negation — the file's own trailing comment ("Do NOT add
# negation patterns here") is about negation specifically, and this isn't one.
#
# bh-5009a narrowed this to the `--keep-pre-migrate` path and RETIRED the auto-commit that
# came with it. The default path no longer leaves the directory on disk at all, so there is
# nothing to ignore; and writing a commit into the operator's repo as a side effect of a
# storage migration was never `bh`'s call to make — it now reports the edit instead.
PRE_MIGRATE_GITIGNORE_PATTERN = f"{store_locator.EMBEDDED_STORE_NAME}.pre-migrate-*/"

ROLLBACK_NOTE = (
    "Storage-mode migration (embedded -> shared server) is REVERSIBLE: `bd backup`/"
    "`bd backup restore` preserve full Dolt commit history in both directions. This is "
    "SEPARATE from bd-binary schema migration (bd HEAD's one-way v53->v59 upgrades on "
    "arrival) — that door does not reopen, and it has nothing to do with storage mode."
)

# bh-l90xk: lines starting with either prefix are bd telling the operator it is PROCEEDING
# ("Using the shared server for this run"), never a refusal — `err_line`'s plain first-
# non-empty-line rule picks these up as readily as a real `Error:` line, which is the trap.
_ADVISORY_LINE_PREFIXES = ("Notice:", "Hint:")

# bh-hqmcl's own territory (a shared dolt server started outside bd's bookkeeping, so bd sees a
# busy port it cannot attribute to itself) — matched loosely (substring, not the exact port
# number) so this still fires if the wording ever picks up a different port.
_PORT_BUSY_UNATTRIBUTABLE_MARKER = "busy but cannot identify the process"


def _significant_err_line(res) -> str:
    """Like :func:`beadhive.bd.err_line`, but never lets an informational ``Notice:``/``Hint:``
    line (or its indented continuation lines) stand in for the real failure reason — the SAME
    two-phase-output trap ``hub.ensure_store``'s own docstring already documents for `bd init
    --shared-server`, reintroduced here for `bd dolt start`/`bd bootstrap` (bh-l90xk). Prefers
    the first ``Error:``-prefixed line when one is present (bd's own convention for the actual
    headline); otherwise the first line that isn't part of an advisory block; falls back to
    :func:`beadhive.bd.err_line`'s plain behavior only when every line turned out to be
    advisory (never silently returns nothing)."""
    lines = ((res.stdout or "") + (res.stderr or "")).splitlines()
    significant: list[str] = []
    in_advisory_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            in_advisory_block = False
            continue
        if in_advisory_block and line[:1].isspace():
            continue  # an indented continuation of the notice/hint block just skipped
        in_advisory_block = stripped.startswith(_ADVISORY_LINE_PREFIXES)
        if in_advisory_block:
            continue
        significant.append(stripped)
    for line in significant:
        if line.startswith("Error:"):
            return line
    return significant[0] if significant else err_line(res)


def _is_port_busy_unattributable(res) -> bool:
    """True when `res` is `bd dolt start`'s own "a server IS listening on this port, but bd
    can't tell whose it is" refusal (bh-hqmcl's territory) — the exact condition that made
    `_bootstrap_shared_server` abort every migration on a host whose shared server was started
    outside bd's own bookkeeping, measured directly on a real fleet host."""
    text = (res.stdout or "") + (res.stderr or "")
    return _PORT_BUSY_UNATTRIBUTABLE_MARKER in text


@dataclass
class MechanismOutcome:
    """One migrate mechanism's result PLUS which command actually produced it (bh-l90xk):
    `_bootstrap_shared_server` is a two-command dispatch (`bd dolt start --global` then `bd
    bootstrap`), and a caller that hardcodes "bd bootstrap" as the label regardless of which one
    actually returned non-zero misattributes the failure — measured directly on a real fleet
    host, where the `dolt start` step was the one that failed."""

    result: object
    command_label: str
    port_busy_unattributable: bool = False


def _backup_root(cfg: dict) -> Path:
    """``$BH_HOME/backups/migrate`` — root 4 of the backup contract (bh-5009a).

    Was ``~/.beadhive/storage-migrate-backups/<flattened-sanitized-hive-id>/``: outside the repo
    (correct — the ADR's boundary always put verified backups there) but invisible to
    ``bh backup usage``, keyed by a second hive-addressing scheme nothing else in the codebase
    used, and with no retention policy at all. nvhack's migration alone wrote 28 MB that nothing
    would ever have pruned."""
    from . import backup as backup_mod

    return backup_mod.migrate_root(cfg)


def _state_path(cfg: dict) -> Path:
    return config.home() / "storage-migrate-state.json"


def _lock_dir(cfg: dict) -> Path:
    return config.home() / "storage-migrate-locks"


def _sanitize_id(hive_id: str) -> str:
    """Flatten a hive id into one filesystem-safe component.

    No longer used to ADDRESS a backup — bh-5009a keys the migrate root on the
    ``<provider>/<org>/<repo>`` triplet, which needs no sanitization — but kept because it is
    still how the pre-bh-5009a directory names were built, and ``backup._legacy_migrate_slug_map``
    replays it forward over the registry to resolve those names back to triplets. Also names the
    per-hive lock file, where flat is what's wanted."""
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in hive_id)


# ---- backup: reuse engine.py's connection-oriented backup + hq.py's verification shape -----


def _issue_count(hive_dir: Path) -> int:
    """The hive's own reported issue count (`bd status`) — the cross-check every backup level
    below verifies against, never trusted from an export's line count alone. -1 when unreadable."""
    data = bd_mod.json(["status", "--no-activity"], hive_dir)
    if not isinstance(data, dict):
        return -1
    return int((data.get("summary") or {}).get("total_issues", -1))


def _backup_jsonl(hive_dir: Path, backup_dir: Path, cfg: dict, *, dry_run: bool) -> BackupTarget:
    # `issues.jsonl`, not `<prefix>-issues.jsonl` (bh-5009a): the containing path already names
    # the hive, and the un-prefixed name matches what every other root writes — so a set stays
    # readable by the same tooling even after a prefix rename, which a prefixed filename does
    # not (the file would keep claiming a prefix the hive no longer has).
    out = backup_dir / "issues.jsonl"
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
    hive_dir: Path,
    backup_dir: Path,
    cfg: dict,
    prefix: str,
    *,
    dry_run: bool,
    hive: str = "",
) -> BackupPlan:
    """Two levels, both VERIFIED (bh-kobw discipline — a green checkmark on an empty backup is
    the failure this exists to prevent): the portable JSONL floor, and the connection-oriented
    Dolt-native full-fidelity level `_apply_native` below actually restores from. Taking BOTH is
    what makes this the strongest artifact `bh` writes, and why it — not the moved-aside store —
    is the canonical pre-migration copy.

    A real run also drops a `manifest.json` recording what the set is and whether it verified
    (bh-5009a), so `bh backup usage` and any later restore read a fact instead of inferring one
    from directory mtimes."""
    plan = BackupPlan(dry_run=dry_run)
    plan.targets.append(_backup_jsonl(hive_dir, backup_dir, cfg, dry_run=dry_run))
    plan.targets.append(_backup_native(hive_dir, backup_dir, cfg, dry_run=dry_run))
    if not dry_run:
        from . import backup as backup_mod

        backup_mod.write_manifest(
            backup_dir,
            kind="migrate",
            hive=hive,
            prefix=prefix,
            verified=plan.ok,
            artifacts={t.name: t.size_bytes for t in plan.targets if t.size_bytes},
            source_dolt_mode="embedded",
            target_dolt_mode="server",
            issue_count=_issue_count(hive_dir),
        )
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


def origin_has_dolt_data(cwd) -> bool:
    """True when `cwd`'s git `origin` already carries beads state under `refs/dolt/data` — a
    live network probe (never a filesystem fact, unlike this module's other helpers above), but
    read-only and side-effect-free.

    THE ONE PROBE both `onboard.py`'s fresh-mint step (`_act_bd_init`'s bootstrap branch) and
    this module's migrate step select their bd mechanism on (bh-oa225) — lifted here rather than
    kept as two copies that can drift apart. `onboard._origin_has_dolt_data` delegates to this
    directly; storage_migrate is the lower module in the import graph (`onboard.py` already
    imports `SHARED_SERVER_CONFIG_KEY`/`SHARED_SERVER_FLAG` from here), so this is the direction
    that avoids a circular import rather than the reverse.

    `cwd` accepts anything the git binary can be pointed at with `cwd=` (a `Path`, a `str`, or
    `None`/"" to mean "inherit the caller's own process cwd" — the shape `onboard.Ctx.cwd`
    itself carries)."""
    res = run(
        ["git", "ls-remote", "origin", "refs/dolt/data"],
        cwd=str(cwd) if cwd else None,
        check=False,
        capture=True,
        timeout=GIT_TIMEOUT,
    )
    return getattr(res, "returncode", 1) == 0 and bool((getattr(res, "stdout", "") or "").strip())


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


def _bd(
    args: list[str],
    cwd: Path,
    *,
    actor: str = "",
    timeout: float = BD_TIMEOUT,
    env: dict | None = None,
):
    """Run a `bd` subcommand scoped to `cwd`. Passes BOTH `-C <cwd>` AND the subprocess's own
    `cwd=` kwarg, deliberately redundant: measured against a real bd binary, `bd init
    --reinit-local`'s "remote already has Dolt history" pre-flight check does NOT consistently
    honor `-C` alone — invoked from a process whose OWN working directory happens to sit inside
    an unrelated git repo that has a real `origin` with `refs/dolt/data` (exactly this repo's
    own dev worktree, which dogfoods bd), it refused citing THAT ambient repo's remote, not
    `cwd`'s. Setting the subprocess's actual OS-level working directory to `cwd` too closes the
    gap regardless of which one bd's check ends up trusting. Escalated (bh escalate) as a bd bug
    rather than routed around less directly.

    `env`, when given, is GAP-FILLED over the real environment (never a replacement of it —
    `run.run`'s own `env=` kwarg IS the full child environment, not an overlay), so a caller only
    ever has to name the ONE variable it needs on top of everything else `bd` expects (`PATH`,
    `HOME`, ...)."""
    cmd = ["bd", "-C", str(cwd)]
    if actor:
        cmd += ["--actor", actor]
    cmd += args
    run_env = dict(os.environ, **env) if env else None
    return run(cmd, check=False, capture=True, timeout=timeout, cwd=str(cwd), env=run_env)


def _reinit_shared_server(hive_dir: Path, prefix: str, db_name: str, actor: str):
    args = ["init", "--prefix", prefix, SHARED_SERVER_FLAG, "--reinit-local", "--non-interactive"]
    if db_name and db_name != prefix:
        args += ["--database", db_name]
    return _bd(args, hive_dir, actor=actor, timeout=BD_TIMEOUT)


def _bootstrap_shared_server(hive_dir: Path, actor: str) -> MechanismOutcome:
    """The bootstrap-from-origin mechanism (bh-oa225, module docstring's "MECHANISM SELECTION"):
    `bd bootstrap` has no `--shared-server`/`--database` flag of its own (unlike `bd init`) —
    same activation lever `onboard.py`'s own bootstrap branch uses, bd's own env var. No
    `--prefix`/`--database` to pass either: bootstrap reads the LOCAL project's own already-
    persisted prefix/database name (unchanged by this call, but see :func:`migrate_hive`'s own
    caller-side fix for what it reads that name FROM — bh-8g6cj) rather than taking them as
    flags.

    MEASURED for bh-oa225, and NOT what `_reinit_shared_server` above needs: unlike `bd init
    --shared-server`, `bd bootstrap` (activated via the same env var) does NOT auto-start bd's
    own managed shared dolt server when one isn't already reachable at the configured host/port
    — it fails outright (`dial tcp 127.0.0.1:<port>: connect: connection refused`), never falling
    back to spawning one the way `bd init --shared-server --reinit-local` demonstrably does.

    bh-l90xk revises what follows from that: this used to run `bd dolt start --global`
    UNCONDITIONALLY first, reasoning it was "idempotent and cheap against an already-running
    server" — true only when bd itself started that server. Measured on a real fleet host: the
    shared server was already up (started outside bd's own bookkeeping), `bd dolt start
    --global` returned `rc=1` anyway ("port 3308 is busy but cannot identify the process" —
    bh-hqmcl's territory), and that aborted every migration before bootstrap was ever reached
    even though the server it needed was reachable the whole time. `dolt_health.probe_shared_
    server()` (a real endpoint connect, no subprocess, bh-areg.3) answers "is anything already
    listening" cheaply and side-effect-free — probe FIRST, and only attempt a start when nothing
    answers. `port_busy_unattributable` on the returned :class:`MechanismOutcome` flags the
    bh-hqmcl condition distinctly from an ordinary start failure when a start WAS attempted and
    still failed that way."""
    env = {SHARED_SERVER_ENV_VAR: "1"}
    if not dolt_health.probe_shared_server().reachable:
        started = _bd(
            ["dolt", "start", "--global"], hive_dir, actor=actor, timeout=BD_TIMEOUT, env=env
        )
        if started.returncode:
            return MechanismOutcome(
                result=started,
                command_label="bd dolt start --global",
                port_busy_unattributable=_is_port_busy_unattributable(started),
            )
    result = _bd(
        ["bootstrap", "--non-interactive"], hive_dir, actor=actor, timeout=BD_TIMEOUT, env=env
    )
    return MechanismOutcome(result=result, command_label="bd bootstrap")


def select_mechanism(hive_dir: Path) -> str:
    """ "bootstrap" when `hive_dir`'s git `origin` already carries `refs/dolt/data` (bootstrap's
    PRECONDITION, never its blocker), else "reinit" — the ONE decision both the real migrate step
    and `--dry-run`'s preview make it on, via the SAME probe (:func:`origin_has_dolt_data`), so
    preview and live run can never diverge from each other."""
    return "bootstrap" if origin_has_dolt_data(hive_dir) else "reinit"


def mechanism_blocker(hive_dir: Path, mechanism: str) -> str | None:
    """`None` when `mechanism` is expected to succeed against `hive_dir`; otherwise the reason
    `bd` itself would refuse it — PROVEN on this fleet (bh-oa225's own bead): `bd init
    --reinit-local` refuses outright whenever the remote already carries `refs/dolt/data`, and
    every hive that has ever pushed bead state has it.

    :func:`select_mechanism` already routes around this by construction (it never returns
    "reinit" when `origin_has_dolt_data` is true), so under normal operation this can only ever
    return `None` — this is the SAME evidenced-precondition check as a defensive backstop, so a
    future regression back to "always reinit" is caught here rather than resurfacing as a dry-run
    that claims `would-migrate` for an operation proven to always fail (the exact defect this
    bead exists to fix), mirroring `detect_target_collisions`/`echo_collisions`'s pre-flight
    shape (bh-g5ujg) — a real, evidenced blocker, checked and reported before `--confirm`, never
    merely hoped past."""
    if mechanism == "reinit" and origin_has_dolt_data(hive_dir):
        return (
            "remote already has Dolt history (refs/dolt/data) — `bd init --reinit-local` "
            "refuses this (bh-oa225); mechanism selection should have chosen bootstrap here"
        )
    return None


def _fix_metadata_dolt_mode(hive_dir: Path, mode: str = "server") -> None:
    """Constraint 1: persist `dolt_mode` into `.beads/metadata.json` ITSELF — `bd init
    --shared-server --reinit-local` does not do this on its own (measured against a real bd
    binary; see the module docstring and the integration test), leaving the exact drift this
    migration exists to close if skipped."""
    path = _metadata_path(hive_dir)
    data = _read_metadata(hive_dir)
    data["dolt_mode"] = mode
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _read_dolt_database(hive_dir: Path) -> str:
    """The `dolt_database` value currently in `hive_dir`'s metadata.json — bd's OWN field
    (distinct from this module's additive `dolt_server_database` key, `store_locator
    .SERVER_DATABASE_KEY`), and, measured directly (bh-8g6cj), the ONE name bd itself resolves
    BOTH `bd bootstrap`'s clone target AND, once migrated, the project's ongoing server
    connection through — `bd bootstrap` has no working `--database` override in shared-server
    mode (the global `--database` flag is documented, and confirmed, "proxied-server mode
    only"; passing it here is silently ignored)."""
    return str(_read_metadata(hive_dir).get("dolt_database") or "")


def _set_dolt_database(hive_dir: Path, name: str) -> None:
    """Write `dolt_database` into metadata.json, preserving every other key — the write-side
    counterpart to :func:`_read_dolt_database`, used by :func:`migrate_hive` to repoint bootstrap
    at this module's own collision-free `db_name` and, on a bootstrap that doesn't actually
    migrate, to put the original value straight back (bh-8g6cj)."""
    path = _metadata_path(hive_dir)
    data = _read_metadata(hive_dir)
    data["dolt_database"] = name
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


def _ensure_pre_migrate_gitignore(hive_dir: Path) -> bool:
    """Add :data:`PRE_MIGRATE_GITIGNORE_PATTERN` to `.beads/.gitignore` if it's git-TRACKED and
    doesn't already carry it. A no-op (False) when there's no `.beads/.gitignore` file, or when
    it isn't tracked (a zero-footprint hive already excludes ALL of `.beads/` via
    `.git/info/exclude`, so there's nothing to fix — bh-xsv3 is a furnished-hive-only bug), or
    when the pattern is already present (idempotent — safe to call on every migrate). Inserted
    right after the existing `embeddeddolt/` line when found (keeps the Dolt-database patterns
    grouped), else appended at EOF; never before it, never a negation (that file's own footer
    warns against negation specifically, not against adding patterns after its comment block).
    Returns True iff it wrote."""
    gi = hive_dir / ".beads" / ".gitignore"
    if not gi.is_file():
        return False
    tracked = run(
        ["git", "ls-files", "--error-unmatch", ".beads/.gitignore"],
        cwd=str(hive_dir),
        check=False,
        capture=True,
        timeout=GIT_TIMEOUT,
    )
    if getattr(tracked, "returncode", 1) != 0:
        return False
    lines = gi.read_text().splitlines()
    if PRE_MIGRATE_GITIGNORE_PATTERN in lines:
        return False
    anchor = f"{store_locator.EMBEDDED_STORE_NAME}/"
    insert_at = (lines.index(anchor) + 1) if anchor in lines else len(lines)
    lines.insert(insert_at, PRE_MIGRATE_GITIGNORE_PATTERN)
    gi.write_text("\n".join(lines) + "\n")
    return True


def _retire_embedded_store(hive_dir: Path, *, keep: bool) -> tuple[str | None, str | None]:
    """Dispose of the original embedded store once the migration has VERIFIED. Returns
    ``(kept_path, finding)``.

    Always renames first (hq_restore.py's own `_apply_tar` precedent: move aside, never delete
    in place), which is also what keeps `store_locator.has_embedded_store()` correct for every
    other consumer — hq.py's own backup dispatch included — once this hive is no longer embedded.

    Then, by default, REMOVES the renamed copy (bh-5009a). Measured on nvhack's real migration,
    the moved-aside store is 46 MB of which `noms/` (27 MB — the actual data) is the same size
    as the migrate set's own `dolt-native/`, and 19 MB is `git-remote-cache/`, derived junk. So
    keeping it stores ~19 MB of waste per hive to preserve data the migrate set already holds in
    bd-restorable form. Its ONE unique property is an in-place rollback (rename it back), and
    that expires the moment `verify_migration` passes — which is the only point this is called.

    `keep=True` (`--keep-pre-migrate`) buys that rollback window back. Only then is the
    `.beads/.gitignore` pattern needed, because only then is there an untracked directory to
    cover: on a furnished hive (tracked `.beads/`) it would otherwise surface as hundreds of MB
    of untracked files, one `git add -A` away from being committed (bh-xsv3). The pattern is
    written but deliberately NOT committed — a migration has no business authoring commits in
    the operator's repo; it reports and lets them decide."""
    store = store_locator.embedded_store_dir(hive_dir)
    if not store.is_dir():
        return None, None
    at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    aside = store.with_name(f"{store.name}.pre-migrate-{at}")
    store.rename(aside)
    if not keep:
        shutil.rmtree(aside, ignore_errors=True)
        return None, None
    finding = (
        f"kept the pre-migrate store at {aside} for in-place rollback (rename it back) — "
        "remove it with `bh backup reclaim --root migrate --confirm` once you're satisfied"
    )
    if _ensure_pre_migrate_gitignore(hive_dir):
        finding += (
            f"; added {PRE_MIGRATE_GITIGNORE_PATTERN!r} to .beads/.gitignore so it stays out "
            "of `git status` — commit or revert that line yourself"
        )
    return str(aside), finding


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
    # no-store: `.beads/` exists but holds no database — nothing to move (bh-g5ujg)
    # blocked: `--dry-run` (or a defensive re-check in a real run) caught a mechanism that would
    # be refused — see `mechanism_blocker` (bh-oa225).
    # status values: migrated|already-migrated|would-migrate|no-store|skipped|failed|blocked
    status: str = "pending"
    # bootstrap|reinit — which mechanism `select_mechanism` picked for this hive (bh-oa225); set
    # for every embedded hive this reaches a migrate decision for, dry-run included.
    mechanism: str = ""
    server_database: str = ""
    detail: str = ""
    backup_plan: BackupPlan | None = None
    pre_issue_count: int = -1
    post_issue_count: int = -1
    schema_version: str = ""
    dolt_mode: str = ""
    size_bytes: int = 0
    target_path: str = ""
    findings: list[str] = field(default_factory=list)
    # Where this run's verified pre-migration backup set landed, and the moved-aside embedded
    # store IF `--keep-pre-migrate` kept one (bh-5009a — empty on the default path, which
    # removes it once `verify_migration` passes).
    backup_dir: str = ""
    kept_pre_migrate: str = ""


def _effective_prefix(hive_dir: Path, entry: dict) -> str:
    data = bd_mod.json(["config", "get", "issue_prefix"], hive_dir)
    if isinstance(data, dict) and data.get("value"):
        return str(data["value"])
    return str(entry.get("prefix") or entry.get("repo") or "")


def migrate_hive(
    entry: dict,
    cfg: dict,
    *,
    dry_run: bool = False,
    actor: str = "",
    keep_pre_migrate: bool = False,
) -> HiveMigrationResult:
    """One hive's full lifecycle: back up (VERIFIED before anything destructive) -> migrate ->
    verify -> report. Idempotent — a hive already off embedded is a no-op (but still heals a
    partially-applied prior run: `backup.enabled`/`dolt.shared-server` get re-asserted, cheap and
    safe either way).

    `keep_pre_migrate` leaves the moved-aside embedded store on disk for an in-place rollback
    instead of removing it once verification passes — see :func:`_retire_embedded_store`."""
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
        # bh-8g6cj: `dolt_mode` flips to "server" a few steps BEFORE the original embedded
        # store gets moved aside (the last step of a real migration, unchanged — see the module
        # docstring). A hive whose mode already says "server" but whose ORIGINAL, un-renamed
        # `embeddeddolt/` is still sitting on disk never reached that last step — the signature
        # of a migration INTERRUPTED partway through (backup_restore/verify never completed),
        # not a clean, finished one. Silently declaring "already-migrated" here would mistake
        # that for a fresh, done hive and skip straight to config-healing below, never surfacing
        # that there is still real work left — exactly the trap the bead's own design section
        # warns against.
        if mode == "server" and store_locator.has_embedded_store(hive_dir):
            result.status = "failed"
            result.dolt_mode = mode
            result.detail = (
                "dolt_mode is already 'server' but the original embeddeddolt/ store is still "
                "on disk (never moved aside) — a prior migration attempt was interrupted before "
                "completing; re-run migrate-storage to finish it rather than treating this hive "
                "as already migrated"
            )
            return result
        result.status = "already-migrated"
        result.dolt_mode = mode or "unknown"
        if not dry_run:
            _persist_backup_enabled(hive_dir, actor)
            _persist_shared_server_config(hive_dir, actor)
            # A hive that migrated under a pre-bh-5009a build left its moved-aside store on
            # disk. REPORT it rather than removing it here: this branch is the idempotent
            # no-op path, and deleting hundreds of MB as a side effect of a re-run nobody
            # expected to change anything is the wrong place for that decision. `bh backup
            # usage` lists it too, and `reclaim --root migrate --confirm` is the deliberate act.
            pattern = f"{store_locator.EMBEDDED_STORE_NAME}.pre-migrate-*"
            leftovers = sorted(p for p in (hive_dir / ".beads").glob(pattern) if p.is_dir())
            for path in leftovers:
                result.findings.append(
                    f"leftover pre-migrate store from an earlier migration: {path} "
                    f"({_dir_size(path):,}B) — superseded by this hive's backup set; remove it "
                    "with `bh backup reclaim --root migrate --confirm`"
                )
        return result

    embedded_dir = store_locator.embedded_store_dir(hive_dir)
    result.size_bytes = _dir_size(embedded_dir)
    # The SERVER name, not the embedded directory name (bh-g5ujg): `dolt_database` is `beads` for
    # almost every hive, and on a shared server that namespace is shared, so it collides.
    db_name = store_locator.server_database(hive_dir, prefix)
    result.server_database = db_name
    result.target_path = str(shared_server_target_dir(db_name))

    if result.size_bytes == 0:
        # `.beads/` present but no database under it — the "registered but never materialized"
        # shape (a clone whose `.beads/` came from git carries config.yaml + metadata.json with
        # no store). There is nothing to move, and minting an empty database here would shadow a
        # later real one, so this is NOT `would-migrate`.
        result.status = "no-store"
        result.detail = "no database under .beads/embeddeddolt/ — nothing to migrate"
        return result

    # Mechanism SELECTION, not assumption (bh-oa225): bootstrap-from-origin when the remote
    # already has `refs/dolt/data` (bootstrap's precondition), reinit-in-place otherwise — the
    # SAME decision `--dry-run` previews below, so a preview can never promise a mechanism the
    # real run wouldn't also pick.
    mechanism = select_mechanism(hive_dir)
    result.mechanism = mechanism
    blocker = mechanism_blocker(hive_dir, mechanism)
    if blocker:
        result.status = "blocked"
        result.detail = blocker
        return result

    if dry_run:
        result.status = "would-migrate"
        return result

    try:
        with hive_migration_lock(cfg, hive_id):
            from . import backup as backup_mod

            pre_count = _issue_count(hive_dir)
            slug = backup_mod.entry_slug(entry)
            backup_dir = backup_mod.migrate_set_dir(slug, cfg)
            plan = take_backup(hive_dir, backup_dir, cfg, prefix, dry_run=False, hive=slug)
            result.backup_plan = plan
            result.backup_dir = str(backup_dir)
            if not plan.ok:
                result.status = "failed"
                result.detail = "backup could not be verified — refusing to migrate"
                return result

            if mechanism == "bootstrap":
                # bh-8g6cj: `bd bootstrap` targets `.beads/metadata.json`'s own `dolt_database`
                # field for its server-side clone destination, NOT this module's collision-free
                # `db_name` (it has no working `--database` override in shared-server mode —
                # see :func:`_read_dolt_database`). Repoint it to `db_name` right before calling
                # bootstrap; if bootstrap doesn't actually migrate (declines/errors — original_db
                # is only ever restored in that case), put the ORIGINAL value straight back so
                # the still-embedded hive stays exactly as readable as it was.
                original_db = _read_dolt_database(hive_dir)
                if original_db and original_db != db_name:
                    _set_dolt_database(hive_dir, db_name)
                outcome = _bootstrap_shared_server(hive_dir, actor)
                if outcome.result.returncode and original_db and original_db != db_name:
                    _set_dolt_database(hive_dir, original_db)
            else:
                outcome = MechanismOutcome(
                    result=_reinit_shared_server(hive_dir, prefix, db_name, actor),
                    command_label="bd init --reinit-local",
                )
            if outcome.result.returncode:
                result.status = "failed"
                reason = _significant_err_line(outcome.result)
                detail = f"{outcome.command_label} refused: {reason}"
                if outcome.port_busy_unattributable:
                    detail += (
                        " — a dolt server appears to already be running on that port outside "
                        "bd's own bookkeeping (see bh-hqmcl); this is not an ordinary migration "
                        "failure"
                    )
                result.detail = detail
                return result

            _fix_metadata_dolt_mode(hive_dir, "server")
            # Record the server name rather than leaving it derivable (bh-g5ujg). A derivation is
            # not a record: re-deriving on a later run is how an already-migrated hive gets
            # "corrected" onto a name its store isn't under.
            store_locator.ensure_server_database_persisted(hive_dir, db_name)
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

            # Verification has passed, so the in-place rollback the original store offered is
            # now redundant with the backup set taken above — retire it (bh-5009a). Strictly
            # after `verify_migration`, never before: until the migrated store is proven
            # readable AND complete, the original is the only thing standing between an
            # operator and a lost corpus.
            kept, finding = _retire_embedded_store(hive_dir, keep=keep_pre_migrate)
            result.kept_pre_migrate = kept or ""
            if finding:
                result.findings.append(finding)

            # Retention, right after a new set is written and verified — the same posture root 1
            # takes (`hq._prune_hq_backups_best_effort`), and for the same reason: `bh` owns this
            # write path end to end, so the operator never has to remember to reclaim.
            pruned = backup_mod.prune_migrate_backups(slug, cfg)
            if pruned.removed:
                result.findings.append(
                    f"pruned {len(pruned.removed)} older backup set(s) past "
                    f"backup.migrate_keep ({pruned.reclaimed_bytes:,}B reclaimed)"
                )
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


@dataclass
class TargetPlan:
    """One hive's resolved shared-server destination, computed WITHOUT touching anything.

    Deliberately carries no size/would-migrate field: this runs on EVERY invocation, and sizing a
    store means walking it (`_dir_size` rglobs the whole tree — ~1.5GB across this fleet). The
    collision question is about NAMES, and a name costs one metadata read."""

    hive_id: str
    database: str
    target_path: str


def plan_targets(cfg: dict) -> list[TargetPlan]:
    """Every registered hive's resolved shared-server target — the input to
    :func:`detect_target_collisions`.

    Includes hives that are ALREADY on the server: an un-migrated hive resolving onto a database
    an already-migrated one owns is exactly as destructive as two un-migrated ones colliding, so
    the occupied names have to be in the map too."""
    plans: list[TargetPlan] = []
    for entry in fleet_order(cfg):
        hive_dir = registry.hive_dir(entry)
        if not (hive_dir / ".beads").is_dir():
            continue
        db = store_locator.server_database(hive_dir, _effective_prefix(hive_dir, entry))
        plans.append(
            TargetPlan(
                hive_id=registry.hive_key(entry),
                database=db,
                target_path=str(shared_server_target_dir(db)),
            )
        )
    return plans


def detect_target_collisions(cfg: dict) -> dict[str, list[str]]:
    """Targets claimed by more than one hive, as ``{target_path: [hive_id, ...]}`` (empty when
    the plan is safe).

    A PRE-FLIGHT INVARIANT, not a per-hive check: hive N's migration is only safe in the context
    of all the others, so this is resolved across the whole fleet before anything is migrated —
    including for a single-hive run, where migrating one hive onto a name another already owns is
    just as destructive."""
    by_target: dict[str, list[str]] = {}
    for plan in plan_targets(cfg):
        by_target.setdefault(plan.target_path, []).append(plan.hive_id)
    return {target: ids for target, ids in sorted(by_target.items()) if len(ids) > 1}


def echo_collisions(collisions: dict[str, list[str]]) -> None:
    """Render collisions as the blockers they are. Deliberately loud and on stderr: the failure
    this guards against is an operator reading a cheerful summary and proceeding to `--confirm`."""
    typer.echo(
        "✗ shared-server target collision — refusing to migrate (bh-g5ujg).\n"
        "  These hives resolve to the SAME database; migrating would merge separate bead "
        "corpora into one store:",
        err=True,
    )
    for target, ids in collisions.items():
        typer.echo(f"    {target}", err=True)
        for hive_id in ids:
            typer.echo(f"      <- {hive_id}", err=True)
    typer.echo(
        "  Each hive needs a distinct `dolt_server_database` in its .beads/metadata.json "
        "(normally the sanitized hive prefix).",
        err=True,
    )


def migrate_fleet(
    cfg: dict, *, dry_run: bool = False, actor: str = "", keep_pre_migrate: bool = False
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
        result = migrate_hive(
            entry, cfg, dry_run=dry_run, actor=actor, keep_pre_migrate=keep_pre_migrate
        )
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
        "no-store": "⚠",
        "skipped": "⚠",
        "failed": "✗",
        "blocked": "✗",
    }.get(r.status, "?")
    typer.echo(f"{icon} {r.hive_id}  [{r.status}]" + (f" — {r.detail}" if r.detail else ""))
    if r.status == "would-migrate":
        typer.echo(
            f"    mechanism: {r.mechanism}  size: {r.size_bytes:,}B  database: {r.server_database}"
            f"  target: {r.target_path}"
        )
    if r.backup_plan is not None:
        echo_backup_plan(r.backup_plan)
    if r.status == "migrated":
        typer.echo(
            f"    mechanism: {r.mechanism}  issues: {r.pre_issue_count} -> {r.post_issue_count}  "
            f"schema_version: {r.schema_version}  dolt_mode: {r.dolt_mode}"
        )
        if r.backup_dir:
            typer.echo(f"    backup set: {r.backup_dir}  (`bh backup usage` lists it)")
    for f in r.findings:
        typer.echo(f"    finding: {f}")


def migrate(
    hive_id: str = "",
    *,
    dry_run: bool = False,
    confirm: bool = False,
    keep_pre_migrate: bool = False,
) -> None:
    """`bh hive migrate-storage [HIVE_ID]`: one hive (HIVE_ID given) or the whole fleet (empty —
    HQ last), backup -> migrate -> verify -> report."""
    from .identity import resolve_actor

    cfg = config.load()

    # Pre-flight, BEFORE the --confirm gate and before any per-hive work, and enforced on
    # --dry-run too: a preview that renders a colliding plan without flagging it is the defect
    # this bead exists for (bh-g5ujg). Non-zero exit so a scripted `--dry-run && --confirm`
    # cannot walk straight into it.
    collisions = detect_target_collisions(cfg)
    if collisions:
        echo_collisions(collisions)
        raise typer.Exit(1)

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
        result = migrate_hive(
            entry, cfg, dry_run=dry_run, actor=actor, keep_pre_migrate=keep_pre_migrate
        )
        _echo_result(result)
        if result.status in ("failed", "blocked"):
            raise typer.Exit(1)
        return

    actor = resolve_actor("", "")
    results = migrate_fleet(cfg, dry_run=dry_run, actor=actor, keep_pre_migrate=keep_pre_migrate)
    for r in results:
        _echo_result(r)
    failed = [r for r in results if r.status in ("failed", "blocked")]
    migrated = sum(1 for r in results if r.status in ("migrated", "already-migrated"))
    no_store = sum(1 for r in results if r.status == "no-store")
    typer.echo(
        f"\n{migrated} migrated/up-to-date, {len(failed)} failed"
        + (f", {no_store} with no store" if no_store else "")
        + f", {len(results)} total"
        + (" (dry-run — nothing changed)" if dry_run else "")
    )
    if failed:
        raise typer.Exit(1)
