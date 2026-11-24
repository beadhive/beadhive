"""Machine paths, environment resolution, and packaged config assets.

The public surface remains :mod:`beadhive.config`.  Functions here accept that
facade as a collaborator so its long-standing monkeypatch seams remain runtime
lookups rather than import-time aliases.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BH_", extra="ignore", env_ignore_empty=True)

    home: str | None = Field(None, validation_alias=AliasChoices("BH_HOME", "WS_HOME"))
    config: str | None = Field(None, validation_alias=AliasChoices("BH_CONFIG", "WS_CONFIG"))
    hub: str | None = Field(None, validation_alias=AliasChoices("BH_HUB", "WS_HUB"))
    hq: str | None = Field(None, validation_alias=AliasChoices("BH_HQ", "WS_HQ"))
    cache: str | None = Field(None, validation_alias=AliasChoices("BH_CACHE", "WS_CACHE"))
    worktrees: str | None = Field(
        None, validation_alias=AliasChoices("BH_WORKTREES", "WS_WORKTREES")
    )
    debug: str | None = Field(None, validation_alias=AliasChoices("BH_DEBUG", "WS_DEBUG"))
    bd_pass_enabled: str | None = Field(
        None, validation_alias=AliasChoices("BH_BD_PASS_ENABLED", "WS_BD_PASS_ENABLED")
    )
    git_pass_enabled: str | None = Field(
        None, validation_alias=AliasChoices("BH_GIT_PASS_ENABLED", "WS_GIT_PASS_ENABLED")
    )
    skip_setup_check: str | None = Field(
        None, validation_alias=AliasChoices("BH_SKIP_SETUP_CHECK", "WS_SKIP_SETUP_CHECK")
    )
    image_manifest: str | None = Field(None, validation_alias=AliasChoices("BH_IMAGE_MANIFEST"))
    plugin_dir: str | None = Field(None, validation_alias=AliasChoices("BH_PLUGIN_DIR"))
    opencode_skills_home: str | None = Field(
        None, validation_alias=AliasChoices("BH_OPENCODE_SKILLS_HOME")
    )
    claude_home: str | None = Field(None, validation_alias=AliasChoices("BH_CLAUDE_HOME"))
    codex_home: str | None = Field(None, validation_alias=AliasChoices("BH_CODEX_HOME"))
    harness: str | None = Field(None, validation_alias=AliasChoices("BH_HARNESS"))
    role: str | None = Field(None, validation_alias=AliasChoices("BH_ROLE", "WS_ROLE"))
    dev: str | None = Field(None, validation_alias=AliasChoices("BH_DEV", "WS_DEV"))
    crew: str | None = Field(None, validation_alias=AliasChoices("BH_CREW", "WS_CREW"))
    genai_model: str | None = Field(
        None, validation_alias=AliasChoices("BH_GENAI_MODEL", "WS_GENAI_MODEL")
    )
    genai_system: str | None = Field(
        None, validation_alias=AliasChoices("BH_GENAI_SYSTEM", "WS_GENAI_SYSTEM")
    )
    observaloop_profile: str | None = Field(
        None, validation_alias=AliasChoices("BH_OBSERVALOOP_PROFILE", "WS_OBSERVALOOP_PROFILE")
    )


def env(api, field: str) -> str | None:
    value = getattr(api._Env(), field)
    if value is not None:
        aliases = api._Env.model_fields[field].validation_alias.choices
        if len(aliases) > 1 and os.environ.get(aliases[0]) is None and os.environ.get(aliases[1]):
            api._warning(
                "deprecated_env_var",
                logger_name=api.__name__,
                old=aliases[1],
                new=aliases[0],
                hint=f"set {aliases[0]} instead — {aliases[1]} support will be removed later",
            )
    return value


def home(api) -> Path:
    value = api._env("home")
    return Path(value).expanduser() if value else api._DEFAULT_HOME_NEW


_HQ_PATH_MEMO: dict[Path, tuple[tuple[int, int], str | None]] = {}


def _host_hq_path(path: Path) -> str | None:
    """``hq.path`` from the host file, or ``None``. A lightweight ``(mtime, size)``-keyed read
    that deliberately bypasses the layered loader: ``hq_dir`` is called from the loader's own
    neighbours, so it must never recurse into (or add parses to) a full load, and a missing,
    unreadable or malformed file simply means the key is unset."""
    try:
        info = path.stat()
        signature = (info.st_mtime_ns, info.st_size)
        cached = _HQ_PATH_MEMO.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        from ruamel.yaml import YAML

        document = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        value = (document.get("hq") or {}).get("path")
        result = str(value).strip() if isinstance(value, str) and value.strip() else None
    except Exception:  # noqa: BLE001 - config unreadable: behave as if hq.path were unset
        return None
    _HQ_PATH_MEMO[path] = (signature, result)
    return result


def hq_dir(api) -> Path:
    """The HQ checkout. Precedence: ``$BH_HQ`` (legacy ``$WS_HQ``) > host config ``hq.path`` >
    ``<home>/hq``. The config read is best-effort and never raises: a missing, unreadable or
    malformed host file falls through to the default, exactly as a host without the key."""
    value = api._env("hq")
    if value:
        return Path(value).expanduser()
    configured = _host_hq_path(api.config_path())
    if configured:
        return Path(configured).expanduser()
    return api.home() / "hq"


def hq_home(api) -> Path:
    """Parent of the HQ checkout: where HQ-adjacent state (HQ backups, fleet state, beadyard
    adoption journals) lives. ``<home>`` by default, so an unconfigured host is unchanged.
    Only a host-config ``hq.path`` (with ``$BH_HQ`` unset) relocates it: ``$BH_HQ`` hosts keep
    their pre-existing state under ``<home>`` so an upgrade never strands it."""
    if not api._env("hq") and _host_hq_path(api.config_path()):
        return api.hq_dir().parent
    return api.home()


def config_path(api) -> Path:
    value = api._env("config")
    return Path(value).expanduser() if value else api.home() / "config.yaml"


def named_home(api, field: str, default: str) -> Path:
    value = api._env(field)
    return Path(value).expanduser() if value else api.home() / default


# statfs(2) ``f_type`` magics for memory-backed filesystems (linux/magic.h). A worktree root on
# one of these turns every per-tree ``.venv`` / build cache into unreclaimable RAM (bh-xzsdf).
TMPFS_MAGIC = 0x01021994
RAMFS_MAGIC = 0x858458F6
MEMORY_BACKED_MAGICS = frozenset({TMPFS_MAGIC, RAMFS_MAGIC})
ALLOW_TMPFS_KEY = "worktrees.allow_tmpfs"
ALLOW_TMPFS_ENV = "BH_WORKTREES_ALLOW_TMPFS"
_ENV_TRUE = frozenset({"1", "true", "yes", "on"})


def statfs_type(path: Path) -> int | None:
    """``statfs(2)`` ``f_type`` of the filesystem holding ``path`` (the nearest existing
    ancestor when ``path`` is not created yet), or ``None`` when it cannot be read (non-Linux,
    no libc, permission). Kept as the single seam tests fake instead of a real tmpfs."""
    # This buffer and f_type offset describe Linux statfs only. Darwin's
    # legacy LP64 and modern layouts both exceed this buffer and place f_type
    # elsewhere. Never call another platform's native ABI here.
    if sys.platform != "linux":
        return None
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(256)  # Linux struct statfs fits in this buffer.
        if libc.statfs(os.fsencode(probe), buf) != 0:
            return None
        return ctypes.c_long.from_buffer(buf).value & 0xFFFFFFFF  # f_type is the first field
    except (OSError, AttributeError, ValueError):
        return None


def is_memory_backed(api, path: Path) -> bool:
    """True when ``path`` lives on tmpfs/ramfs (RAM-backed, not disk)."""
    return api.statfs_type(path) in MEMORY_BACKED_MAGICS


def worktrees_allow_tmpfs(api, cfg=None) -> bool:
    """Explicit opt-in to a RAM-backed worktree root: ``$BH_WORKTREES_ALLOW_TMPFS`` or the
    ``worktrees.allow_tmpfs`` config key. Off by default."""
    if os.environ.get(ALLOW_TMPFS_ENV, "").strip().lower() in _ENV_TRUE:
        return True
    return bool(api.worktrees_cfg(cfg).get("allow_tmpfs", False))


def disk_worktrees_root(api, cfg=None) -> Path:
    """The disk-backed default root: ``worktrees.path`` when set, else ``<home>/worktrees``."""
    path = api.worktrees_cfg(cfg).get("path") or str(api.home() / "worktrees")
    return Path(path).expanduser()


def _in_use(root: Path) -> bool:
    """True when ``root`` is a directory that already holds worktrees (any entry)."""
    try:
        return root.is_dir() and any(root.iterdir())
    except OSError:
        return False


MACOS_USE_TEMP_KEY = "worktrees.macos_use_temp"
MACOS_ROOT_KEY = "worktrees.macos_root"


def host_platform() -> str:
    """``sys.platform``; the single seam tests inject to exercise the macOS branch on Linux."""
    return sys.platform


def macos_purges_temp(api, cfg=None) -> bool:
    """True on macOS unless ``worktrees.macos_use_temp`` opts back in. macOS periodic cleanup
    (dirhelper) purges files under ``$TMPDIR`` not accessed for ~3 days, which gutted a
    long-lived epic worktree (bh-uwlsw), so the temp dir is not a safe default root there."""
    if api.host_platform() != "darwin":
        return False
    return not bool(api.worktrees_cfg(cfg).get("macos_use_temp", False))


def macos_worktrees_root(api, cfg=None) -> Path:
    """Default root on macOS: ``worktrees.macos_root`` when set, else the disk root
    (``worktrees.path`` or ``<home>/worktrees``) -- never under ``$TMPDIR``."""
    value = api.worktrees_cfg(cfg).get("macos_root")
    if value:
        return Path(str(value)).expanduser()
    return api.disk_worktrees_root(cfg)


def worktrees_root(api, cfg=None) -> Path:
    """Resolve the shadow worktree root. ``$BH_WORKTREES`` wins; persistent mode uses the disk
    root. Ephemeral mode (default) prefers ``<os-temp>/bh-worktrees`` but that is RAM on many
    Linux hosts, so unless ``worktrees.allow_tmpfs`` opts in it falls back to the disk-backed
    root (bh-xzsdf).
    On macOS the default is the disk root instead (bh-uwlsw).
    Migration: a RAM-backed temp root that ALREADY holds worktrees keeps
    resolving (so in-flight beads stay reachable); :func:`worktrees_root_refusal` then blocks
    creating anything new there until it drains or the host opts in. An *explicitly* RAM-backed
    root (env / path) is likewise never silently moved."""
    value = api._env("worktrees")
    if value:
        return Path(value).expanduser()
    if api.worktrees_ephemeral(cfg):
        temp_root = Path(tempfile.gettempdir()) / "bh-worktrees"
        # macOS purges $TMPDIR, so every NEW worktree goes outside it (bh-uwlsw), even when the
        # temp root still holds old ones: those stay reachable through ``worktrees_roots`` (a
        # lookup-order change) and are never moved automatically.
        if api.macos_purges_temp(cfg):
            return api.macos_worktrees_root(cfg)
        if (
            api.worktrees_allow_tmpfs(cfg)
            or not api.is_memory_backed(temp_root)
            or _in_use(temp_root)
        ):
            return temp_root
    return api.disk_worktrees_root(cfg)


def under_os_temp(path: Path) -> bool:
    """True when ``path`` is under the OS temp dir (the always-sandbox-writable ``gettempdir``)."""
    try:
        Path(path).expanduser().resolve().relative_to(Path(tempfile.gettempdir()).resolve())
    except (ValueError, OSError):
        return False
    return True


def worktrees_root_needs_grant(api, cfg=None) -> bool:
    """True when the EFFECTIVE primary root is outside the OS temp dir, so a sandboxed seat
    needs an explicit write grant for it. Keyed off where the root really resolves, never off
    ``worktrees.ephemeral``: on macOS (bh-uwlsw) and on Linux hosts whose temp dir is RAM-backed
    the ephemeral default resolves to the disk root. A legacy in-use ``$TMPDIR`` root needs no
    grant of its own: it is under the OS temp dir, which sandboxes already make writable."""
    return not api.under_os_temp(api.worktrees_root(cfg))


def _holds_worktrees(root: Path) -> bool:
    """True when ``root`` holds at least one ``<provider>/<org>/<repo>/<leaf>`` directory.
    Empty provider/org/repo directories left behind after reclaim do not count."""
    try:
        return root.is_dir() and any(p.is_dir() for p in root.glob("*/*/*/*"))
    except OSError:
        return False


def legacy_temp_root(api, cfg=None) -> Path | None:
    """The purge-exposed ``<os-temp>/bh-worktrees`` when macOS no longer defaults there but it
    still holds worktrees (bh-uwlsw); else ``None``. Existing beads in it keep resolving, but
    nothing new is created there."""
    if not api.macos_purges_temp(cfg) or api._env("worktrees"):
        return None
    temp_root = Path(tempfile.gettempdir()) / "bh-worktrees"
    if temp_root == api.worktrees_root(cfg) or not _holds_worktrees(temp_root):
        return None
    return temp_root


def worktrees_roots(api, cfg=None) -> list[Path]:
    """Every root an EXISTING worktree may live under: the primary ``worktrees_root`` first,
    then the in-use legacy temp root (macOS only). Look existing paths up through this; create
    new ones under ``worktrees_root`` only."""
    roots = [api.worktrees_root(cfg)]
    legacy = api.legacy_temp_root(cfg)
    if legacy is not None:
        roots.append(legacy)
    return roots


def root_containing(api, path, cfg=None) -> Path | None:
    """The (resolved) worktree root holding ``path``, or ``None`` when it is under none."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    for root in api.worktrees_roots(cfg):
        try:
            base = root.resolve()
            resolved.relative_to(base)
        except (ValueError, OSError):
            continue
        return base
    return None


