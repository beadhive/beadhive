"""Compatibility collaborators for canonical configuration persistence ports."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from .modules.config.adapters.yaml_store import RoundTripYamlStore, round_trip_yaml
from .modules.config.application.resolution import deep_merge
from .modules.config.domain.ports import ConfigScope

_mutation_lock = threading.RLock()
_load_cache_lock = threading.RLock()
_load_cache: dict[tuple[tuple, tuple], str] = {}

# Public compatibility seams. The adapter scopes access with ``yaml_lock``; its standalone
# default constructs one parser per operation.
yaml = round_trip_yaml()
yaml_lock = threading.Lock()


def _store(api) -> RoundTripYamlStore:
    return RoundTripYamlStore(
        path_for=lambda scope: (
            api.fleet_path() if scope == ConfigScope.FLEET else api.config_path()
        ),
        binary_alias=api.BINARY_ALIAS,
        yaml_factory=lambda: api._yaml,
        yaml_lock=api._yaml_lock,
        mutation_lock=_mutation_lock,
    )


def mutation(path: Path):
    store = RoundTripYamlStore(
        path_for=lambda _scope: path,
        yaml_factory=lambda: yaml,
        yaml_lock=yaml_lock,
        mutation_lock=_mutation_lock,
    )
    return store.transaction(ConfigScope.HOST)


def load_path(api, path: Path, *, missing_ok: bool = False):
    return _store(api).load_path(path, missing_ok=missing_ok)


def _file_signature(path: Path) -> tuple:
    """Return the read-cache identity for a config path, including absence."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (str(path), None)
    return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def clear_load_cache() -> None:
    """Invalidate the process-local effective-config memo after an in-process write."""
    with _load_cache_lock:
        _load_cache.clear()


def leaf_paths(node, prefix: str = ""):
    yield from (path for path, _ in leaf_items(node, prefix))


def leaf_items(node, prefix: str = ""):
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from leaf_items(value, f"{prefix}.{key}" if prefix else str(key))
    elif prefix:
        yield prefix, node


def fleet_override_violations(host) -> list[str]:
    from . import config_partition

    return [
        path
        for path, value in leaf_items(host)
        if config_partition.partition_of(path) == config_partition.FLEET
        and not config_partition.host_override_value_allowed(path, value)
    ]


def _load_uncached(api):
    fleet = api.load_fleet()
    try:
        host = api.load_host()
    except FileNotFoundError:
        if not fleet:
            raise
        return fleet
    if not fleet:
        return host
    api._reject_fleet_overrides(host)
    return api._deep_merge(fleet, host)


def load(api):
    """Load an isolated effective config, parsing each unchanged layer once per process.

    The memo stores JSON rather than the mutable ``CommentedMap`` returned by ruamel.  Each
    caller gets a fresh plain mapping, so one request/thread cannot corrupt another caller's
    view.  The measured JSON round-trip costs 0.46 ms, versus 10.02 ms for deepcopy and
    74.06 ms for reparsing; comment-preserving mutation paths still use ``load_host`` /
    ``load_fleet`` directly.  Path and stat metadata make external edits self-invalidating.
    """
    key = (_file_signature(api.fleet_path()), _file_signature(api.config_path()))
    with _load_cache_lock:
        payload = _load_cache.get(key)
        if payload is None:
            payload = json.dumps(_load_uncached(api), separators=(",", ":"))
            _load_cache.clear()  # only the current filesystem revision is useful
            _load_cache[key] = payload
    return json.loads(payload)


def key_provenance(api) -> dict[str, str]:
    fleet_keys = set(api._leaf_paths(api.load_fleet()))
    try:
        host = api.load_host()
    except FileNotFoundError:
        host = CommentedMap()
    host_keys = set(api._leaf_paths(host))
    return {
        key: (
            api.PROVENANCE_OVERRIDE
            if key in fleet_keys and key in host_keys
            else api.PROVENANCE_HOST
            if key in host_keys
            else api.PROVENANCE_FLEET
        )
        for key in fleet_keys | host_keys
    }


def atomic_dump(api, data, path: Path) -> None:
    _store(api).save_path(data, path)


def save_host(api, data) -> None:
    api._guard_hq_registry_controller()
    _store(api).save_document(ConfigScope.HOST, data)
    clear_load_cache()


def save_fleet(api, data) -> None:
    _store(api).save_document(ConfigScope.FLEET, data)
    clear_load_cache()


def guard_hq_registry_controller(api) -> None:
    actor = api._env("dev") or api._env("crew") or ""
    api._guard_module().guard_controller_readonly(actor)


def reject_fleet_overrides(api, host) -> None:
    violations = api.fleet_override_violations(host)
    if not violations:
        return
    keys = "\n".join(f"  - {key}" for key in violations)
    raise api.ConfigError(
        f"host config {api.config_path()} overrides fleet-only key(s):\n{keys}\n"
        f"  these are fleet-wide truth and belong in {api.fleet_path()} — remove them from "
        "the host config, or add the key to "
        "config_partition.FLEET_HOST_OVERRIDE_ALLOWLIST if a per-host override is genuinely "
        "intended."
    )


def reject_fleet_override_for_key(api, parts: list[str], value) -> None:
    if not api.load_fleet():
        return
    nested: dict = {}
    node = nested
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    api._reject_fleet_overrides(nested)


def reconcile_host_after_fleet(api) -> list[str]:
    try:
        api.load()
    except FileNotFoundError:
        return []
    except api.ConfigError:
        pass
    else:
        return []
    host = api.load_host()
    violations = api.fleet_override_violations(host)
    if not violations:
        return []
    for path in violations:
        api._delete_leaf_pruning_empty(host, path)
    api.save(host)
    return violations


def load_reconciling(api) -> dict:
    try:
        return api.load()
    except api.ConfigError:
        api.reconcile_host_after_fleet()
        return api.load()


__all__ = (
    "atomic_dump",
    "clear_load_cache",
    "deep_merge",
    "fleet_override_violations",
    "guard_hq_registry_controller",
    "key_provenance",
    "leaf_paths",
    "load",
    "load_path",
    "load_reconciling",
    "mutation",
    "reconcile_host_after_fleet",
    "reject_fleet_override_for_key",
    "reject_fleet_overrides",
    "save_fleet",
    "save_host",
    "yaml",
    "yaml_lock",
)
