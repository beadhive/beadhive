"""backup.py — boundary + retention for bh's backup roots (bh-cmqp.2, bh-5009a).

See ``docs/design/backup-retention-boundary-adr.md`` for the design; this module implements
it, one function set per root:

- **HQ pre-push backup** (:func:`hq_root`) — automatic keep-N prune, called right after
  ``hq._take_backup`` verifies a new one (``hq._wire_remote``). ``bh`` owns this write path
  end to end, so pruning can run synchronously with zero operator involvement.
- **bd's own per-hive Dolt-native backup** (``<hive>/.beads/backup/``) — operator-invoked
  rotate + keep-N generations (``bh backup reclaim --root hive``), using only bd's own
  sanctioned lifecycle verbs (``bd backup remove``/``init``/``sync``) plus a plain filesystem
  rename. ``bh`` doesn't own bd's sync timer, so this never runs automatically.
- **The JSONL mirror** (:func:`mirrors_root`, ``bh backup export``) — a fixed per-hive default
  path, cwd-INDEPENDENT (the bh-mw97 failure class), overwritten each run. No pruning code:
  keep-1 is the policy, enforced simply by never accumulating more than one file at the
  default path.
- **The pre-migration backup** (:func:`migrate_root`, ``bh hive migrate-storage``) — the
  verified JSONL + Dolt-native pair taken before a storage-mode migration touches anything.
  Automatic keep-N prune per hive, same posture as HQ's: ``bh`` owns the write path.

bh-5009a: all four live under ONE root (``$BH_HOME/backups/``), addressed
``<category>/<provider>/<org>/<repo>/<instant>/`` with ONE time format
(:data:`STAMP_FORMAT`) — the migrate root used to sit at ``~/.beadhive/storage-migrate-
backups/`` under a FLATTENED, sanitized hive key with no retention policy at all, and HQ's at
a hardcoded ``~/.beadhive/hq-backups/`` that no ``$BH_HOME`` override could move. Category
first (not provider first) so ``hq``/``mirrors``/``migrate`` can never be mistaken for a
provider name. Every dated set carries a :data:`MANIFEST_NAME` recording what it was taken
before and whether it VERIFIED, so ``bh backup usage`` reads a fact instead of stat-ing
directories and inferring one.

The pre-relocation locations are still READ (:func:`legacy_roots`) so an operator who has not
run ``bh backup migrate-layout`` yet keeps a working ``bh hq restore`` — a silent relocation
that stranded the only pre-push backup would be the exact failure this whole contract exists
to prevent.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config
from .bd import err_line
from .run import run

BD_TIMEOUT = 120.0  # seconds — matches hq.py's own bd-call budget

# ONE time format across every root (bh-5009a): compact ISO-8601 UTC, lexically sortable, so
# retention is a sort-and-slice no matter which root it runs against. The legacy HQ root's
# date-only `%Y-%m-%d` names are a strict PREFIX of this, so the two still interleave
# correctly under the same plain reverse sort while both roots are being read.
STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"
MANIFEST_NAME = "manifest.json"

_MIRROR_FILENAME = "issues.jsonl"
_HIVE_BACKUP_DIRNAME = "backup"  # <hive>/.beads/backup — bd's own destination
_CATEGORIES = ("hq", "mirrors", "migrate")
# `storage_migrate._retire_embedded_store`'s renamed original store. Not written by this
# module, but it IS a migration artifact on the operator's disk, so `bh backup usage` reports
# it and `bh backup reclaim --root migrate` can remove it (bh-5009a).
PRE_MIGRATE_GLOB = "embeddeddolt.pre-migrate-*"


def stamp(now: datetime | None = None) -> str:
    """The one backup-instant string every root names its dated sets with."""
    return (now or datetime.now(UTC)).strftime(STAMP_FORMAT)


# ---- the one root, and the four categories under it (bh-5009a) ------------------


def root(cfg=None) -> Path:
    """``$BH_HOME/backups`` — every bh-owned backup artifact lives under here.

    Anchored on :func:`config.home`, not a hardcoded ``~/.beadhive``: ``BH_HOME``/``WS_HOME``
    is the established "everything bh owns on a machine" override (``config.py``'s own module
    docstring), and the pre-bh-5009a ``~/.beadhive/hq-backups`` was hardcoded and therefore
    the one root an operator could NOT relocate."""
    return config.home() / "backups"


def hq_root(cfg=None) -> Path:
    """Root 1 — HQ's pre-push backup sets. ``hq`` (a subject) rather than ``prepush`` (a kind)
    reads inconsistent next to ``mirrors``/``migrate``, and HQ will legitimately appear in two
    places once HQ itself migrates (``hq/<instant>/`` for pre-push, ``migrate/local/factory/
    hq/<instant>/`` for its storage migration). Deliberate: ``manifest.json``'s ``kind`` field
    disambiguates the two at zero path cost, so don't "fix" this to ``prepush/``."""
    return root(cfg) / "hq"