def legacy_temp_warning(api, cfg=None) -> str | None:
    """Loud text when macOS still has worktrees under the purge-exposed temp root, else ``None``."""
    legacy = api.legacy_temp_root(cfg)
    if legacy is None:
        return None
    return (
        f"macOS purges $TMPDIR and {legacy} still holds worktrees: they can lose their .git "
        "file and tracked files without warning (bh-uwlsw). New worktrees are created under "
        f"{api.worktrees_root(cfg)}. Drain the old ones: finish or `bh work abandon <id> --rm` "
        "each bead there (bh never moves or deletes them for you); once none remain this "
        "warning stops. A stray directory that is not a worktree also counts: remove it by "
        "hand to clear the warning."
    )


def worktrees_root_refusal(api, cfg=None) -> str | None:
    """Actionable refusal text when the resolved root is RAM-backed and not opted in, else
    ``None``. Applies to bead worktrees and the per-lane ``verify-*`` clean checkouts alike
    (they share the root). Existing worktrees on a RAM root are never touched by this."""
    root = api.worktrees_root(cfg)
    if api.worktrees_allow_tmpfs(cfg) or not api.is_memory_backed(root):
        return None
    return (
        f"worktree root {root} is on a RAM-backed filesystem (tmpfs/ramfs): per-worktree "
        "venvs and caches would consume unreclaimable memory.\n"
        "  Fix one: point it at disk (unset $BH_WORKTREES / set worktrees.path to a "
        "disk-backed directory; existing worktrees here must be finished/abandoned first) · "
        "or opt in explicitly with "
        f"`bh config set {ALLOW_TMPFS_KEY} true --scope host` (or {ALLOW_TMPFS_ENV}=1)"
    )


