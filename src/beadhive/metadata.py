"""ws.metadata — the single workspace-metadata aggregation path (read-through cache).

`ws doctor` and `ws hive survey` recompute the whole fleet's repo state from scratch on every
invocation; ~62% of that is an `os.walk` disk-sizing per repo (see docs/METADATA-CACHE.md §1).
This module is the one place that owns the expensive walk + git plumbing (``measure``) and
persists the result under ``$BH_CACHE/metadata.json`` so a second read serves in ~ms.

Pure read / compute / store — no rendering, no ``typer``. Sits above ``safety`` / ``registry`` /
``identity`` / ``config`` and below the consumers, so ``doctor`` / ``survey`` can read *from*
this module (bead .3) without an import cycle. Event invalidation + background reload are bead .4.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

from . import config, identity, registry, safety

# On-disk schema version — a file with a different version is treated as an empty (cold) cache.
CACHE_VERSION = 1

# Default coarse TTL for the whole-file freshness backstop (config `metadata.ttl`, seconds).
DEFAULT_TTL = 300.0

_CACHE_FILENAME = "metadata.json"
_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RepoMetadata:
    """One ``repos.<provider/org/repo>`` record.

    A cheap freshness fingerprint (``git_head`` + ``git_mtime``, recomputed without walking the
    tree) plus the payload the two consumers pull today (the union of ``safety.ScanResult`` +
    commit age / maturity / last-commit-date). ``difficulty`` is deliberately NOT stored — it is a
    cheap pure derivation (``safety.difficulty``) consumers recompute on read.
    """

    git_head: str
    git_mtime: float
    measured_at: str
    category: str
    has_origin: bool
    stash_count: int
    disk_bytes: int
    commit_count: int
    age_days: float | None
    last_commit: str | None
    branches: list[dict] = field(default_factory=list)
    worktrees: list[str] = field(default_factory=list)
    dolt_ref: dict = field(default_factory=dict)


@dataclass
class MetadataCache:
    """The whole ``$BH_CACHE/metadata.json`` object."""

    version: int
    last_updated: str | None
    workspace_root: str
    repos: dict[str, RepoMetadata] = field(default_factory=dict)


_REPO_FIELDS = frozenset(f.name for f in fields(RepoMetadata))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """Current UTC instant as an ``%Y-%m-%dT%H:%M:%SZ`` stamp."""
    return datetime.now(UTC).strftime(_TIMESTAMP_FMT)


def _cache_path() -> Path:
    """``$BH_CACHE/metadata.json`` (reuses the existing minimal-clone cache dir)."""
    return config.cache_dir() / _CACHE_FILENAME


def _last_commit_date(repo_path: str) -> str | None:
    """Last-commit date as ``YYYY-MM-DD``, or ``None`` for a repo with no commits.

    Mirrors ``survey._last_commit_date`` but owned here to keep this module below the consumers
    (survey reads from the cache in bead .3, so importing it would cycle).
    """
    rc, out = safety._run(["log", "-1", "--format=%ci"], repo_path)
    if rc != 0 or not out.strip():
        return None
    # %ci format: "YYYY-MM-DD HH:MM:SS +ZONE" — take only the date part.
    return out.strip().split()[0]


def _age_seconds(measured_at: str | None) -> float:
    """Seconds since ``measured_at`` (``inf`` when absent/unparseable)."""
    if not measured_at:
        return float("inf")
    try:
        dt = datetime.strptime(measured_at, _TIMESTAMP_FMT).replace(tzinfo=UTC)
    except ValueError:
        return float("inf")
    return (datetime.now(UTC) - dt).total_seconds()


def _ttl_expired(measured_at: str | None, ttl: float | None) -> bool:
    """Whether the coarse TTL backstop considers ``measured_at`` stale.

    ``None`` (unset) / negative (never-expire) ⇒ never; ``0`` (bypass) ⇒ always; positive ⇒
    age-based. See docs/METADATA-CACHE.md §3.
    """
    if ttl is None or ttl < 0:
        return False
    return _age_seconds(measured_at) >= ttl


def _repo_from_dict(rec) -> RepoMetadata | None:
    """Parse one persisted record; drop it (``None``) on any shape mismatch."""
    if not isinstance(rec, dict):
        return None
    try:
        return RepoMetadata(**{k: rec[k] for k in _REPO_FIELDS if k in rec})
    except TypeError:
        return None


def _fleet_keys(cfg, root: str) -> list[str]:
    """Every on-disk ``provider/org/repo`` git repo under recognized provider dirs.

    Matches ``doctor._scan``'s git-repo enumeration (the exact universe Fleet Health feeds on),
    kept local so this module doesn't import ``doctor``.
    """
    providers = set(registry.effective_providers(cfg))
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    def _dirs(p: Path) -> list[Path]:
        return sorted(c for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))

    keys: list[str] = []
    for prov in _dirs(root_path):
        if prov.name not in providers:
            continue
        for org in _dirs(prov):
            for repo in _dirs(org):
                if (repo / ".git").exists():
                    keys.append(f"{prov.name}/{org.name}/{repo.name}")
    return keys


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def ttl(cfg=None) -> float:
    """Coarse TTL backstop in seconds (config ``metadata.ttl``, default ``300``).

    ``0`` = always-fresh/bypass, negative = never-expire. Read the same way other ``ws`` modules
    read their config section.
    """
    return config.metadata_ttl(cfg)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def load(cfg=None) -> MetadataCache:
    """Parse ``$BH_CACHE/metadata.json``.

    Missing / unparseable / wrong-``version`` file ⇒ an empty (cold) cache — never raises. A
    ``workspace_root`` mismatch (the root moved) is a hard coarse-invalidate: the persisted repos
    are dropped and an empty cache stamped with the current root is returned (§3).
    """
    root = identity.workspace_root()
    empty = MetadataCache(version=CACHE_VERSION, last_updated=None, workspace_root=root, repos={})

    path = _cache_path()
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return empty
    if data.get("workspace_root") != root:
        return empty  # root moved ⇒ every cached path is wrong

    repos: dict[str, RepoMetadata] = {}
    for key, rec in (data.get("repos") or {}).items():
        parsed = _repo_from_dict(rec)
        if parsed is not None:
            repos[key] = parsed
    return MetadataCache(
        version=CACHE_VERSION,
        last_updated=data.get("last_updated"),
        workspace_root=root,
        repos=repos,
    )


def get(cfg, key: str) -> RepoMetadata | None:
    """One repo's record, or ``None`` if absent. Freshness is the caller's call (``is_stale``)."""
    return load(cfg).repos.get(key)


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------


def measure(path: str | Path) -> RepoMetadata:
    """Compute ONE record from scratch — the expensive path (``safety.scan`` + age + maturity).

    The single choke point that owns the walk + git plumbing, so every read path and the (bead .4)
    background reloader share one implementation and one attribution point.
    """
    resolved = str(Path(path).resolve())
    scan = safety.scan(resolved)
    age = safety.last_commit_age_days(resolved)
    head, mtime = fingerprint(resolved)
    return RepoMetadata(
        git_head=head,
        git_mtime=mtime,
        measured_at=_now(),
        category=str(scan.category),
        has_origin=scan.has_origin,
        stash_count=scan.stash_count,
        disk_bytes=scan.disk_bytes,
        commit_count=safety._maturity_commit_count(resolved),
        age_days=None if math.isinf(age) else age,
        last_commit=_last_commit_date(resolved),
        branches=[asdict(b) for b in scan.branches],
        worktrees=list(scan.worktrees),
        dolt_ref=asdict(scan.dolt_ref),
    )


def fingerprint(path: str | Path) -> tuple[str, float]:
    """Cheap ``(git_head, git_mtime)`` for a repo WITHOUT walking the tree — the staleness probe.

    ``git_head`` is ``git rev-parse HEAD`` (empty for a no-commit repo); ``git_mtime`` is the mtime
    of ``<repo>/.git`` (a refs/index churn signal). Detects *git-state* change, not pure
    working-tree-size change (§3 caveat).
    """
    resolved = Path(path).resolve()
    rc, out = safety._run(["rev-parse", "HEAD"], str(resolved))
    head = out.strip() if rc == 0 else ""
    try:
        mtime = (resolved / ".git").stat().st_mtime
    except OSError:
        mtime = 0.0
    return head, mtime


def is_stale(entry: RepoMetadata | None, path, *, ttl: float | None) -> bool:
    """True if the entry is absent, its fingerprint no longer matches, OR the TTL backstop fired."""
    if entry is None:
        return True
    head, mtime = fingerprint(path)
    if head != entry.git_head or mtime != entry.git_mtime:
        return True
    return _ttl_expired(entry.measured_at, ttl)


# ---------------------------------------------------------------------------
# Refresh / write
# ---------------------------------------------------------------------------


def store(cfg, cache: MetadataCache) -> None:
    """Atomic write of the whole file (temp + ``os.replace``) so a reader never sees a half-file."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(cache), indent=2)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(payload)
    os.replace(tmp, path)


