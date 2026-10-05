"""Compatibility collaborators for canonical configuration persistence ports."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from .hq_document_validation import (
    DocumentValidationError,
    validate_settings_mapping,
)
from .modules.config.adapters.yaml_store import RoundTripYamlStore, round_trip_yaml
from .modules.config.application.resolution import deep_merge
from .modules.config.domain.ports import ConfigScope, FleetConfigDocument

_mutation_lock = threading.RLock()
_load_cache_lock = threading.RLock()
_load_cache: dict[tuple[tuple, tuple], str] = {}
#: SQL effective-config derivations keyed by exact (fleet.yaml content digest, HOST JSON).
#: This memoizes pure parsing/validation only, never a snapshot: see ``_load_sql``.
_SQL_EFFECTIVE_MAX = 4
_sql_effective: OrderedDict[tuple[str, str], str] = OrderedDict()
_sql_effective_lock = threading.Lock()


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


def _validate_settings(api, document, *, scope: str) -> None:
    # Preserve the canonical layer-qualified invalid/future version diagnostic
    # before the value-free raw-shape gate rejects the same document.
    api._assert_mutable_schema_version(document, scope)
    try:
        validate_settings_mapping(document, scope=scope)
    except DocumentValidationError as exc:
        raise getattr(api, "ConfigError", ValueError)(str(exc)) from None


def load_host_raw_for_repair(api):
    """Read opaque HOST YAML for an explicit editor/migration, never for admission."""
    return load_path(api, api.config_path())


def load_raw_for_diagnostics(api):
    """Read opaque local source for explicit diagnostics, never a usable view.

    A malformed HOST must not cause a diagnostic command to consult a selected
    SQL backend or treat a local Git mirror as its authority. This port is only
    used by ``config validate`` and the literal warning path after a usable
    read has already failed.
    """
    host = load_host_raw_for_repair(api)
    try:
        selected, _ = _validated_selection(api, host)
    except api.ConfigError:
        # A malformed selector does not authorize treating a local Git mirror
        # as the central source even for an explicit diagnostic.
        return host
    if selected:
        return host
    try:
        fleet = load_path(api, api.fleet_path(), missing_ok=True)
    except YAMLError:
        raise api.ConfigError("fleet configuration YAML syntax invalid") from None
    return deep_merge(fleet, host)


def load_host(api):
    try:
        document = load_host_raw_for_repair(api)
    except YAMLError:
        raise api.ConfigError("host configuration YAML syntax invalid") from None
    _validate_settings(api, document, scope="host")
    return document


def load_fleet_raw_for_repair(api):
    """Read local Git source for repair; SQL remains a qualified committed read."""
    if sql_selected(api):
        return load_fleet(api)
    return load_path(api, api.fleet_path(), missing_ok=True)


def _validated_selection(api, host) -> tuple[bool, str]:
    """Validate the HOST selector and pin the selected backend plus HQ identity."""
    from .beadyard_identity import parse_id
    from .modules.config.contracts import HqSqlConfig

    error_type = getattr(api, "ConfigError", ValueError)
    try:
        hq = host.get("hq", {})
        sql = HqSqlConfig.model_validate(hq.get("sql", {}))
        mode = hq.get("mode", "git")
        selected = sql.enabled
        beadyard_id = hq.get("beadyard_id")
        if beadyard_id is not None:
            beadyard_id = parse_id(beadyard_id)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "mode": mode,
                    "beadyard_id": beadyard_id,
                    **({"sql": sql.model_dump(mode="json")} if selected else {}),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    except (ValueError, TypeError, AttributeError):
        # Minimal facade collaborators also call this port; never leak Pydantic's
        # rejected values, even when they do not expose the public ConfigError.
        raise error_type("invalid SQL HOST bootstrap") from None
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
        selected != active.sql_selected or fingerprint != active.binding_fingerprint
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
    _validate_settings(api, fleet, scope="fleet")
    if (fleet.get("hq") or {}).get("mode") != "dolt-server":
        raise api.ConfigError("committed HQ mode does not match SQL host binding")
    return fleet


def load_fleet(api):
    # A transaction must recheck the live binding; only an ordinary effective
    # read may reuse the HOST it just selected at the facade boundary.
    host = None if _fleet_transaction.get() is not None else _fleet_read_host.get()
    if not sql_selected(api, host):
        try:
            fleet = load_path(api, api.fleet_path(), missing_ok=True)
        except YAMLError:
            raise api.ConfigError("fleet configuration YAML syntax invalid") from None
        _validate_settings(api, fleet, scope="fleet")
        return fleet
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
    with _sql_effective_lock:
        _sql_effective.clear()


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
    merged = api._deep_merge(fleet, host)
    _validate_settings(api, merged, scope="host")
    return merged, False


def _load_fleet_for_host(api, host):
    """Keep the public facade seam without parsing the already-selected HOST again."""
    token = _fleet_read_host.set(host)
    try:
        return api.load_fleet()
    finally:
        _fleet_read_host.reset(token)


def _load_sql(api, host) -> str:
    """Return the effective SQL-selected config as a JSON payload.

    ``host`` is the HOST mapping ``_load_uncached`` already parsed and passed through
    ``sql_selected`` (which also pins an active transaction's binding), so it is not re-read
    twice more here. The committed snapshot is fetched on EVERY call — that read is what
    requalifies the current revision, the rollback floor and the snapshot's finite validity,
    and a revision change or outage surfaces immediately. Only the pure derivation from the
    returned bytes (fleet.yaml parse + settings validation + merge, ~0.5 s) is memoized,
    keyed by the exact fleet.yaml content and HOST, so a byte-identical revision is not
    re-parsed by every seam of a long validation run (bh-931we).
    """
    active = _fleet_transaction.get()
    snapshot = active.snapshot if active is not None else _sql_attachment(api, host)[1]
    document = next((item for item in snapshot.documents if item.path == "fleet.yaml"), None)
    key = None
    if document is not None and isinstance(document.content, str):
        key = (
            hashlib.sha256(document.content.encode("utf-8")).hexdigest(),
            json.dumps(host, separators=(",", ":")),
        )
        with _sql_effective_lock:
            payload = _sql_effective.get(key)
            if payload is not None:
                _sql_effective.move_to_end(key)
                return payload
    fleet = _fleet_document(api, snapshot)
    api._reject_fleet_overrides(host)
    merged = api._deep_merge(fleet, host)
    _validate_settings(api, merged, scope="host")
    payload = json.dumps(merged, separators=(",", ":"))
    if key is not None:
        with _sql_effective_lock:
            _sql_effective[key] = payload
            while len(_sql_effective) > _SQL_EFFECTIVE_MAX:
                _sql_effective.popitem(last=False)
    return payload


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
        return json.loads(_load_sql(api, prepared) if selected else json.dumps(prepared))
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
        return json.loads(_load_sql(api, prepared))
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
    _validate_settings(api, data, scope="host")
    if _fleet_transaction.get() is not None:
        sql_selected(api)  # reject a changed on-disk selector before HOST cleanup
    current_selected = sql_selected(api)
    proposed_selected = sql_selected(api, data)
    if current_selected and not proposed_selected:
        # Disabling the SQL selector is a config-authority transition, not an
        # ordinary HOST edit.  It needs the dedicated verified latest-export
        # procedure; otherwise any `bh config set hq.sql.enabled false` would
        # silently revive an older Git config head.
        raise api.ConfigError("SQL HQ rollback requires a verified latest-export transition")
    if proposed_selected:
        # A host switch is only persisted after a committed qualified readback.
        _fleet_document(api, _sql_attachment(api, data)[1])
    _store(api).save_document(ConfigScope.HOST, data)
    clear_load_cache()


def save_host_legacy_migration(api, data, *, original_bytes: bytes) -> None:
    """Repair only the explicit legacy key migration without a usable invalid read.

    The migration's raw input can contain removed core keys. Its result must be
    valid, and neither the selected backend nor its HOST binding may change.
    """
    api._guard_hq_registry_controller()
    _validate_settings(api, data, scope="host")
    path = api.config_path()
    with mutation(path):
        if path.read_bytes() != original_bytes:
            raise api.ConfigError("HOST changed during legacy key migration")
        current = api.load_host_raw_for_repair()
        selected, binding = _validated_selection(api, current)
        proposed_selected, proposed_binding = _validated_selection(api, data)
        if (selected, binding) != (proposed_selected, proposed_binding):
            raise api.ConfigError("HOST binding changed during legacy key migration")
        if selected:
            _fleet_document(api, _sql_attachment(api, data)[1])
        _store(api).save_document(ConfigScope.HOST, data)
    clear_load_cache()


def _restore_host_bytes(api, original: bytes, *, written: bytes) -> None:
    """Restore the exact prior HOST carrier only if our own write still owns it."""
    path = api.config_path()
    if path.read_bytes() != written:
        raise api.ConfigError("HOST changed during selector readback; manual recovery required")
    old_mode = path.stat().st_mode & 0o777
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, old_mode)
        os.replace(temporary, path)
        temporary = None
        RoundTripYamlStore._fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    clear_load_cache()


def save_host_after_verified_seed(api, data, *, revision: str, beadyard_id: str) -> None:
    """Switch Git→SQL with exact committed readback or restore original HOST bytes."""
    from .beadyard_identity import identity_in_documents

    api._guard_hq_registry_controller()
    _validate_settings(api, data, scope="host")
    if _fleet_transaction.get() is not None:
        raise api.ConfigError("HQ seed switch cannot run inside a fleet transaction")
    with _store(api).transaction(ConfigScope.HOST):
        current = api.load_host()
        if sql_selected(api, current) or not sql_selected(api, data):
            raise api.ConfigError("HQ seed switch requires Git source and proposed SQL")
        original = api.config_path().read_bytes()
        before, proposed = deepcopy(dict(current)), deepcopy(dict(data))
        before_hq, proposed_hq = before.get("hq"), proposed.get("hq")
        if not isinstance(before_hq, dict) or not isinstance(proposed_hq, dict):
            raise api.ConfigError("HQ seed HOST selector invalid")
        before_hq.setdefault("sql", {})["enabled"] = True
        if before != proposed:
            raise api.ConfigError("HQ seed HOST edit contains unrelated changes")
        preflight = _sql_attachment(api, data)[1]
        if (
            preflight.commit_revision != revision
            or identity_in_documents(preflight.documents, required=True) != beadyard_id
        ):
            raise api.ConfigError("HQ seed committed revision or identity changed")
        _store(api).save_document(ConfigScope.HOST, data)
        written = api.config_path().read_bytes()
        clear_load_cache()
        try:
            readback = api.fleet_snapshot()
            if (
                api.load_host() != data
                or readback.commit_revision != revision
                or identity_in_documents(readback.documents, required=True) != beadyard_id
            ):
                raise api.ConfigError("HQ seed selector readback differs")
        except Exception:
            _restore_host_bytes(api, original, written=written)
            raise api.ConfigError(
                "HQ seed selector readback failed; original HOST restored"
            ) from None


def save_host_after_verified_export(api, data, receipt, mirror_plan) -> None:
    """Commit an explicit latest-authority rollback after live dual-backend proof.

    The Git adapter verifies its signed head and witness. The SQL adapter verifies
    its current protected head and local anti-rollback floor. A local HOST lock
    serializes this machine's writers; the operator must separately suspend all
    central SQL publishers before export and keep them suspended through switch.
    """
    from .hq_control_plane import attach_fleet_config
    from .hq_transition import (
        GitMirrorPlan,
        RollbackReceipt,
        TransitionError,
        receipt_for_export,
        verify_git_export_lineage,
        verify_git_mirror,
        verify_writer_suspension,
    )

    api._guard_hq_registry_controller()
    _validate_settings(api, data, scope="host")
    if _fleet_transaction.get() is not None:
        raise api.ConfigError("HQ rollback cannot run inside a fleet transaction")
    if not isinstance(receipt, RollbackReceipt):
        raise api.ConfigError("HQ rollback requires a typed latest-export receipt")
    if not isinstance(mirror_plan, GitMirrorPlan) or (
        mirror_plan.source_revision,
        mirror_plan.export_revision,
    ) != (receipt.source_revision, receipt.export_revision):
        raise api.ConfigError("HQ rollback requires a verified latest Git mirror plan")
    with _store(api).transaction(ConfigScope.HOST):
        current = api.load_host()
        if not sql_selected(api, current) or sql_selected(api, data):
            raise api.ConfigError("HQ rollback requires selected SQL and proposed Git")
        before, proposed = deepcopy(dict(current)), deepcopy(dict(data))
        before_hq, proposed_hq = before.get("hq"), proposed.get("hq")
        if not isinstance(before_hq, dict) or not isinstance(proposed_hq, dict):
            raise api.ConfigError("HQ rollback HOST selector invalid")
        if "mode" in before_hq:
            before_hq["mode"] = "git"
        before_hq.setdefault("sql", {})["enabled"] = False
        if before != proposed:
            raise api.ConfigError("HQ rollback HOST edit contains unrelated changes")
        original_bytes = api.config_path().read_bytes()

        def live_receipt():
            sql_snapshot = _sql_attachment(api, current)[1]
            git_bootstrap = {
                "mode": "git",
                "authority_anchor": current["hq"].get("authority_anchor"),
            }
            git_store, git_snapshot = attach_fleet_config(api.hq_dir(), bootstrap=git_bootstrap)
            verify_git_export_lineage(git_store, receipt)
            policy = git_store.plane._policy()
            verify_writer_suspension(
                receipt.suspension,
                operator_signers=policy["operator_signers"],
                ssh_keygen=policy["executables"]["ssh_keygen"]["path"],
            )
            verify_git_mirror(mirror_plan, git_snapshot.documents)
            return receipt_for_export(
                sql_snapshot,
                git_snapshot,
                original_host_bytes=original_bytes,
                proposed_host=data,
                suspension=receipt.suspension,
                export_expected_parent=receipt.export_expected_parent,
                export_publication_id=receipt.export_publication_id,
            )

        try:
            if live_receipt() != receipt:
                raise TransitionError("current SQL or signed Git export changed")
            _store(api).save_document(ConfigScope.HOST, data)
            clear_load_cache()
            if api.load_host() != data or live_receipt() != receipt:
                raise TransitionError("HQ rollback readback or authority changed")
            # The ordinary facade must see the signed Git export after selection.
            if api.load_fleet() != _fleet_document_git(api, receipt, current):
                raise TransitionError("HQ rollback public Git readback differs")
            from .modules.config.adapters.workspace_selection import selected_git_sources

            selected_paths = selected_git_sources(
                api.load(), root=api._workspace_root_for_transition(), hq_dir=api.hq_dir()
            )
            selected = tuple((path.name, path.read_text()) for path in selected_paths)
            expected_workspace = tuple(
                (item.path, item.content)
                for item in attach_fleet_config(
                    api.hq_dir(),
                    bootstrap={
                        "mode": "git",
                        "authority_anchor": current["hq"].get("authority_anchor"),
                    },
                )[1].documents
                if item.path == "workspace.toml"
                or (item.path.startswith("workspace-") and item.path.endswith(".toml"))
            )
            if selected != expected_workspace:
                raise TransitionError("HQ rollback public workspace resolver differs")
        except Exception as exc:
            # A post-save failure restores the original selected SQL bootstrap.
            # The external writer freeze is still required across this boundary.
            if api.config_path().read_bytes() != original_bytes:
                path = api.config_path()
                old_mode = path.stat().st_mode & 0o777
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                        stream.write(original_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, old_mode)
                    os.replace(temporary, path)
                    temporary = None
                    RoundTripYamlStore._fsync_directory(path.parent)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                clear_load_cache()
            if isinstance(exc, TransitionError):
                raise api.ConfigError(str(exc)) from None
            raise api.ConfigError("HQ rollback verification unavailable") from None


def _fleet_document_git(api, receipt, previous_host):
    from .hq_control_plane import attach_fleet_config

    bootstrap = {"mode": "git", "authority_anchor": previous_host["hq"].get("authority_anchor")}
    snapshot = attach_fleet_config(api.hq_dir(), bootstrap=bootstrap)[1]
    if snapshot.commit_revision != receipt.export_revision:
        raise api.ConfigError("signed Git export head changed")
    document = next((item for item in snapshot.documents if item.path == "fleet.yaml"), None)
    if document is None:
        raise api.ConfigError("signed Git fleet document missing")
    with api._yaml_lock:
        return api._yaml.load(StringIO(document.content))


def save_fleet(api, data) -> None:
    _validate_settings(api, data, scope="fleet")
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