#: Default ceiling on live (provisioned, not yet reclaimed) worktrees per host. 158 accumulated
#: trees once livelocked a host (bh-qbu9t); 64 leaves ample room for a wide fan-out. ``0``
#: switches the cap off. Override with ``worktrees.max_live`` or ``$BH_WORKTREES_MAX_LIVE``.
DEFAULT_MAX_LIVE_WORKTREES = 64
MAX_LIVE_KEY = "worktrees.max_live"
MAX_LIVE_ENV = "BH_WORKTREES_MAX_LIVE"
RECLAIM_ON_MERGE_KEY = "worktrees.reclaim_on_merge"


def _as_count(value) -> int | None:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def worktrees_max_live(api, cfg=None) -> int:
    """Cap on live worktrees per host (``0`` = unlimited). ``$BH_WORKTREES_MAX_LIVE`` wins, then
    ``worktrees.max_live``; an unreadable / negative value falls back to the default rather than
    silently disabling the guard."""
    env = _as_count(os.environ.get(MAX_LIVE_ENV, "").strip() or None)
    if env is not None:
        return env
    configured = _as_count(api.worktrees_cfg(cfg).get("max_live"))
    return DEFAULT_MAX_LIVE_WORKTREES if configured is None else configured


#: Default RAM-backed usage (GiB) above which ``bh doctor`` escalates a swapless host. ``0``
#: switches every RAM-backed-storage doctor warning off (bh-01asp).
DEFAULT_RAM_WARN_GIB = 4.0
RAM_WARN_KEY = "worktrees.ram_warn_gib"