def refresh(cfg, keys: Iterable[str] | None = None, *, root=None) -> MetadataCache:
    """Recompute ``keys`` (or the full on-disk fleet when ``None``), merge into the loaded cache,
    stamp ``last_updated``, atomically store, and return the new cache. What a cold read and a
    (bead .4) background reload both call."""
    root = root or identity.workspace_root()
    cache = load(cfg)
    target = list(keys) if keys is not None else _fleet_keys(cfg, root)

    repos = dict(cache.repos)
    for key in target:
        repos[key] = measure(Path(root) / key)

    new = MetadataCache(
        version=CACHE_VERSION,
        last_updated=_now(),
        workspace_root=root,
        repos=repos,
    )
    store(cfg, new)
    return new


# ---------------------------------------------------------------------------
# Read-through convenience (consumers — bead .3)
# ---------------------------------------------------------------------------


def read_fleet(
    cfg,
    keys: list[str],
    *,
    ttl: float | None,
    on_miss: str = "compute",
) -> dict[str, RepoMetadata]:
    """Return records for ``keys``, serving fresh cached entries as-is.

    ``on_miss='compute'`` (blocking): stale/missing entries are computed inline and persisted.
    ``on_miss='stale'`` (serve-stale-while-revalidate): a stale entry is served as-is and a missing
    one is left absent — the background refresh that fills it is bead .4, so nothing is queued here.
    """
    root = identity.workspace_root()
    cache = load(cfg)

    out: dict[str, RepoMetadata] = {}
    to_compute: list[str] = []
    for key in keys:
        entry = cache.repos.get(key)
        if entry is not None and not is_stale(entry, Path(root) / key, ttl=ttl):
            out[key] = entry
        elif on_miss == "stale":
            if entry is not None:
                out[key] = entry  # serve stale while (bead .4) revalidates
        else:
            to_compute.append(key)

    if to_compute:
        refreshed = refresh(cfg, to_compute, root=root)
        for key in to_compute:
            rec = refreshed.repos.get(key)
            if rec is not None:
                out[key] = rec
    return out


