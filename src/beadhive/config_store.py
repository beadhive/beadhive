"""Compatibility collaborators for canonical configuration persistence ports."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from .modules.config.adapters.yaml_store import RoundTripYamlStore, round_trip_yaml
from .modules.config.application.resolution import deep_merge
from .modules.config.domain.ports import ConfigScope, FleetConfigDocument

_mutation_lock = threading.RLock()
_load_cache_lock = threading.RLock()
_load_cache: dict[tuple[tuple, tuple], str] = {}


@dataclass(frozen=True)
class _FleetTransaction:
    sql_selected: bool
    binding_fingerprint: str
    store: object | None = None
    snapshot: object | None = None


_fleet_transaction: ContextVar[_FleetTransaction | None] = ContextVar(
    "fleet_transaction", default=None
)
_fleet_read_host: ContextVar[Mapping | None] = ContextVar("fleet_read_host", default=None)


def fleet_transaction_active() -> bool:
    active = _fleet_transaction.get()
    return active is not None and active.sql_selected


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


def _validated_selection(api, host) -> tuple[bool, str]:
    """Validate the HOST selector and fingerprint the exact SQL bootstrap."""
    from .modules.config.contracts import HqSqlConfig

    error_type = getattr(api, "ConfigError", ValueError)
    try:
        hq = host.get("hq", {})
        sql = HqSqlConfig.model_validate(hq.get("sql", {}))
        mode = hq.get("mode", "git")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"mode": mode, "sql": sql.model_dump(mode="json")},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    except (ValueError, TypeError, AttributeError):
        # Minimal facade collaborators also call this port; never leak Pydantic's
        # rejected values, even when they do not expose the public ConfigError.
        raise error_type("invalid SQL HOST bootstrap") from None
    selected = sql.enabled
    if mode not in ("git", "dolt-server") or (not selected and mode != "git"):
        raise error_type("unsupported HQ configuration mode")
    return selected, fingerprint


def sql_selected(api, host=None) -> bool:
    """The HOST switch alone selects SQL; a mutation pins its original binding."""
    if host is None:
        try:
            host = api.load_host()
        except FileNotFoundError:
            host = CommentedMap()
    selected, fingerprint = _validated_selection(api, host)
    active = _fleet_transaction.get()
    if active is not None and (
        selected != active.sql_selected or (selected and fingerprint != active.binding_fingerprint)
    ):
        raise api.ConfigError("selected HOST binding changed during fleet transaction")
    return selected


def _sql_attachment(api, host=None):
    from .hq_control_plane import attach_fleet_config

    host = host if host is not None else api.load_host()
    return attach_fleet_config(bootstrap=host.get("hq") or {})


def _fleet_document(api, snapshot):
    document = next((item for item in snapshot.documents if item.path == "fleet.yaml"), None)
    if document is None:
        raise api.ConfigError("committed SQL fleet document missing")
    with api._yaml_lock:
        fleet = api._yaml.load(StringIO(document.content))
    if not isinstance(fleet, Mapping):
        raise api.ConfigError("committed SQL fleet document invalid")
    if (fleet.get("hq") or {}).get("mode") != "dolt-server":
        raise api.ConfigError("committed HQ mode does not match SQL host binding")
    return fleet


def load_fleet(api):
    # A transaction must recheck the live binding; only an ordinary effective
    # read may reuse the HOST it just selected at the facade boundary.
    host = None if _fleet_transaction.get() is not None else _fleet_read_host.get()
    if not sql_selected(api, host):
        return load_path(api, api.fleet_path(), missing_ok=True)
    active = _fleet_transaction.get()
    snapshot = active.snapshot if active is not None else _sql_attachment(api)[1]
    return _fleet_document(api, snapshot)


def fleet_snapshot(api):
    """Return current committed SQL documents and their provenance."""
    if not sql_selected(api):
        return None
    active = _fleet_transaction.get()
    return active.snapshot if active is not None else _sql_attachment(api)[1]


def publish_fleet_document(api, path: str, content: str | None):
    """Replace/delete one committed document against the transaction's first head."""
    active = _fleet_transaction.get()
    if active is None or not active.sql_selected:
        raise api.ConfigError("SQL document publication requires an original-revision transaction")
    sql_selected(api)  # no document or file write after a HOST backend/binding switch
    store, snapshot = active.store, active.snapshot
    found = False
    documents = []
    for item in snapshot.documents:
        if item.path == path:
            found = True
            if content is not None:
                documents.append(FleetConfigDocument(path, content))
        else:
            documents.append(item)
    if content is not None and not found:
        documents.append(FleetConfigDocument(path, content))
    if content is None and not found:
        raise FileNotFoundError(path)
    published = store.publish_snapshot(tuple(documents), expected_revision=snapshot.commit_revision)
    clear_load_cache()
    return published