def mirrors_root(cfg=None) -> Path:
    """Root 3 — ``bh backup export``'s JSONL interchange mirrors. PLURAL: there is one mirror
    per hive, so the directory holds many."""
    return root(cfg) / "mirrors"


def migrate_root(cfg=None) -> Path:
    """Root 4 — ``bh hive migrate-storage``'s verified pre-migration backup sets."""
    return root(cfg) / "migrate"


def _backed_up_entries(cfg) -> list:
    """Every registered entry that can hold backup artifacts — ordinary hives PLUS the Factory
    HQ singleton.

    ``registry.hives`` deliberately excludes HQ (it is a singleton, not a hive), but for THIS
    module's purposes HQ is just another store: it has its own ``.beads/backup``, it migrates
    storage mode like anything else, and it therefore writes a ``migrate/`` set under
    ``local/factory/hq``. Leaving it out sent HQ's own 68.7 MB pre-migration backup to
    ``migrate/_unresolved/`` on a real host — the same "HQ is a hive too, last in line"
    reasoning ``storage_migrate.fleet_order`` already encodes."""
    from . import registry

    hq = registry.hive_of_kind(cfg, registry.HQ_KIND)
    return [*registry.hives(cfg), *([hq] if hq is not None else [])]


def entry_slug(entry) -> str:
    """``provider/org/repo`` for a registry entry — the SAME triplet ``$GIT_WORKSPACE`` and
    ``registry.hive_key`` use, needing no sanitization and introducing no second addressing
    scheme (the pre-bh-5009a migrate root flattened it to ``github-briancripe-nvidia-
    hackathon``). See :func:`write_manifest` for the rename hazard this carries."""
    return f"{entry['provider']}/{entry['org']}/{entry['repo']}"


def _dated_dirs(base: Path) -> list[Path]:
    """Every dated set directly under ``base``, NEWEST FIRST — :data:`STAMP_FORMAT` sorts
    chronologically as a plain string, so this needs no ``stat()`` and no clock read."""
    if not base.is_dir():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)