# ---------------------------------------------------------------------------
# Invalidation + background reload (bead .4)
# ---------------------------------------------------------------------------


def _spawn_reload(cfg, keys):
    """Kick a best-effort daemon thread that recomputes ``keys`` and persists them.

    The single-repo walk relocates OFF the interactive path (docs/METADATA-CACHE.md §4): the
    mutating op returns immediately and a later ``doctor`` / ``survey`` reads a warm entry instead
    of paying the recompute inline. Deliberately one throwaway thread per invalidation — no pool,
    no daemon service (tunable later). Never raises into the caller. Returns the started ``Thread``.
    """
    def _reload():
        try:
            refresh(cfg, keys)
        except Exception:
            pass  # background best-effort — a failed reload just leaves the entry to a cold read

    t = threading.Thread(target=_reload, name="ws-metadata-reload", daemon=True)
    t.start()
    return t


def invalidate_all(cfg) -> None:
    """Coarse invalidation: drop every repo entry and reset ``last_updated`` (the next read is a
    cold recompute). Used by fleet-wide mutating ops where no single repo key is cheap/obvious."""
    root = identity.workspace_root()
    empty = MetadataCache(version=CACHE_VERSION, last_updated=None, workspace_root=root, repos={})
    store(cfg, empty)


def invalidate(cfg, key: str | None = None, *, reload: bool = True):
    """The single hook every mutating op calls to keep the workspace-metadata cache honest.

    ``key=None`` → coarse (:func:`invalidate_all`). ``key`` set → per-repo: drop that one entry so
    it is recomputed on next read, and — when ``reload`` and ``metadata.background_reload`` is on —
    kick a threaded single-repo :func:`refresh` so a later ``doctor`` / ``survey`` serves fresh data
    without a full blocking recompute on the hot path. Returns the reload ``Thread`` (or ``None``).

    Best-effort: a cache read/write failure is swallowed so it can never break the mutating op that
    called it — the fingerprint probe and TTL backstop still self-heal a missed invalidation.
    """
    try:
        if key is None:
            invalidate_all(cfg)
            return None
        cache = load(cfg)
        if key in cache.repos:
            repos = dict(cache.repos)
            del repos[key]
            store(cfg, MetadataCache(
                version=CACHE_VERSION,
                last_updated=cache.last_updated,
                workspace_root=cache.workspace_root,
                repos=repos,
            ))
        if reload and config.metadata_background_reload(cfg):
            return _spawn_reload(cfg, [key])
    except Exception:
        return None
    return None
