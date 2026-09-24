"""Safe host-local package cache placement for managed worktrees.

The resolver knows about filesystems and cache adapter controls, but never about cache
contents.  Package managers remain solely responsible for populating their opaque stores.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import platformdirs

from .config_consumer_ports import work_settings as config

DEFAULT_EPHEMERAL_ROOT = Path("/tmp/bh-cache")
DEFAULT_MIN_FREE_BYTES = 512 * 1024**2
DEFAULT_MIN_FREE_INODES = 10_000
PRIVATE_MODE = 0o700
_APPLICATION = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class CacheLocalityError(RuntimeError):
    """A managed cache candidate failed its safety contract."""


@dataclass(frozen=True)
class CacheAdapter:
    """Framework controls required to select a cache and materialization method."""

    application: str
    cache_environment: str
    cache_environment_aliases: tuple[str, ...] = ()
    hardlink_environment: tuple[tuple[str, str], ...] = ()
    copy_environment: tuple[tuple[str, str], ...] = ()
    native_aliases: tuple[str, ...] = ()
    dependency_directory: str = ".cache-target"
    native_subdirectory: str = ""


UV_ADAPTER = CacheAdapter(
    application="uv",
    cache_environment="UV_CACHE_DIR",
    hardlink_environment=(("UV_LINK_MODE", "hardlink"),),
    copy_environment=(("UV_LINK_MODE", "copy"),),
    dependency_directory=".venv",
    native_subdirectory="uv",
)
PNPM_ADAPTER = CacheAdapter(
    application="pnpm",
    cache_environment="PNPM_CONFIG_STORE_DIR",
    cache_environment_aliases=("npm_config_store_dir",),
    native_aliases=("PNPM_STORE_DIR",),
    hardlink_environment=(
        ("PNPM_CONFIG_PACKAGE_IMPORT_METHOD", "hardlink"),
        ("npm_config_package_import_method", "hardlink"),
    ),
    copy_environment=(
        ("PNPM_CONFIG_PACKAGE_IMPORT_METHOD", "copy"),
        ("npm_config_package_import_method", "copy"),
    ),
    dependency_directory="node_modules",
    native_subdirectory="pnpm/store",
)
ADAPTERS = {adapter.application: adapter for adapter in (UV_ADAPTER, PNPM_ADAPTER)}


@dataclass(frozen=True)
class CacheSelection:
    application: str
    path: Path
    target: Path
    tier: str
    link_method: str
    environment: dict[str, str]
    admitted: bool
    managed: bool
    cache_device: int | None
    target_device: int | None
    free_bytes: int | None
    free_inodes: int | None
    diagnostic: str

    def report(self) -> dict[str, object]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        payload["target"] = str(self.target)
        return payload


def adapter_for_application(application: str) -> CacheAdapter:
    try:
        return ADAPTERS[application]
    except KeyError as exc:
        raise CacheLocalityError(f"unsupported cache application {application!r}") from exc


def _safe_application(application: str) -> str:
    if not _APPLICATION.fullmatch(application):
        raise CacheLocalityError(
            "cache application must be a normalized lower-case name without path separators"
        )
    return application


def _nearest_existing(path: Path) -> Path:
    candidate = path.absolute()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise CacheLocalityError(f"no existing parent for {path}")
        candidate = parent
    return candidate


def _device(path: Path) -> int:
    return _nearest_existing(path).stat().st_dev


def _capacity(path: Path) -> tuple[int, int]:
    existing = _nearest_existing(path)
    return shutil.disk_usage(existing).free, os.statvfs(existing).f_favail


def _check_owned_directory(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CacheLocalityError(f"cache path is symlinked or not a directory: {path}")
    if info.st_uid != os.getuid():
        raise CacheLocalityError(
            f"cache path is owned by uid {info.st_uid}, expected {os.getuid()}: {path}"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode != PRIVATE_MODE:
        path.chmod(PRIVATE_MODE)
        if stat.S_IMODE(path.stat().st_mode) != PRIVATE_MODE:
            raise CacheLocalityError(f"cache path mode is not 0700: {path}")


def _acquire_init_lock(root: Path, timeout: float = 5.0) -> int:
    descriptor = os.open(root / ".beadhive-init.lock", os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise CacheLocalityError(
                    f"timed out initializing shared cache root: {root}"
                ) from None
            time.sleep(0.01)


def _prepare_managed(root: Path, application: str) -> Path:
    if not root.is_absolute():
        raise CacheLocalityError(f"managed cache root must be absolute: {root}")
    if root.exists() and root.is_symlink():
        raise CacheLocalityError(f"managed cache root must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_MODE)
    _check_owned_directory(root)
    descriptor = _acquire_init_lock(root)
    try:
        child = root / _safe_application(application)
        child.mkdir(mode=PRIVATE_MODE, exist_ok=True)
        _check_owned_directory(child)
        return child
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _prepare_explicit(path: Path) -> Path:
    """Create the exact operator-selected cache without managing or traversing its contents."""
    if path.exists() and path.is_symlink():
        raise CacheLocalityError(f"explicit cache path must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_MODE)
    _check_owned_directory(path)
    return path


def configured_ephemeral(checkout: Path) -> bool | None:
    """Return Beadhive's authoritative class for a managed checkout, if it is one."""
    try:
        cfg = config.load()
        root = config.worktrees_root(cfg).expanduser().absolute()
        checkout.absolute().relative_to(root)
        return config.worktrees_ephemeral(cfg)
    except ValueError:
        return False
    except (ImportError, OSError, RuntimeError, TypeError):
        return None