def _hive_dirs(base: Path) -> list[Path]:
    """Every ``<provider>/<org>/<repo>`` leaf under a category root, sorted for determinism."""
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*/*/*") if p.is_dir())


# ---- manifest.json: what a dated set IS, recorded rather than inferred -----------


def _bh_version() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version("beadhive")
    except Exception:  # noqa: BLE001 — a version lookup must never fail a backup
        return "unknown"


def write_manifest(
    dest: Path,
    *,
    kind: str,
    hive: str = "",
    prefix: str = "",
    verified: bool = False,
    artifacts: dict[str, int] | None = None,
    taken_at: datetime | None = None,
    **extra,
) -> Path:
    """Record what this dated set is, next to the set itself (bh-5009a).

    Without one, nothing on disk says whether a backup VERIFIED, what it was taken BEFORE, or
    which ``bh`` wrote it — ``bh backup usage`` has to stat directories and infer, and a
    restore has to trust the operator's memory. With one, usage is a cheap read and retention
    can prefer keeping verified sets over unverified ones.

    Records BOTH ``hive`` (the triplet) and ``prefix`` on purpose: a triplet MOVES when a
    hive's provider/org/repo changes (bh-l9h56 / bh-484xb are literally about that) and a
    prefix moves when the prefix changes, so neither addressing scheme alone is stable. Both
    written down at least makes an orphaned set self-identifying rather than anonymous.

    Best-effort by construction — a manifest that cannot be written must never fail the backup
    it describes; the caller gets ``dest/manifest.json`` back either way."""
    path = Path(dest) / MANIFEST_NAME
    payload = {
        "kind": kind,
        "hive": hive,
        "prefix": prefix,
        "taken_at": (taken_at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bh_version": _bh_version(),
        "verified": bool(verified),
        "artifacts": artifacts or {},
        **{k: v for k, v in extra.items() if v not in ("", None, -1)},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass
    return path


def read_manifest(dest: Path) -> dict:
    """A dated set's manifest, or ``{}`` for a set written before bh-5009a (or a corrupt one).
    Callers must treat an empty manifest as "unknown", never as "unverified" — every set
    already on disk predates this file."""
    try:
        data = json.loads((Path(dest) / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _hq_backup_dirs(cfg=None) -> list[Path]:
    """Every dated directory under :func:`hq_root` AND the pre-bh-5009a ``~/.beadhive/hq-
    backups/``, NEWEST FIRST. Unlike ``hq_restore.list_backups`` this counts every directory
    that exists on disk, restorable or not — an empty/partial one still occupies a slot worth
    pruning.

    Reading BOTH roots is what lets keep-N drain the legacy location on its own: an operator
    who never runs ``bh backup migrate-layout`` still converges, because a legacy set is just
    an old set and ages out of the newest-N window like any other. Sorted on ``name``, not the
    full path, so sets from two different roots interleave chronologically."""
    seen: dict[Path, Path] = {}
    for d in (*_dated_dirs(hq_root(cfg)), *_dated_dirs(legacy_hq_root(cfg))):
        # Keyed on the RESOLVED path: should the two roots ever coincide (an operator pointing
        # $BH_HOME at the legacy layout, a symlink), a duplicated entry would make keep-N count
        # the same set twice and then rmtree a directory it had already decided to keep.
        seen.setdefault(d.resolve(), d)
    return sorted(seen.values(), key=lambda p: p.name, reverse=True)


def hq_backup_usage(cfg=None) -> tuple[Path, int, int]:
    """``(root, total size bytes, dated-set count)`` for the CURRENT HQ pre-push backup root.

    Current root only, deliberately — anything still in the legacy location gets its own
    :func:`legacy_roots` row in ``bh backup usage``, and counting it in both places would
    inflate the reported total by exactly the bytes an operator is being told to relocate.
    Retention (:func:`_hq_backup_dirs`) still spans both; that is a different question."""
    cfg = cfg if cfg is not None else config.load()
    dirs = _dated_dirs(hq_root(cfg))
    return hq_root(cfg), sum(_dir_size(d) for d in dirs), len(dirs)


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
    under :func:`mirrors_root`, independent of which subdirectory of the hive the operator was
    standing in (bh-cmqp.2). A single fixed file is overwritten each run; see the ADR for why
    this mechanism intentionally retains no history.

    bh-5009a moved this one level down (``backups/<triplet>/`` -> ``backups/mirrors/
    <triplet>/``) so a provider name can never sit at the same level as a category name; the
    old location is still reported by :func:`legacy_roots` until relocated."""
    return mirrors_root(cfg) / _hive_slug(cfg, cwd)


def mirror_usage(cfg=None, cwd=None) -> tuple[Path, int]:
    """``(path, size bytes)`` for the current hive's JSONL mirror export file."""
    out = mirror_root(cfg, cwd) / _MIRROR_FILENAME
    return out, (out.stat().st_size if out.is_file() else 0)


# ---- root 4: the pre-migration backup (bh-5009a) ---------------------------------


def migrate_hive_root(slug: str, cfg=None) -> Path:
    """Where one hive's pre-migration backup sets live: ``backups/migrate/<provider>/<org>/
    <repo>/``."""
    return migrate_root(cfg) / slug


def migrate_set_dir(slug: str, cfg=None, *, at: str = "") -> Path:
    """A specific instant's set under :func:`migrate_hive_root`."""
    return migrate_hive_root(slug, cfg) / (at or stamp())


def migrate_usage(cfg=None) -> tuple[Path, int, int, int]:
    """``(root, total size bytes, dated-set count, hive count)`` across every hive's
    pre-migration backup sets in the CURRENT root — the legacy root gets its own
    :func:`legacy_roots` row, for the same reason :func:`hq_backup_usage` excludes it."""
    cfg = cfg if cfg is not None else config.load()
    hives = _hive_dirs(migrate_root(cfg))
    sets = [d for h in hives for d in _dated_dirs(h)]
    return migrate_root(cfg), sum(_dir_size(d) for d in sets), len(sets), len(hives)


def prune_migrate_backups(
    slug: str = "", cfg=None, *, keep: int | None = None, dry_run: bool = False
) -> PruneResult:
    """Keep the newest ``keep`` (default ``config.backup_migrate_keep``) pre-migration sets PER
    HIVE; rmtree the rest. Runs automatically right after a migration writes and verifies a new
    one (``storage_migrate.migrate_hive``) — ``bh`` owns this write path end to end, the same
    reason root 1's prune is automatic — and on demand via ``bh backup reclaim --root migrate``.

    Per hive, not across the root: a fleet migration would otherwise let one hive's three sets
    evict another hive's only one. ``slug=""`` sweeps every hive; the legacy flattened root is
    swept too so it drains on its own."""
    cfg = cfg if cfg is not None else config.load()
    resolved_keep = config.backup_migrate_keep(cfg) if keep is None else keep
    hives = (
        [migrate_hive_root(slug, cfg)]
        if slug
        else [*_hive_dirs(migrate_root(cfg)), *_dated_dirs(legacy_migrate_root(cfg))]
    )
    merged = PruneResult(dry_run=dry_run)
    for hive in hives:
        one = _prune_keep_n(_dated_dirs(hive), resolved_keep, dry_run=dry_run)
        merged.removed.extend(f"{hive.name}/{n}" for n in one.removed)
        merged.reclaimed_bytes += one.reclaimed_bytes
    return merged


def pre_migrate_stores(cfg=None) -> list[Path]:
    """Every ``<hive>/.beads/embeddeddolt.pre-migrate-<stamp>/`` still on disk across the fleet.

    The OTHER migration artifact, and the one that lands INSIDE the operator's repo: on a
    furnished hive (tracked ``.beads/``) it surfaces as hundreds of MB of untracked files, one
    ``git add -A`` away from being committed. ``storage_migrate`` now prunes it right after
    ``verify_migration`` passes, so this only finds one where an operator passed
    ``--keep-pre-migrate`` or migrated under a pre-bh-5009a build — which is exactly why it
    has to be findable rather than silently accumulating."""
    from . import registry

    cfg = cfg if cfg is not None else config.load()
    found: list[Path] = []
    for entry in _backed_up_entries(cfg):
        beads = registry.hive_dir(entry) / ".beads"
        if beads.is_dir():
            found.extend(sorted(p for p in beads.glob(PRE_MIGRATE_GLOB) if p.is_dir()))
    return found


def prune_pre_migrate_stores(cfg=None, *, dry_run: bool = False) -> PruneResult:
    """Remove every leftover pre-migrate store (:func:`pre_migrate_stores`). Its only unique
    value over the migrate set is an IN-PLACE rollback (rename it back), which expires the
    moment ``verify_migration`` passes — measured on a real migration, its ``noms/`` is the
    same size as the set's ``dolt-native/``, plus ~19 MB of derived transport cache, so
    archiving it would store waste to preserve data the set already holds in bd-restorable
    form."""
    result = PruneResult(dry_run=dry_run)
    for store in pre_migrate_stores(cfg):
        result.removed.append(str(store))
        result.reclaimed_bytes += _dir_size(store)
        if not dry_run:
            shutil.rmtree(store, ignore_errors=True)
    return result


# ---- root 2: bd's own per-hive Dolt-native backup --------------------------------


def hive_backup_dir(hive_dir: Path) -> Path:
    return Path(hive_dir) / ".beads" / _HIVE_BACKUP_DIRNAME


def hive_backup_usage(hive_dir: Path) -> int:
    """Current size (bytes) of a hive's ``.beads/backup/`` — bd's own live destination."""
    return _dir_size(hive_backup_dir(hive_dir))


_DOLT_BACKUP_JSON = "dolt-backup.json"  # bd's own record of its LIVE backup destination


def _backup_url_to_path(url: str) -> Path | None:
    """``file://`` URLs only — a DoltHub remote (``https://...``) or any other non-filesystem
    destination an operator configured on purpose is never something this module has business
    rewriting, so it maps to `None`, same as "no registration at all"."""
    if not url.startswith("file://"):
        return None
    return Path(url[len("file://") :])


def bd_backup_target(hive_dir) -> Path | None:
    """The filesystem path `hive_dir`'s `.beads/dolt-backup.json` currently names as bd's live
    backup destination (root #2's OWN bookkeeping of itself) — `None` when there's no
    registration, the file is unreadable, or it names a non-filesystem destination."""
    path = Path(hive_dir) / ".beads" / _DOLT_BACKUP_JSON
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _backup_url_to_path(str(data.get("backup_url") or ""))


def bd_backup_points_into_migrate_root(hive_dir, cfg=None) -> Path | None:
    """bh-ypfnu: `bd_backup_target(hive_dir)` when it sits inside a migrate root — root #4's
    CURRENT consolidated location (:func:`migrate_root`) or its pre-bh-5009a legacy one
    (:func:`legacy_migrate_root`) — else `None`.

    The ADR's boundary: root #4 is a point-in-time SNAPSHOT, root #2 (:func:`hive_backup_dir`)
    is bd's ongoing LIVE destination. A registration recorded here is always wrong, whether the
    snapshot it names still physically exists (measured on bh-infra: MIS-pointed, bd's activity
    timer would mutate the very set that exists to hold pre-migration state) or was since moved
    out from under it by an earlier, unhealed `migrate_layout` run (measured on nvhack:
    DANGLING, the directory is gone). The recorded path text does not change on its own when the
    directory moves — that IS the bug — so one prefix test against both roots catches both
    shapes identically, with no need to separately probe whether the target still exists."""
    cfg = cfg if cfg is not None else config.load()
    target = bd_backup_target(hive_dir)
    if target is None:
        return None
    for candidate_root in (migrate_root(cfg), legacy_migrate_root(cfg)):
        try:
            target.relative_to(candidate_root)
        except ValueError:
            continue
        return target
    return None


def repoint_bd_backup(hive_dir, cfg=None, *, actor: str = "") -> str:
    """Re-point bd's live backup destination back to root #2 (`<hive>/.beads/backup`) — the
    ADR's boundary, restored. `bd backup add` is idempotent for a new destination (engine.py's
    own comment: "bd removes+re-adds under the hood"), so this is safe to call unconditionally,
    whether or not a registration currently exists or where it currently points. Returns ``""``
    on success, else a short failure detail — never raises; a failure here is a backup-hygiene
    regression, not a corpus-safety one (the caller only ever reaches this after its own
    migration has independently verified)."""
    from . import engine as engine_mod

    cfg = cfg if cfg is not None else config.load()
    dest = hive_backup_dir(hive_dir)
    res = engine_mod.get_engine(cfg).backup(hive_dir, dest, actor=actor)
    if res.returncode:
        return f"bd backup add {dest} failed: {err_line(res)}"
    return ""


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


# ---- the pre-bh-5009a layout: still read, relocatable on demand -------------------


def legacy_hq_root(cfg=None) -> Path:
    """Root 1's pre-bh-5009a location. Hardcoded under ``~/.beadhive`` in the original — kept
    resolving through :func:`config.home` here so a host with ``$BH_HOME`` set still finds its
    own legacy sets rather than someone else's."""
    return config.home() / "hq-backups"


def legacy_migrate_root(cfg=None) -> Path:
    """Root 4's pre-bh-5009a location, keyed by a FLATTENED sanitized hive id
    (``github-briancripe-nvidia-hackathon``) instead of the ``<provider>/<org>/<repo>``
    triplet."""
    return config.home() / "storage-migrate-backups"


def legacy_mirror_dirs(cfg=None) -> list[Path]:
    """Root 3's pre-bh-5009a mirrors: ``backups/<provider>/…`` — i.e. anything directly under
    :func:`root` that ISN'T one of the three category names. Identified by exclusion rather
    than by a provider list on purpose: the ``_unmanaged/<name>`` fallback slug
    :func:`_hive_slug` writes for an unidentifiable repo is a legacy mirror too, and no
    provider list would ever contain it."""
    return [p for p in _dated_dirs(root(cfg)) if p.name not in _CATEGORIES]


@dataclass
class LegacyRoot:
    """One pre-bh-5009a location that still holds artifacts."""

    kind: str  # "hq" | "mirrors" | "migrate"
    path: Path
    size_bytes: int = 0


def legacy_roots(cfg=None) -> list[LegacyRoot]:
    """Every legacy location that still exists AND holds something. Empty once
    :func:`migrate_layout` has run (or on a host that never wrote one) — which is what makes
    it safe for ``bh backup usage`` to render these as a to-do rather than a permanent row."""
    cfg = cfg if cfg is not None else config.load()
    out: list[LegacyRoot] = []
    for kind, path in (("hq", legacy_hq_root(cfg)), ("migrate", legacy_migrate_root(cfg))):
        size = _dir_size(path)
        if path.is_dir() and size:
            out.append(LegacyRoot(kind=kind, path=path, size_bytes=size))
    mirrors = legacy_mirror_dirs(cfg)
    if mirrors:
        out.append(
            LegacyRoot(
                kind="mirrors",
                path=root(cfg),
                size_bytes=sum(_dir_size(d) for d in mirrors),
            )
        )
    return out


def _relocate(src: Path, dest: Path) -> str:
    """Move ``src`` to ``dest``, preferring a plain rename. Returns a short description of
    which mechanism was used.

    Within one filesystem a rename IS the whole operation — atomic, no copy, no disk headroom
    needed for a second copy of a 69 MB backup set. Across filesystems ``rename`` raises
    ``EXDEV``, which is DETECTED explicitly here rather than papered over by reaching for
    ``shutil.move`` unconditionally: the fallback copies, verifies the copy's size matches, and
    only then removes the source, so an interrupted cross-device relocation leaves the source
    intact instead of a half-written destination and nothing else."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dest)
        return "renamed"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    before = _dir_size(src)
    shutil.copytree(src, dest, dirs_exist_ok=True)
    if _dir_size(dest) != before:
        raise OSError(
            f"cross-filesystem copy of {src} -> {dest} did not reproduce {before}B — "
            "leaving the source in place"
        )
    shutil.rmtree(src)
    return "copied across filesystems (source verified, then removed)"


def _legacy_migrate_slug_map(cfg) -> dict[str, str]:
    """Sanitized legacy directory name -> ``<provider>/<org>/<repo>``, built from the registry.

    The flattening is not invertible on its own (``github-beadhive-beadhive-ui`` could split
    three ways), so the registry is the only honest source: sanitize each REGISTERED hive's key
    and match forward. A directory no registered hive claims is not guessed at — see
    :func:`migrate_layout`."""
    from . import registry
    from .storage_migrate import _sanitize_id

    return {_sanitize_id(registry.hive_key(e)): entry_slug(e) for e in _backed_up_entries(cfg)}


@dataclass
class LayoutMove:
    src: Path
    dest: Path
    kind: str
    size_bytes: int = 0
    how: str = ""
    error: str = ""


@dataclass
class LayoutMigration:
    dry_run: bool = False
    moves: list[LayoutMove] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(m.error for m in self.moves)

    @property
    def moved_bytes(self) -> int:
        return sum(m.size_bytes for m in self.moves if not m.error)


def migrate_layout(cfg=None, *, dry_run: bool = True, actor: str = "") -> LayoutMigration:
    """One-time relocation of every pre-bh-5009a artifact into the consolidated layout, PLUS
    (bh-ypfnu) healing any registered hive whose bd backup destination is left dangling or
    mis-pointed into a migrate root by the relocation — see the loop below.

    Not required for correctness — every read path already spans both locations — but it is
    what makes ``bh backup usage`` stop reporting a legacy row, and it re-keys the migrate root
    off the flattened sanitized id onto the triplet. Idempotent: a destination that already
    exists is left alone and reported, never merged over.

    A legacy migrate directory no registered hive claims lands under ``migrate/_unresolved/``
    rather than being deleted or guessed at — an orphan from a retired or renamed hive is still
    somebody's only pre-migration backup."""
    cfg = cfg if cfg is not None else config.load()
    out = LayoutMigration(dry_run=dry_run)

    def plan(src: Path, dest: Path, kind: str) -> None:
        move = LayoutMove(src=src, dest=dest, kind=kind, size_bytes=_dir_size(src))
        if dest.exists():
            move.error = f"destination already exists: {dest}"
        elif not dry_run:
            try:
                move.how = _relocate(src, dest)
            except OSError as exc:
                move.error = str(exc)
        out.moves.append(move)

    for d in _dated_dirs(legacy_hq_root(cfg)):
        plan(d, hq_root(cfg) / d.name, "hq")

    slugs = _legacy_migrate_slug_map(cfg)
    for hive_dir in _dated_dirs(legacy_migrate_root(cfg)):
        slug = slugs.get(hive_dir.name)
        if slug is None:
            slug = f"_unresolved/{hive_dir.name}"
            out.notes.append(
                f"{hive_dir.name}: no registered hive claims this legacy key — filed under "
                f"migrate/_unresolved/ rather than guessed at or discarded"
            )
        for d in _dated_dirs(hive_dir):
            plan(d, migrate_root(cfg) / slug / d.name, "migrate")

    for provider_dir in legacy_mirror_dirs(cfg):
        plan(provider_dir, mirrors_root(cfg) / provider_dir.name, "mirrors")

    # bh-ypfnu: heal every registered hive whose bd backup registration currently points INTO
    # a migrate root — the legacy one this very call may be emptying out above, the current
    # consolidated one, or (nvhack, measured on this host) a path an EARLIER `migrate_layout`
    # run already relocated out from under it before this fix existed, now dangling. The
    # recorded path text never follows a move on its own (that's the bug), so this check runs
    # independently of whether THIS run has anything left to relocate for that hive.
    from . import registry

    for entry in _backed_up_entries(cfg):
        hive_dir = registry.hive_dir(entry)
        if not hive_dir.is_dir():
            continue
        target = bd_backup_points_into_migrate_root(hive_dir, cfg)
        if target is None:
            continue
        move = LayoutMove(src=target, dest=hive_backup_dir(hive_dir), kind="backup-registration")
        if dry_run:
            move.how = (
                "would `bd backup add`/`bd backup sync` — bd's live backup destination is "
                "dangling/mis-pointed into a migrate set, not root #2"
            )
        else:
            err = repoint_bd_backup(hive_dir, cfg, actor=actor)
            if err:
                move.error = err
            else:
                move.how = "re-pointed bd's live backup destination to .beads/backup"
        out.moves.append(move)

    if not dry_run:
        # Only empty shells are removed — `rmdir` fails loudly on a directory that still holds
        # anything, which is the guard we want here rather than an `ignore_errors` rmtree that
        # would silently take unrelocated content with it.
        for leftover in (legacy_hq_root(cfg), legacy_migrate_root(cfg)):
            for d in sorted(leftover.rglob("*"), reverse=True) + [leftover]:
                try:
                    d.rmdir()
                except OSError:
                    pass
    return out


# ---- usage report (all four roots) -----------------------------------------------


@dataclass
class RootUsage:
    """One line of ``bh backup usage`` — a root's current size + its retention policy."""

    root: str  # "hq" | "hive" | "mirror" | "migrate" | "pre-migrate" | "legacy"
    label: str
    path: Path
    size_bytes: int
    detail: str = ""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def usage_report(cfg=None) -> list[RootUsage]:
    """Every backup root's current size: HQ's dated sets, every registered + locally-checked-out
    hive's ``.beads/backup/`` (bd's own), the CURRENT hive's mirror export slot, the
    pre-migration sets, any leftover in-repo pre-migrate stores, and any artifact still sitting
    in a pre-bh-5009a location. Read-only — never mutates anything on disk.

    The migration artifacts being HERE is the point of bh-5009a: before it, the strongest backup
    ``bh`` takes (a verified JSONL + Dolt-native pair, taken immediately before an irreversible-
    feeling operation) was invisible to the one command whose job is to show what backups
    exist, and nothing pruned it."""
    from . import registry
    from .identity import workspace_root

    cfg = cfg if cfg is not None else config.load()
    out: list[RootUsage] = []

    hq_path, hq_bytes, hq_count = hq_backup_usage(cfg)
    out.append(
        RootUsage(
            root="hq",
            label=f"HQ pre-push backup ({_plural(hq_count, 'dated set')})",
            path=hq_path,
            size_bytes=hq_bytes,
            detail=f"keep newest {config.backup_hq_keep(cfg)} (backup.hq_keep)",
        )
    )

    for entry in _backed_up_entries(cfg):
        hive_dir = registry.hive_dir(entry)
        if not hive_dir.is_dir():
            continue
        size = hive_backup_usage(hive_dir)
        if size == 0 and not hive_backup_dir(hive_dir).is_dir():
            continue  # bd's backup: never configured for this hive — nothing to report
        prefix = entry.get("prefix") or entry.get("repo") or str(hive_dir)
        # bh-ypfnu: this row used to always claim `.beads/backup` was "bd's own" live
        # destination for a migrated hive — measured false on both bh-infra (mis-pointed into
        # this run's migrate snapshot) and nvhack (dangling, the snapshot it names was since
        # relocated). Say so instead of asserting a location bd may not actually be using.
        mispointed = bd_backup_points_into_migrate_root(hive_dir, cfg)
        detail = (
            f"⚠ bd's live destination is NOT actually here — it is dangling/mis-pointed into "
            f"a migrate set ({mispointed}); `bh hive migrate-storage {prefix}` re-points it "
            "(bh-ypfnu)"
            if mispointed is not None
            else f"cap {config.backup_hive_cap_mb(cfg)} MB (backup.hive_cap_mb)"
        )
        out.append(
            RootUsage(
                root="hive",
                label=f"{prefix}  .beads/backup (bd's own)",
                path=hive_backup_dir(hive_dir),
                size_bytes=size,
                detail=detail,
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

    mig_path, mig_bytes, mig_sets, mig_hives = migrate_usage(cfg)
    out.append(
        RootUsage(
            root="migrate",
            label=f"pre-migration backup ({_plural(mig_sets, 'set')}, {mig_hives} hive(s))",
            path=mig_path,
            size_bytes=mig_bytes,
            detail=(
                f"keep newest {config.backup_migrate_keep(cfg)} per hive "
                "(backup.migrate_keep), pruned after each verified migration"
            ),
        )
    )

    stores = pre_migrate_stores(cfg)
    if stores:
        out.append(
            RootUsage(
                root="pre-migrate",
                label=f"in-repo pre-migrate stores ({_plural(len(stores), 'hive')})",
                path=stores[0] if len(stores) == 1 else Path(workspace_root()),
                size_bytes=sum(_dir_size(s) for s in stores),
                detail=(
                    "superseded by the migrate set above once verified — remove with "
                    "`bh backup reclaim --root migrate --confirm`"
                ),
            )
        )

    for legacy in legacy_roots(cfg):
        out.append(
            RootUsage(
                root="legacy",
                label=f"legacy {legacy.kind} location (pre-bh-5009a layout)",
                path=legacy.path,
                size_bytes=legacy.size_bytes,
                detail="still read, but relocate it with `bh backup migrate-layout --confirm`",
            )
        )
    return out


def total_warning(entries: list[RootUsage], cfg=None) -> str:
    """A one-line warning when everything above adds up past ``backup.total_warn_mb``, else "".

    The counterpart to keep-3 on the HQ root (bh-5009a): the alternative considered there was
    pruning HQ's pre-push set once the remote exists, which was rejected — after a successful
    push the REMOTE holds the post-migration state, so a migration that corrupted something has
    already propagated the corruption, and the pre-push set is the only clean rollback point.
    A size warning gets the disk relief without trading away the one backup that cannot be
    reconstructed."""
    cfg = cfg if cfg is not None else config.load()
    cap_mb = config.backup_total_warn_mb(cfg)
    total_mb = sum(e.size_bytes for e in entries) / (1024 * 1024)
    if cap_mb <= 0 or total_mb < cap_mb:
        return ""
    return (
        f"backups total {total_mb:,.0f} MB, past the {cap_mb} MB warning threshold "
        "(backup.total_warn_mb) — `bh backup reclaim --root all --dry-run` shows what "
        "retention would free"
    )
