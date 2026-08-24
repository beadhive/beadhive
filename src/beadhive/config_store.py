"""Round-trip host/fleet config storage and partition reconciliation."""

from __future__ import annotations

import copy
import fcntl
import os
import tempfile
import threading
from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

_mutation_lock = threading.RLock()

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)
yaml.width = 4096
yaml_lock = threading.Lock()


@contextmanager
def mutation(path: Path):
    """Serialize a complete read/modify/write transaction across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with _mutation_lock, lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_path(api, path: Path, *, missing_ok: bool = False):
    if not path.is_file():
        if missing_ok:
            return CommentedMap()
        raise FileNotFoundError(
            f"{api.BINARY_ALIAS} config not found at {path}\n"
            f"  scaffold it with:  {api.BINARY_ALIAS} config init"
        )
    text = path.read_text()
    with api._yaml_lock:
        return api._yaml.load(text) or CommentedMap()


def leaf_paths(node, prefix: str = ""):
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from leaf_paths(value, f"{prefix}.{key}" if prefix else str(key))
    elif prefix:
        yield prefix


def fleet_override_violations(host) -> list[str]:
    from . import config_partition

    return [
        path
        for path in leaf_paths(host)
        if config_partition.partition_of(path) == config_partition.FLEET
        and not config_partition.is_host_overridable(path)
    ]


def deep_merge(base, over):
    merged = copy.deepcopy(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(current, MutableMapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load(api):
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
    """Replace one YAML file atomically; a failed dump never truncates the live file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            tmp_name = stream.name
            with api._yaml_lock:
                api._yaml.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        tmp_name = None
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


def save_host(api, data) -> None:
    api._guard_hq_registry_controller()
    atomic_dump(api, data, api.config_path())


def save_fleet(api, data) -> None:
    atomic_dump(api, data, api.fleet_path())


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
