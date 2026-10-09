"""Machine paths, environment resolution, and packaged config assets.

The public surface remains :mod:`beadhive.config`.  Functions here accept that
facade as a collaborator so its long-standing monkeypatch seams remain runtime
lookups rather than import-time aliases.
"""

from __future__ import annotations

import json
import os
import shutil
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
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(256)  # struct statfs is < 130 bytes on every ABI
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


def worktrees_root(api, cfg=None) -> Path:
    """Resolve the shadow worktree root. ``$BH_WORKTREES`` wins; persistent mode uses the disk
    root. Ephemeral mode (default) prefers ``<os-temp>/bh-worktrees`` but that is RAM on many
    Linux hosts, so unless ``worktrees.allow_tmpfs`` opts in it falls back to the disk-backed
    root (bh-xzsdf). Migration: a RAM-backed temp root that ALREADY holds worktrees keeps
    resolving (so in-flight beads stay reachable); :func:`worktrees_root_refusal` then blocks
    creating anything new there until it drains or the host opts in. An *explicitly* RAM-backed
    root (env / path) is likewise never silently moved."""
    value = api._env("worktrees")
    if value:
        return Path(value).expanduser()
    if api.worktrees_ephemeral(cfg):
        temp_root = Path(tempfile.gettempdir()) / "bh-worktrees"
        if (
            api.worktrees_allow_tmpfs(cfg)
            or not api.is_memory_backed(temp_root)
            or _in_use(temp_root)
        ):
            return temp_root
    return api.disk_worktrees_root(cfg)


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