def _filesystem_type(path: Path) -> str:
    """Return the Linux mount type for *path*; empty on platforms without mountinfo."""
    try:
        resolved = _nearest_existing(path).resolve()
        matches: list[tuple[int, str]] = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, separator, after = line.partition(" - ")
            if not separator:
                continue
            fields = before.split()
            mount = Path(fields[4].replace("\\040", " "))
            try:
                resolved.relative_to(mount)
            except ValueError:
                continue
            matches.append((len(mount.parts), after.split()[0]))
        return max(matches)[1] if matches else ""
    except (OSError, IndexError, ValueError):
        return ""


def _is_ephemeral(
    checkout: Path,
    environ: Mapping[str, str],
    worktree_root: Path | None = None,
) -> bool:
    explicit = environ.get("BH_WORKTREES")
    roots = [worktree_root] if worktree_root is not None else []
    if explicit:
        roots.append(Path(explicit).expanduser())
    roots.append(Path(tempfile.gettempdir()) / "bh-worktrees")
    checkout = checkout.absolute()
    for root in roots:
        if root is None:
            continue
        root = root.expanduser().absolute()
        try:
            checkout.relative_to(root)
        except ValueError:
            continue
        # /tmp is the portable managed ephemeral convention.  A custom root is classified from
        # its real mount, so /run/user/... and other host tmpfs layouts work without a path list.
        try:
            root.relative_to(Path(tempfile.gettempdir()).absolute())
            return True
        except ValueError:
            return _filesystem_type(root) in {"tmpfs", "ramfs"}
    return _filesystem_type(checkout) in {"tmpfs", "ramfs"}


def _native_override(adapter: CacheAdapter, env: Mapping[str, str]) -> str:
    for key in (
        adapter.cache_environment,
        *adapter.cache_environment_aliases,
        *adapter.native_aliases,
    ):
        if value := env.get(key, "").strip():
            return value
    return ""


def _environment(adapter: CacheAdapter, path: Path, same_device: bool) -> dict[str, str]:
    values = {adapter.cache_environment: str(path)} if adapter.cache_environment else {}
    values.update({key: str(path) for key in adapter.cache_environment_aliases})
    values.update(adapter.hardlink_environment if same_device else adapter.copy_environment)
    return values


def _selection(
    adapter: CacheAdapter,
    path: Path,
    target: Path,
    *,
    tier: str,
    managed: bool,
    admitted: bool,
    diagnostic: str,
) -> CacheSelection:
    cache_device = _device(path)
    target_device = _device(target)
    same_device = cache_device == target_device and admitted
    free_bytes, free_inodes = _capacity(path)
    return CacheSelection(
        application=adapter.application,
        path=path,
        target=target,
        tier=tier,
        link_method="hardlink" if same_device else "copy",
        environment=_environment(adapter, path, same_device),
        admitted=same_device,
        managed=managed,
        cache_device=cache_device,
        target_device=target_device,
        free_bytes=free_bytes,
        free_inodes=free_inodes,
        diagnostic=diagnostic,
    )