@contextmanager
def fleet_mutation(api):
    active = _fleet_transaction.get()
    if active is not None:
        if active.sql_selected:
            raise api.ConfigError("nested fleet revision transaction")
        sql_selected(api)  # retain the original Git selection in nested legacy edits
        with mutation(api.fleet_path()):
            yield
        return
    try:
        host = api.load_host()
    except FileNotFoundError:
        host = CommentedMap()
    selected = sql_selected(api, host)
    _, fingerprint = _validated_selection(api, host)
    store, snapshot = _sql_attachment(api, host) if selected else (None, None)
    token = _fleet_transaction.set(_FleetTransaction(selected, fingerprint, store, snapshot))
    try:
        if selected:
            yield
        else:
            with mutation(api.fleet_path()):
                yield
    finally:
        _fleet_transaction.reset(token)


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
    try:
        host = api.load_host()
    except FileNotFoundError:
        fleet = _load_fleet_for_host(api, CommentedMap())
        if not fleet:
            raise
        return fleet, False
    if sql_selected(api, host):
        return host, True
    fleet = _load_fleet_for_host(api, host)
    if not fleet:
        return host, False
    api._reject_fleet_overrides(host)
    return api._deep_merge(fleet, host), False


def _load_fleet_for_host(api, host):
    """Keep the public facade seam without parsing the already-selected HOST again."""
    token = _fleet_read_host.set(host)
    try:
        return api.load_fleet()
    finally:
        _fleet_read_host.reset(token)


def _load_sql(api, host):
    snapshot = fleet_snapshot(api)
    fleet = _fleet_document(api, snapshot)
    api._reject_fleet_overrides(host)
    return api._deep_merge(fleet, host)


def load(api):
    """Load an isolated effective config; memoize unchanged filesystem layers only.

    The memo stores JSON rather than the mutable ``CommentedMap`` returned by ruamel.  Each
    caller gets a fresh plain mapping, so one request/thread cannot corrupt another caller's
    view. Path and stat metadata make Git filesystem edits self-invalidating; SQL reads
    always qualify a current committed snapshot. Comment-preserving mutations still use
    ``load_host`` / ``load_fleet`` directly.
    """
    if _fleet_transaction.get() is not None:
        prepared, selected = _load_uncached(api)
        return json.loads(json.dumps(_load_sql(api, prepared) if selected else prepared))
    # Filesystem memoization applies only to Git. An authenticated SQL read
    # must requalify its finite validity and committed revision on every call.
    key = None
    if hasattr(api, "fleet_path") and hasattr(api, "config_path"):
        try:
            key = (_file_signature(api.fleet_path()), _file_signature(api.config_path()))
        except OSError:
            # An unreadable optional local fleet path cannot prevent the HOST
            # selector from choosing the authenticated SQL service.
            pass
    with _load_cache_lock:
        payload = _load_cache.get(key) if key is not None else None
        if payload is None:
            prepared, sql_enabled = _load_uncached(api)
            if not sql_enabled:
                payload = json.dumps(prepared, separators=(",", ":"))
            if payload is not None and key is not None:
                _load_cache.clear()  # only the current filesystem revision is useful
                _load_cache[key] = payload
    if payload is None:
        # No global effective-cache mutex is held during broker/SQL I/O.
        return json.loads(json.dumps(_load_sql(api, prepared)))
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
    if _fleet_transaction.get() is not None:
        sql_selected(api)  # reject a changed on-disk selector before HOST cleanup
    if sql_selected(api, data):
        # A host switch is only persisted after a committed qualified readback.
        _fleet_document(api, _sql_attachment(api, data)[1])
    _store(api).save_document(ConfigScope.HOST, data)
    clear_load_cache()


def save_fleet(api, data) -> None:
    if not sql_selected(api):
        _store(api).save_document(ConfigScope.FLEET, data)
        clear_load_cache()
        return
    active = _fleet_transaction.get()
    if active is None:
        raise api.ConfigError("SQL fleet publication requires an original-revision transaction")
    store, snapshot = active.store, active.snapshot
    stream = StringIO()
    with api._yaml_lock:
        api._yaml.dump(data, stream)
    documents = tuple(
        FleetConfigDocument(
            item.path, stream.getvalue() if item.path == "fleet.yaml" else item.content
        )
        for item in snapshot.documents
    )
    _fleet_document(api, replace(snapshot, documents=documents))
    published = store.publish_snapshot(documents, expected_revision=snapshot.commit_revision)
    _fleet_document(api, published)
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