def worktrees_ram_warn_bytes(api, cfg=None) -> int:
    """Bytes of RAM-backed (tmpfs/ramfs) storage in use above which doctor warns that the host
    has no swap; ``0`` disables the RAM-backed doctor warnings. Unreadable / negative values
    fall back to :data:`DEFAULT_RAM_WARN_GIB`."""
    raw = api.worktrees_cfg(cfg).get("ram_warn_gib")
    try:
        gib = float(raw)
    except (TypeError, ValueError):
        gib = -1.0
    if gib < 0:
        gib = DEFAULT_RAM_WARN_GIB
    return int(gib * 1024**3)


def worktrees_reclaim_on_merge(api, cfg=None) -> bool:
    """Whether a clean ``work merge`` / ``work finish`` removes the merged bead's worktree
    (the branch stays the durable artifact). On by default; ``--rm`` forces it per call."""
    return bool(api.worktrees_cfg(cfg).get("reclaim_on_merge", True))


def codex_sandbox_active() -> bool:
    return bool(os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED", "").strip())


def codex_default_sandbox_covers(path: Path) -> bool:
    try:
        target = path.resolve()
    except OSError:
        return True
    return any(
        target == root or target.is_relative_to(root)
        for root in (Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve())
    )


def package_asset(package: str, *parts: str) -> Path:
    target = files(package)
    for part in parts:
        target = target / part
    return Path(str(target))


def scaffold_home(api, force: bool = False, dry_run: bool = False) -> list[tuple[Path, bool]]:
    host = api._host_module()
    pairs = [
        (api.template("config.example.yaml"), api.config_path()),
        (api.template("docker-compose.yml"), api.compose_file()),
        (api.template("docker-compose.otel.yml"), api.otel_compose_file()),
        (api.template("env.example"), api.home() / ".env.example"),
    ]
    if not dry_run:
        api.home().mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, bool]] = []
    for src, dst in pairs:
        if dst.exists() and not force:
            results.append((dst, False))
        elif dry_run:
            results.append((dst, True))
        else:
            shutil.copy(src, dst)
            results.append((dst, True))
    results.append((host.path(), (not host.path().exists()) if dry_run else host.mint_if_needed()))
    return results


def plugin_root(api, cfg=None) -> Path:
    override = api._Env().plugin_dir
    if override:
        return Path(override).expanduser()
    try:
        cfg = cfg if cfg is not None else api.load()
    except FileNotFoundError:
        cfg = {}
    plugin = api.claude_plugin_name(cfg)
    root = api._marketplace_root(cfg, plugin) or Path(api.__file__).resolve().parents[2]
    manifest = root / ".claude-plugin" / "marketplace.json"
    try:
        for item in json.loads(manifest.read_text()).get("plugins") or []:
            if (item or {}).get("name") == plugin:
                return (root / str(item.get("source") or ".")).resolve()
    except (OSError, json.JSONDecodeError):
        pass
    return root


def harness_home(api, field: str, *default: str) -> Path:
    override = getattr(api._Env(), field)
    return Path(override).expanduser() if override else Path.home().joinpath(*default)