def resolve_cache(
    adapter: CacheAdapter,
    checkout: Path,
    dependency_target: Path | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    ephemeral: bool | None = None,
    worktree_root: Path | None = None,
) -> CacheSelection:
    """Choose a safe cache tier and explicit link method for one dependency target."""
    env = os.environ if environ is None else environ
    application = _safe_application(adapter.application)
    checkout = checkout.absolute()
    target = dependency_target or checkout / adapter.dependency_directory
    target = target.absolute()

    fallback_reasons: list[str] = []
    if override := _native_override(adapter, env):
        path = Path(override).expanduser().absolute()
        try:
            _prepare_explicit(path)
            same = _device(path) == _device(target)
            reason = "explicit native cache override"
            if not same:
                reason += "; cache and dependency target are on different devices, using copy mode"
            return _selection(
                adapter,
                path,
                target,
                tier="native-explicit",
                managed=False,
                admitted=same,
                diagnostic=reason,
            )
        except (CacheLocalityError, OSError) as exc:
            fallback_reasons.append(f"explicit native cache unsafe: {exc}")

    ephemeral = _is_ephemeral(checkout, env, worktree_root) if ephemeral is None else ephemeral
    if ephemeral:
        configured = env.get("BH_TMPFS_CACHE_DIR", str(DEFAULT_EPHEMERAL_ROOT))
        root = Path(configured).expanduser()
        try:
            path = _prepare_managed(root, application)
            free_bytes, free_inodes = _capacity(path)
            minimum_bytes = int(env.get("BH_CACHE_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES))
            minimum_inodes = int(env.get("BH_CACHE_MIN_FREE_INODES", DEFAULT_MIN_FREE_INODES))
            if free_bytes < minimum_bytes or free_inodes < minimum_inodes:
                raise CacheLocalityError(
                    f"tmpfs reserve failed: {free_bytes} bytes/{free_inodes} inodes free; "
                    f"requires {minimum_bytes}/{minimum_inodes}"
                )
            if _device(path) != _device(target):
                raise CacheLocalityError(
                    "tmpfs cache and dependency target are on different devices"
                )
            return _selection(
                adapter,
                path,
                target,
                tier="ephemeral",
                managed=True,
                admitted=True,
                diagnostic="ephemeral cache admitted on dependency target device",
            )
        except (CacheLocalityError, OSError, ValueError) as exc:
            fallback_reasons.append(str(exc))

    xdg = Path(env.get("XDG_CACHE_HOME", platformdirs.user_cache_path())).expanduser().absolute()
    durable_root = Path(
        env.get("BH_DURABLE_CACHE_DIR", xdg / "beadhive" / "frameworks")
    ).expanduser()
    try:
        durable = _prepare_managed(durable_root, application)
        if _device(durable) == _device(target):
            reason = "durable cache admitted on dependency target device"
            if fallback_reasons:
                reason = f"cache fallback ({'; '.join(fallback_reasons)}); {reason}"
            return _selection(
                adapter,
                durable,
                target,
                tier="durable",
                managed=True,
                admitted=True,
                diagnostic=reason,
            )
        fallback_reasons.append("durable cache and dependency target are on different devices")
    except (CacheLocalityError, OSError, ValueError) as exc:
        fallback_reasons.append(f"durable cache unsafe: {exc}")

    native = xdg / (adapter.native_subdirectory or application)
    reason = "; ".join(fallback_reasons) or "no same-device managed cache"
    reason += "; using native cache with explicit copy mode"
    return _selection(
        adapter,
        native,
        target,
        tier="native-copy",
        managed=False,
        admitted=False,
        diagnostic=reason,
    )


def unsupported_cache_fallback(
    application: str,
    checkout: Path,
    dependency_target: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> CacheSelection:
    """Describe the visible copy fallback when no framework adapter is installed.

    With no supported cache variable Beadhive cannot relocate or prune that framework's cache.
    The command therefore inherits its native behavior and this result makes that decision
    observable to callers without inventing unsupported controls.
    """
    application = _safe_application(application)
    env = os.environ if environ is None else environ
    xdg = Path(env.get("XDG_CACHE_HOME", platformdirs.user_cache_path())).expanduser().absolute()
    adapter = CacheAdapter(application=application, cache_environment="")
    path = xdg / application
    target = (dependency_target or checkout / adapter.dependency_directory).absolute()
    return _selection(
        adapter,
        path,
        target,
        tier="unsupported-copy",
        managed=False,
        admitted=False,
        diagnostic=(
            f"unsupported cache application {application!r}; no cache controls exported; "
            "using the framework's durable native cache/copy behavior"
        ),
    )


def prune_cache(_selection: CacheSelection) -> None:
    """Refuse opaque cache pruning; framework-supported maintenance owns that operation."""
    raise CacheLocalityError(
        "Beadhive does not prune opaque framework caches; wait for users to become idle and "
        "use the framework's supported cache command"
    )


def adapter_for_command(command: Sequence[str]) -> CacheAdapter | None:
    """Return the built-in adapter used by a direct or shell-wrapped command."""
    tokens = [str(token) for token in command]
    for token in tokens:
        name = Path(token).name
        if name in ADAPTERS:
            return ADAPTERS[name]
        if token in {"-c", "sh", "bash"}:
            continue
        for application, adapter in ADAPTERS.items():
            if re.search(rf"(^|[;&|()\s]){re.escape(application)}([;&|()\s]|$)", token):
                return adapter
    return None


def command_environment(
    command: Sequence[str],
    checkout: Path,
    environ: Mapping[str, str] | None = None,
    *,
    worktree_root: Path | None = None,
    ephemeral: bool | None = None,
) -> tuple[dict[str, str], CacheSelection] | None:
    adapter = adapter_for_command(command)
    if adapter is None:
        return None
    env = dict(os.environ if environ is None else environ)
    selection = resolve_cache(
        adapter,
        checkout,
        environ=env,
        worktree_root=worktree_root,
        ephemeral=ephemeral,
    )
    env.update(selection.environment)
    return env, selection


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application", choices=sorted(ADAPTERS))
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--format", choices=("json", "lines"), default="json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    selection = resolve_cache(
        adapter_for_application(options.application),
        options.checkout,
        options.target,
        ephemeral=configured_ephemeral(options.checkout),
    )
    if options.format == "lines":
        print(selection.path)
        print(selection.link_method)
        print(selection.diagnostic)
    else:
        print(json.dumps(selection.report(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
