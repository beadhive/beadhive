"""Selected fleet roster documents; pure manifest contracts remain in hosts."""

from __future__ import annotations

from contextlib import nullcontext
from functools import wraps
from io import StringIO
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML

from . import config
from . import hosts as _files

HostManifest = _files.HostManifest
IdentityMechanism = _files.IdentityMechanism
ManifestError = _files.ManifestError
HOST_ROLES = _files.HOST_ROLES
IDENTITY_MECHANISM_KINDS = _files.IDENTITY_MECHANISM_KINDS
canonical_role = _files.canonical_role
manifest_path = _files.manifest_path


def original_revision(fn):
    """Keep the preflight roster read and eventual write on one SQL revision."""

    @wraps(fn)
    def selected_transaction(*args, **kwargs):
        transaction = (
            config._write_transaction(config.SCOPE_FLEET)
            if config.fleet_sql_selected()
            else nullcontext()
        )
        with transaction:
            return fn(*args, **kwargs)

    return selected_transaction


def _document_path(host_id: str) -> str:
    return f"hosts/{host_id}.yaml"


def exists(hq_dir: Path, host_id: str) -> bool:
    if not config.fleet_sql_selected():
        return manifest_path(hq_dir, host_id).exists()
    snapshot = config.fleet_snapshot()
    return any(item.path == _document_path(host_id) for item in snapshot.documents)


def manifest_paths(hq_dir: Path) -> list[Path]:
    if not config.fleet_sql_selected():
        directory = _files.hosts_dir(hq_dir)
        return sorted(directory.glob("*.yaml")) if directory.is_dir() else []
    snapshot = config.fleet_snapshot()
    return sorted(
        (
            hq_dir / item.path
            for item in snapshot.documents
            if item.path.startswith("hosts/") and item.path.endswith(".yaml")
        ),
        key=lambda path: path.name,
    )


def load(hq_dir: Path, host_id: str) -> HostManifest:
    if not config.fleet_sql_selected():
        return _files.load(hq_dir, host_id)
    path = manifest_path(hq_dir, host_id)
    snapshot = config.fleet_snapshot()
    item = next((doc for doc in snapshot.documents if doc.path == _document_path(host_id)), None)
    if item is None:
        raise FileNotFoundError(f"no host manifest for {host_id!r} at {path}")
    raw = YAML(typ="safe").load(item.content) or {}
    if isinstance(raw, dict) and "state" not in raw:
        raw = {**raw, "state": "active"}
    try:
        return HostManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(_files._format_error(path, exc)) from exc


def save(hq_dir: Path, manifest: HostManifest) -> Path:
    if not config.fleet_sql_selected():
        return _files.save(hq_dir, manifest)
    stream = StringIO()
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.dump(manifest.model_dump(mode="json"), stream)
    transaction = (
        nullcontext()
        if config.fleet_transaction_active()
        else config._write_transaction(config.SCOPE_FLEET)
    )
    with transaction:
        config.publish_fleet_document(_document_path(manifest.host_id), stream.getvalue())
    return manifest_path(hq_dir, manifest.host_id)


def remove(hq_dir: Path, host_id: str) -> Path:
    if not config.fleet_sql_selected():
        return _files.remove(hq_dir, host_id)
    transaction = (
        nullcontext()
        if config.fleet_transaction_active()
        else config._write_transaction(config.SCOPE_FLEET)
    )
    with transaction:
        config.publish_fleet_document(_document_path(host_id), None)
    return manifest_path(hq_dir, host_id)
