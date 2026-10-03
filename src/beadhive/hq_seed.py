"""Explicit, restartable Git-source to dedicated Dolt config initialization.

Planning reads the selected local sources but never changes either backend.
Application requires an owned durable intent and an exact fresh-schema parent;
it does not switch HOST, enroll a frame, or touch any Beads database.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ruamel.yaml import YAML

from .beadyard_identity import BeadyardIdentityError, identity_in_documents, parse_id
from .hq_document_validation import DocumentValidationError
from .hq_sql_config import (
    InitialSeedReceipt,
    PublicationUnknown,
    SqlConfigError,
    _digest,
    _validate,
)
from .hq_sql_deadline import flock_until
from .hq_transition import (
    GitMirrorPlan,
    MirrorTarget,
    RollbackReceipt,
    TransitionError,
    WriterSuspensionEvidence,
    parse_receipt_json,
    project_latest_to_git,
    receipt_for_export,
    receipt_json,
    verify_git_export_lineage,
    verify_git_mirror,
    verify_writer_suspension,
)
from .modules.config.domain.ports import FleetConfigDocument


class SeedError(ValueError):
    """A seed plan, intent, source, or destination did not match its original."""


@dataclass(frozen=True)
class GitExportIntent:
    """Original immutable rollback publication request, durable before signed Git push."""

    publication_id: str
    source_backend: str
    source_generation: str
    source_revision: str
    source_documents_sha256: str
    export_expected_parent: str
    beadyard_id: str
    original_host_sha256: str
    proposed_host_sha256: str
    suspension_sha256: str


@dataclass(frozen=True)
class SourceWitness:
    document: str
    source: Path = field(repr=False)
    sha256: str
    size: int


@dataclass(frozen=True)
class SeedPlan:
    hq_dir: Path = field(repr=False)
    fleet_path: Path = field(repr=False)
    documents: tuple[FleetConfigDocument, ...] = field(repr=False)
    sources: tuple[SourceWitness, ...] = field(repr=False)
    source_git_head: str
    source_status_sha256: str
    source_dirty: bool
    source_manifest_id: str
    manifest_id: str
    host_relocation_paths: tuple[str, ...]
    host_relocation_sha256: str
    semantic_parity: bool
    documents_sha256: str
    beadyard_id: str | None
    expected_schema_parent: str | None
    backend_identity: str | None
    generation: str | None
    destination_empty: bool | None
    issues: tuple[str, ...]

    def report(self) -> dict[str, object]:
        """Value-free operator review projection; raw bytes and paths stay private."""
        counts = {"workspace": 0, "host": 0, "hive": 0, "trust": 0}
        for document in self.documents:
            if document.path.startswith("workspace"):
                counts["workspace"] += 1
            elif document.path.startswith("hosts/"):
                counts["host"] += 1
            elif document.path.startswith("hives/"):
                counts["hive"] += 1
            elif document.path == "allowed_signers":
                counts["trust"] += 1
        return {
            "source_git_head": self.source_git_head,
            "source_dirty": self.source_dirty,
            "source_status_sha256": self.source_status_sha256,
            "manifest_id": self.manifest_id,
            "source_manifest_id": self.source_manifest_id,
            "host_relocation_paths": self.host_relocation_paths,
            "semantic_parity": self.semantic_parity,
            "documents_sha256": self.documents_sha256,
            "beadyard_id": self.beadyard_id,
            "source_documents": [
                {"document": row.document, "sha256": row.sha256, "bytes": row.size}
                for row in self.sources
            ],
            "counts": counts,
            "target_schema_parent": self.expected_schema_parent,
            "target_empty": self.destination_empty,
            "backend_identity": self.backend_identity,
            "generation": self.generation,
            "issues": list(self.issues),
        }


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise SeedError("source HQ Git main unavailable")
    return result.stdout


def _read_source(path: Path, logical: str) -> tuple[FleetConfigDocument, SourceWitness]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            raise SeedError("seed source document is not a bounded regular file")
        body = path.read_bytes()
        content = body.decode("utf-8")
    except (OSError, UnicodeError):
        raise SeedError("seed source document unavailable or invalid encoding") from None
    return (
        FleetConfigDocument(logical, content),
        SourceWitness(logical, path, hashlib.sha256(body).hexdigest(), len(body)),
    )


def _central_fleet(source: FleetConfigDocument, host_snapshot: dict | None):
    """Project only named HOST identity leaves, with typed semantic parity."""
    yaml = YAML()
    try:
        raw = yaml.load(source.content)
        if not isinstance(raw, dict):
            raise ValueError()
        original = deepcopy(raw)
        host = deepcopy(dict(host_snapshot or {}))
        projected_host = deepcopy(host)
        from .modules.config.application.partition import HOST, partition_of

        def leaves(node, prefix=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    key_path = f"{prefix}.{key}" if prefix else str(key)
                    yield from leaves(value, key_path)
            elif prefix:
                yield prefix

        paths = tuple(sorted(path for path in leaves(raw) if partition_of(path) == HOST))
        issues = []
        values = {}
        for path in paths:
            if path not in ("work.identity.name", "work.identity.email"):
                issues.append(f"HOST-owned {path} requires reviewed relocation policy")
                continue
            keys = path.split(".")
            node = raw
            for key in keys[:-1]:
                node = node[key]
            value = node.pop(keys[-1])
            values[path] = value
            actual = host
            for key in keys[:-1]:
                actual = actual.get(key, {}) if isinstance(actual, dict) else {}
            if keys[-1] in actual and actual[keys[-1]] != value:
                issues.append(f"HOST-owned {path} conflicts with current local HOST value")
            elif keys[-1] not in actual:
                issues.append(f"HOST-owned {path} requires explicit local HOST relocation")
            destination = projected_host
            for key in keys[:-1]:
                destination = destination.setdefault(key, {})
            destination[keys[-1]] = value
        if isinstance(raw.get("work"), dict) and not raw["work"].get("identity"):
            raw["work"].pop("identity", None)
            if not raw["work"]:
                raw.pop("work")
        hq = raw.get("hq")
        if hq is None:
            raw["hq"] = {"mode": "dolt-server"}
        elif isinstance(hq, dict) and hq.get("mode", "git") in ("git", "dolt-server"):
            hq["mode"] = "dolt-server"
        else:
            raise ValueError()
        from io import StringIO

        out = StringIO()
        yaml.dump(raw, out)
        from .modules.config.application.resolution import ResolutionInputs, resolve_config

        before = resolve_config(ResolutionInputs(fleet=original, host=host)).settings.model_dump(
            mode="json", by_alias=True
        )
        after = resolve_config(
            ResolutionInputs(fleet=raw, host=projected_host)
        ).settings.model_dump(mode="json", by_alias=True)
        before.get("hq", {}).pop("mode", None)
        after.get("hq", {}).pop("mode", None)
        parity = before == after
        if not parity:
            issues.append("resolved settings parity differs after HOST relocation")
        relocation_digest = hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return (
            FleetConfigDocument(source.path, out.getvalue()),
            paths,
            relocation_digest,
            parity,
            tuple(issues),
        )
    except Exception:
        raise SeedError("source fleet HQ mode or HOST relocation cannot be projected") from None


def plan(
    *,
    hq_dir: Path,
    fleet_path: Path,
    workspace_sources,
    destination=None,
    host_snapshot: dict | None = None,
) -> SeedPlan:
    """Capture actual selected raw sources and the target's committed schema HEAD.

    ``workspace_sources`` is the ordered result of the canonical resolver. Each
    selected source must still be backed by its exact file; no HQ fallback is
    silently substituted for an external selected source.
    """
    root = Path(hq_dir)
    if not (root / ".git").is_dir():
        raise SeedError("source HQ Git checkout unavailable")
    git_head = _git(root, "rev-parse", "main").decode().strip()
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    issues: list[str] = []
    documents: list[FleetConfigDocument] = []
    witnesses: list[SourceWitness] = []

    def add(path: Path, logical: str):
        document, witness = _read_source(path, logical)
        documents.append(document)
        witnesses.append(witness)

    for name in ("beadyard.json", "fleet.yaml"):
        source = Path(fleet_path) if name == "fleet.yaml" else root / name
        if not source.is_file() and name == "beadyard.json":
            issues.append("source beadyard identity missing; explicit adoption required")
            continue
        add(source, name)
    selected = tuple(workspace_sources)
    for source in selected:
        if source.path is None:
            raise SeedError("seed requires file-backed selected workspace sources")
        add(Path(source.path), source.name)
        if documents[-1].content != source.content:
            raise SeedError("selected workspace source changed during capture")
    for path in sorted((root / "hosts").glob("*.yaml")):
        add(path, f"hosts/{path.name}")
    for path in sorted((root / "hives").glob("*/*/*.yaml")):
        add(path, str(path.relative_to(root)))
    if (root / "allowed_signers").exists():
        add(root / "allowed_signers", "allowed_signers")
    raw = tuple(documents)
    try:
        owner = identity_in_documents(raw, required=True)
        if _git(root, "show", "main:beadyard.json") != next(
            doc.content.encode() for doc in raw if doc.path == "beadyard.json"
        ):
            issues.append("working and committed HQ identity differ")
    except (BeadyardIdentityError, SeedError, StopIteration):
        owner = None
        issues.append("source committed beadyard identity invalid or unavailable")
    try:
        projected, host_paths, relocation_digest, parity, relocation_issues = _central_fleet(
            next(doc for doc in raw if doc.path == "fleet.yaml"), host_snapshot
        )
        issues.extend(relocation_issues)
        central = tuple(projected if doc.path == "fleet.yaml" else doc for doc in raw)
        _validate(central)
    except (SeedError, SqlConfigError, DocumentValidationError) as exc:
        central = raw
        issues.append(str(exc))
        host_paths, relocation_digest, parity = (), hashlib.sha256(b"{}").hexdigest(), False
    except Exception:
        central = raw
        issues.append("source fleet projection or schema invalid")
        host_paths, relocation_digest, parity = (), hashlib.sha256(b"{}").hexdigest(), False
    target = None
    if destination is not None:
        try:
            target = destination.inspect_initial_destination()
            if not target.empty:
                issues.append("target config database is already initialized")
        except SqlConfigError:
            issues.append("target config schema unavailable or not clean")
    source_body = json.dumps(
        {
            "git_head": git_head,
            "status_sha256": hashlib.sha256(status).hexdigest(),
            "sources": [(row.document, str(row.source), row.sha256, row.size) for row in witnesses],
            "documents_sha256": _digest(central),
            "beadyard_id": owner,
            "projection": "round-trip fleet hq.mode=dolt-server; all other raw bytes retained",
            "host_relocation_paths": host_paths,
            "host_relocation_sha256": relocation_digest,
            "semantic_parity": parity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    source_manifest_id = hashlib.sha256(source_body).hexdigest()
    manifest_body = json.dumps(
        {
            "source_manifest_id": source_manifest_id,
            "target_schema_parent": target.schema_revision if target else None,
            "target_backend": target.backend_identity if target else None,
            "target_generation": target.generation if target else None,
            "target_empty": target.empty if target else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return SeedPlan(
        root,
        Path(fleet_path),
        central,
        tuple(witnesses),
        git_head,
        hashlib.sha256(status).hexdigest(),
        bool(status),
        source_manifest_id,
        hashlib.sha256(manifest_body).hexdigest(),
        host_paths,
        relocation_digest,
        parity,
        _digest(central),
        owner,
        target.schema_revision if target else None,
        target.backend_identity if target else None,
        target.generation if target else None,
        target.empty if target else None,
        tuple(issues),
    )


def plan_current(destination=None) -> SeedPlan:
    """Capture the canonical configured Git source resolver before any switch."""
    from . import config, gitworkspace

    if config.fleet_sql_selected():
        raise SeedError("seed source must remain explicitly selected Git")
    cfg = config.load()
    return plan(
        hq_dir=config.hq_dir(),
        fleet_path=config.fleet_path(),
        workspace_sources=gitworkspace.workspace_sources(cfg),
        destination=destination,
        host_snapshot=config.load_host(),
    )


_INTENT_KEYS = {
    "version",
    "source_manifest_id",
    "manifest_id",
    "publication_id",
    "expected_schema_parent",
    "documents_sha256",
    "document_count",
    "beadyard_id",
    "backend_identity",
    "generation",
    "state",
    "committed_revision",
}


def _intent_path(path: Path) -> Path:
    path = Path(path)
    parent = path.parent
    if not path.is_absolute() or path.is_symlink() or parent.is_symlink():
        raise SeedError("seed intent path must be an owned absolute regular-file path")
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SeedError("seed intent directory custody changed")
    return path


@contextmanager
def _intent_lock(path: Path):
    path = _intent_path(path)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SeedError("seed intent lock custody changed")
        with os.fdopen(fd, "a+b", closefd=False) as handle:
            try:
                flock_until(handle, time.monotonic() + 10)
            except TimeoutError:
                raise SeedError("seed intent custody wait exceeded deadline") from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_intent(
    path: Path, record: dict[str, object], *, create: bool = False, max_bytes: int = 4096
) -> None:
    path = _intent_path(path)
    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    if len(body) > max_bytes:
        raise SeedError("seed intent exceeds bounded journal size")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        if create:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                raise SeedError("seed intent already exists") from None
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_intent(path: Path) -> dict[str, object] | None:
    path = _intent_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise SeedError("seed intent unavailable") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise SeedError("seed intent custody changed")
        body = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        record = json.loads(body)
        if not isinstance(record, dict) or set(record) != _INTENT_KEYS:
            raise ValueError()
        if not isinstance(record["publication_id"], str):
            raise ValueError()
        publication_id = uuid.UUID(record["publication_id"])
        if (
            type(record["version"]) is not int
            or record["version"] != 1
            or publication_id.version != 4
            or str(publication_id) != record["publication_id"]
            or record["state"] not in ("prepared", "submitted", "confirmed")
            or any(
                not isinstance(record[key], str) or not record[key]
                for key in (
                    "source_manifest_id",
                    "manifest_id",
                    "expected_schema_parent",
                    "documents_sha256",
                    "beadyard_id",
                    "backend_identity",
                    "generation",
                )
            )
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", record[key])
                for key in ("source_manifest_id", "manifest_id", "documents_sha256")
            )
            or type(record["document_count"]) is not int
            or record["document_count"] < 1
            or not isinstance(record["committed_revision"], str)
            or (record["state"] == "confirmed") != bool(record["committed_revision"])
        ):
            raise ValueError()
        parse_id(record["beadyard_id"])
    except (ValueError, TypeError, KeyError, AttributeError, BeadyardIdentityError):
        raise SeedError("seed intent invalid") from None
    approval = hashlib.sha256(
        json.dumps(
            {
                "source_manifest_id": record["source_manifest_id"],
                "target_schema_parent": record["expected_schema_parent"],
                "target_backend": record["backend_identity"],
                "target_generation": record["generation"],
                "target_empty": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if approval != record["manifest_id"]:
        raise SeedError("seed intent approval digest invalid")
    return record


def prepare(plan: SeedPlan, intent_path: Path) -> dict[str, object]:
    """Persist one original UUID before any potentially ambiguous SQL action."""
    if plan.issues or plan.beadyard_id is None or not plan.destination_empty:
        raise SeedError("seed plan has unresolved source or destination conflicts")
    if not plan.expected_schema_parent or not plan.backend_identity or not plan.generation:
        raise SeedError("seed target schema identity unavailable")
    record: dict[str, object] = {
        "version": 1,
        "source_manifest_id": plan.source_manifest_id,
        "manifest_id": plan.manifest_id,
        "publication_id": str(uuid.uuid4()),
        "expected_schema_parent": plan.expected_schema_parent,
        "documents_sha256": plan.documents_sha256,
        "document_count": len(plan.documents),
        "beadyard_id": plan.beadyard_id,
        "backend_identity": plan.backend_identity,
        "generation": plan.generation,
        "state": "prepared",
        "committed_revision": "",
    }
    with _intent_lock(intent_path):
        existing = _read_intent(intent_path)
        if existing is not None:
            _match_intent(plan, existing)
            return existing
        _write_intent(intent_path, record, create=True)
        return record


def _transition(
    path: Path, publication_id: str, state: str, committed: str = ""
) -> dict[str, object]:
    with _intent_lock(path):
        current = _read_intent(path)
        if current is None or current["publication_id"] != publication_id:
            raise SeedError("seed intent identity changed")
        if current["state"] == "confirmed":
            if state == "confirmed" and committed != current["committed_revision"]:
                raise SeedError("seed receipt conflicts with durable original")
            return current
        if state == "submitted" and current["state"] == "submitted":
            return current
        if state not in ("submitted", "confirmed"):
            raise SeedError("seed intent transition invalid")
        updated = {**current, "state": state, "committed_revision": committed}
        _write_intent(path, updated)
        return updated


def _match_intent(plan: SeedPlan, record: dict[str, object]) -> None:
    expected = {
        "source_manifest_id": plan.source_manifest_id,
        "documents_sha256": plan.documents_sha256,
        "document_count": len(plan.documents),
        "beadyard_id": plan.beadyard_id,
    }
    if plan.destination_empty:
        expected.update(
            {
                "manifest_id": plan.manifest_id,
                "expected_schema_parent": plan.expected_schema_parent,
                "backend_identity": plan.backend_identity,
                "generation": plan.generation,
            }
        )
    elif plan.backend_identity is not None and plan.generation is not None:
        expected.update(
            {
                "backend_identity": plan.backend_identity,
                "generation": plan.generation,
            }
        )
    if any(record[key] != value for key, value in expected.items()):
        raise SeedError("seed source, identity or original destination changed")


def reconcile(plan: SeedPlan, intent_path: Path, store) -> InitialSeedReceipt | None:
    """Recover only the exact original publication, even after later edits."""
    record = _read_intent(intent_path)
    if record is None:
        raise SeedError("seed intent missing")
    # A recovery plan may now observe a nonempty target. Its source and original
    # destination identity still have to match the durable intent.
    _match_intent(plan, record)
    try:
        receipt = store.recover_initial_snapshot(
            publication_id=record["publication_id"],
            expected_schema_parent=record["expected_schema_parent"],
            documents_sha256=record["documents_sha256"],
            document_count=record["document_count"],
            beadyard_id=record["beadyard_id"],
        )
    except SqlConfigError:
        raise SeedError("original seed receipt unavailable or conflicting") from None
    if receipt is None:
        return None
    if record["committed_revision"] and record["committed_revision"] != receipt.committed_revision:
        raise SeedError("durable seed receipt conflicts with committed history")
    if record["state"] != "confirmed":
        _transition(intent_path, record["publication_id"], "confirmed", receipt.committed_revision)
    return receipt


def apply(
    plan: SeedPlan,
    intent_path: Path,
    store,
    *,
    fresh_plan: Callable[[], SeedPlan],
    retry_original: bool = False,
) -> InitialSeedReceipt | None:
    """Publish from an unchanged plan; UNKNOWN remains recoverable by its UUID."""
    current = fresh_plan()
    if current.source_manifest_id != plan.source_manifest_id:
        raise SeedError("seed source changed since approved plan")
    existing = _read_intent(intent_path)
    if existing is None:
        if current.manifest_id != plan.manifest_id:
            raise SeedError("seed target changed since approved plan")
        record = prepare(current, intent_path)
    else:
        _match_intent(current, existing)
        record = existing
    if record["state"] != "prepared":
        receipt = reconcile(current, intent_path, store)
        if receipt is not None or not retry_original:
            return receipt
    destination = store.inspect_initial_destination()
    if (
        not destination.empty
        or destination.schema_revision != record["expected_schema_parent"]
        or destination.backend_identity != record["backend_identity"]
        or destination.generation != record["generation"]
    ):
        raise SeedError("seed destination changed after original plan")
    _transition(intent_path, record["publication_id"], "submitted")
    try:
        receipt = store.initialize_snapshot(
            current.documents,
            expected_schema_parent=record["expected_schema_parent"],
            publication_id=record["publication_id"],
        )
    except PublicationUnknown:
        return reconcile(current, intent_path, store)
    except SqlConfigError:
        raise SeedError("seed publication refused; inspect original intent before retry") from None
    _transition(intent_path, record["publication_id"], "confirmed", receipt.committed_revision)
    if fresh_plan().source_manifest_id != plan.source_manifest_id:
        raise SeedError("seed published but source changed; keep Git selected and inspect receipt")
    return receipt


def _export_pending_path(path: Path) -> Path:
    path = _intent_path(path)
    return path.with_name(path.name + ".pending")


def _suspension_digest(suspension: WriterSuspensionEvidence) -> str:
    record = asdict(suspension)
    record["artifact_path"] = str(suspension.artifact_path)
    record["signature_path"] = str(suspension.signature_path)
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_export_intent(path: Path) -> GitExportIntent | None:
    path = _export_pending_path(path)
    with _intent_lock(path):
        return _read_export_intent_unlocked(path)


def _prepare_export_intent(path: Path, proposed: GitExportIntent) -> GitExportIntent:
    path = _export_pending_path(path)
    with _intent_lock(path):
        existing = _read_export_intent_unlocked(path)
        if existing is not None:
            if existing != replace(proposed, publication_id=existing.publication_id):
                raise SeedError("original Git export intent changed")
            return existing
        _write_intent(path, asdict(proposed), create=True)
        return proposed


def _read_export_intent_unlocked(path: Path) -> GitExportIntent | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise SeedError("Git export intent custody changed")
        body = os.read(fd, 4097)
    finally:
        os.close(fd)
    return _parse_export_intent_bytes(body)


def _parse_export_intent_bytes(body: bytes) -> GitExportIntent:
    try:
        record = json.loads(body)
        if not isinstance(record, dict) or set(record) != set(GitExportIntent.__dataclass_fields__):
            raise ValueError()
        if any(not isinstance(value, str) or not value for value in record.values()):
            raise ValueError()
        parsed = uuid.UUID(record["publication_id"])
        if parsed.version != 4 or str(parsed) != record["publication_id"]:
            raise ValueError()
        for key in (
            "source_documents_sha256",
            "original_host_sha256",
            "proposed_host_sha256",
            "suspension_sha256",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", record[key]):
                raise ValueError()
        parse_id(record["beadyard_id"])
        return GitExportIntent(**record)
    except (ValueError, TypeError, KeyError, BeadyardIdentityError):
        raise SeedError("Git export intent invalid") from None


def _recover_git_export(git_store, intent: GitExportIntent, expected_documents):
    """Return only the signed direct child carrying this original UUID and body."""
    current = git_store.load_snapshot()
    if tuple(current.documents) != tuple(expected_documents):
        raise SeedError("signed Git export differs from original projected documents")
    sha = current.commit_revision
    try:
        parents = git_store.git(git_store.plane.hq_dir, "rev-list", "--parents", "-n", "1", sha)
        message = git_store.git(git_store.plane.hq_dir, "show", "-s", "--format=%B", sha)
    except Exception:
        raise SeedError("signed Git export ancestry unavailable") from None
    if parents.split() != [sha, intent.export_expected_parent]:
        raise SeedError("signed Git export original parent changed")
    if message.count("HQ export publication ID: ") != 1 or not message.rstrip().endswith(
        "HQ export publication ID: " + intent.publication_id
    ):
        raise SeedError("signed Git export publication ID changed")
    return current


def export_latest_to_signed_git(
    *,
    sql_store,
    git_store,
    expected_sql_revision: str,
    expected_git_revision: str,
    original_host_bytes: bytes,
    proposed_host: dict,
    suspension: WriterSuspensionEvidence,
    intent_path: Path,
) -> RollbackReceipt:
    """Publish the *current* SQL authority to the protected signed Git config ref.

    A server-local operator first suspends/drains the SQL publisher and signs the
    denial-probe artifact. Both adapters authenticate their own current heads.
    A failed/ambiguous Git publication leaves SQL selected; it never authorizes
    a selector change or silently retries against a newer parent.
    """
    try:
        policy = git_store.plane._policy()
        if policy["client"]["role"] != "operator":
            raise SeedError("signed Git export requires operator custody")
        verify_writer_suspension(
            suspension,
            operator_signers=policy["operator_signers"],
            ssh_keygen=policy["executables"]["ssh_keygen"]["path"],
        )
        latest = sql_store.load_snapshot()
        if latest.commit_revision != expected_sql_revision:
            raise SeedError("current SQL head changed before rollback export")
        projected = project_latest_to_git(latest.documents)
        owner = identity_in_documents(projected, required=True)
        if owner is None or not expected_git_revision:
            raise SeedError("rollback requires bound identity and original signed Git parent")
        proposed = GitExportIntent(
            str(uuid.uuid4()),
            latest.backend_identity,
            latest.generation,
            latest.commit_revision,
            _digest(latest.documents),
            expected_git_revision,
            owner,
            hashlib.sha256(original_host_bytes).hexdigest(),
            hashlib.sha256(
                json.dumps(proposed_host, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            _suspension_digest(suspension),
        )
        intent = _prepare_export_intent(intent_path, proposed)
        current_git = git_store.load_snapshot()
        if current_git.commit_revision == intent.export_expected_parent:
            try:
                git_store.publish_snapshot(
                    projected,
                    expected_revision=intent.export_expected_parent,
                    publication_id=intent.publication_id,
                )
            except Exception:
                # A successful protected push can lose its client response.
                # The retry path below only accepts the original signed UUID,
                # direct parent and exact projected document body.
                try:
                    _recover_git_export(git_store, intent, projected)
                except SeedError:
                    raise SeedError(
                        "Git export acknowledgment unknown; resume original durable intent"
                    ) from None
        published = _recover_git_export(git_store, intent, projected)
        current_sql = sql_store.load_snapshot()
        if current_sql.commit_revision != expected_sql_revision:
            raise SeedError("SQL head changed during signed Git export")
        receipt = receipt_for_export(
            current_sql,
            published,
            original_host_bytes=original_host_bytes,
            proposed_host=proposed_host,
            suspension=suspension,
            export_expected_parent=intent.export_expected_parent,
            export_publication_id=intent.publication_id,
        )
        record_rollback_receipt(intent_path, receipt)
        return receipt
    except TransitionError as exc:
        raise SeedError(str(exc)) from None


def load_rollback_receipt(path: Path) -> RollbackReceipt | None:
    """Read a private durable export witness; never treat it as current authority."""
    path = _intent_path(path)
    with _intent_lock(path):
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_size > 4096
            ):
                raise SeedError("rollback receipt custody changed")
            body = os.read(fd, 4097)
        finally:
            os.close(fd)
        try:
            return parse_receipt_json(body)
        except TransitionError:
            raise SeedError("private rollback receipt invalid") from None


def record_rollback_receipt(path: Path, receipt: RollbackReceipt) -> RollbackReceipt:
    """Durably create the immutable original export intent before HOST switching."""
    path = _intent_path(path)
    with _intent_lock(path):
        if path.exists():
            existing = parse_receipt_json(path.read_bytes())
            if existing != receipt:
                raise SeedError("rollback receipt conflicts with durable export intent")
            return existing
        _write_intent(path, json.loads(receipt_json(receipt)), create=True)
        return receipt


def plan_git_mirror(
    *,
    receipt: RollbackReceipt,
    git_snapshot,
    hq_dir: Path,
    workspace_root: Path,
    proposed_host: dict,
) -> GitMirrorPlan:
    """Map the exact signed export to the files the Git facade will select.

    External workspace files are never deleted or renamed. If their selected
    names differ from the committed document set, the operator needs a new
    explicit plan instead of a silent fallback or partial shadowed mirror.
    """
    from .modules.config.application.resolution import ResolutionInputs, resolve_config

    if git_snapshot.commit_revision != receipt.export_revision:
        raise SeedError("signed Git mirror revision changed")
    hq_dir, workspace_root = Path(hq_dir), Path(workspace_root)
    if not hq_dir.is_dir() or hq_dir.is_symlink() or workspace_root.is_symlink():
        raise SeedError("Git mirror source or workspace root unavailable")
    documents = tuple(git_snapshot.documents)
    try:
        fleet = next(item for item in documents if item.path == "fleet.yaml")
        raw = YAML().load(fleet.content)
        host_layer = deepcopy(proposed_host)
        if isinstance(host_layer.get("hq"), dict):
            # hq.mode is a committed FLEET field. A legacy HOST bootstrap mode
            # may select the backend, but is not a second effective settings
            # override when comparing the projected Git layer.
            host_layer["hq"].pop("mode", None)
        settings = resolve_config(ResolutionInputs(fleet=raw, host=host_layer)).settings.model_dump(
            mode="json", by_alias=True
        )
        explicit = (settings.get("git_workspace") or {}).get("path")
        work_docs = tuple(
            item
            for item in documents
            if item.path == "workspace.toml"
            or (item.path.startswith("workspace-") and item.path.endswith(".toml"))
        )
        if explicit:
            selected = (Path(explicit).expanduser(),)
        else:
            from .gitworkspace import _external_configs, glob_configs

            selected = tuple(_external_configs(workspace_root))
            if not selected:
                selected = tuple(glob_configs(hq_dir))
                if not selected:
                    selected = tuple(hq_dir / item.path for item in work_docs)
        if tuple(path.name for path in selected) != tuple(item.path for item in work_docs):
            raise SeedError("Git workspace selected file set differs from signed latest export")
        work_paths = {item.path: path for item, path in zip(work_docs, selected, strict=True)}
        paths = {item.path: work_paths.get(item.path, hq_dir / item.path) for item in documents}
        if len(set(paths.values())) != len(paths):
            raise SeedError("Git mirror path mapping is ambiguous")
        targets = []
        for item in documents:
            path = paths[item.path]
            if not path.is_absolute() or path.is_symlink():
                raise SeedError("Git mirror selected path is not a safe absolute file")
            parent = path.parent
            while parent != parent.parent:
                if parent.is_symlink():
                    raise SeedError("Git mirror selected parent is a symlink")
                parent = parent.parent
            if path.exists():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
                    raise SeedError("Git mirror selected source is not bounded regular file")
                original = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                original = None
            targets.append(
                MirrorTarget(
                    item.path,
                    path,
                    original,
                    hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                )
            )
        manifest = json.dumps(
            {
                "source_revision": receipt.source_revision,
                "export_revision": receipt.export_revision,
                "targets": [
                    (row.document, str(row.path), row.original_sha256, row.export_sha256)
                    for row in targets
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return GitMirrorPlan(
            receipt.source_revision,
            receipt.export_revision,
            hashlib.sha256(manifest.encode()).hexdigest(),
            tuple(targets),
        )
    except (StopIteration, ValueError, TypeError, OSError):
        raise SeedError("Git mirror layout cannot be qualified") from None


def _mirror_record(plan: GitMirrorPlan) -> dict[str, object]:
    return {
        "source_revision": plan.source_revision,
        "export_revision": plan.export_revision,
        "plan_sha256": plan.plan_sha256,
        "targets": [
            [row.document, str(row.path), row.original_sha256, row.export_sha256]
            for row in plan.targets
        ],
    }


def _read_mirror_record(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise SeedError("Git mirror journal custody changed")
        body = os.read(fd, 65537)
    finally:
        os.close(fd)
    try:
        record = json.loads(body)
        if not isinstance(record, dict) or set(record) != {
            "source_revision",
            "export_revision",
            "plan_sha256",
            "targets",
        }:
            raise ValueError()
        if not isinstance(record["targets"], list) or len(record["targets"]) > 1024:
            raise ValueError()
        if any(not isinstance(row, list) or len(row) != 4 for row in record["targets"]):
            raise ValueError()
        return record
    except (ValueError, TypeError):
        raise SeedError("Git mirror journal invalid") from None


def recover_git_mirror_plan(
    journal_path: Path,
    *,
    receipt: RollbackReceipt,
    git_snapshot,
    hq_dir: Path,
    workspace_root: Path,
    proposed_host: dict,
) -> GitMirrorPlan:
    """Rebuild a partially installed mirror from its original private journal."""
    with _intent_lock(journal_path):
        record = _read_mirror_record(_intent_path(journal_path))
    if record is None:
        raise SeedError("Git mirror journal missing")
    try:
        if (record["source_revision"], record["export_revision"]) != (
            receipt.source_revision,
            receipt.export_revision,
        ):
            raise ValueError()
        targets = tuple(
            MirrorTarget(document, Path(path), old, new)
            for document, path, old, new in record["targets"]
        )
        if any(
            not row.path.is_absolute()
            or not re.fullmatch(r"[0-9a-f]{64}", row.export_sha256)
            or (
                row.original_sha256 is not None
                and not re.fullmatch(r"[0-9a-f]{64}", row.original_sha256)
            )
            for row in targets
        ):
            raise ValueError()
        manifest = json.dumps(
            {
                "source_revision": record["source_revision"],
                "export_revision": record["export_revision"],
                "targets": record["targets"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if hashlib.sha256(manifest.encode()).hexdigest() != record["plan_sha256"]:
            raise ValueError()
        current_selection = plan_git_mirror(
            receipt=receipt,
            git_snapshot=git_snapshot,
            hq_dir=hq_dir,
            workspace_root=workspace_root,
            proposed_host=proposed_host,
        )
        if tuple((row.document, row.path, row.export_sha256) for row in targets) != tuple(
            (row.document, row.path, row.export_sha256) for row in current_selection.targets
        ):
            raise ValueError()
        return GitMirrorPlan(
            receipt.source_revision, receipt.export_revision, record["plan_sha256"], targets
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SeedError("Git mirror journal conflicts with signed latest export") from None


def _qualified_export_now(sql_store, git_store, receipt, original_host_bytes, proposed_host):
    policy = git_store.plane._policy()
    verify_writer_suspension(
        receipt.suspension,
        operator_signers=policy["operator_signers"],
        ssh_keygen=policy["executables"]["ssh_keygen"]["path"],
    )
    sql_snapshot = sql_store.load_snapshot()
    git_snapshot = git_store.load_snapshot()
    verify_git_export_lineage(git_store, receipt)
    expected = receipt_for_export(
        sql_snapshot,
        git_snapshot,
        original_host_bytes=original_host_bytes,
        proposed_host=proposed_host,
        suspension=receipt.suspension,
        export_expected_parent=receipt.export_expected_parent,
        export_publication_id=receipt.export_publication_id,
    )
    if expected != receipt:
        raise SeedError("current rollback export differs from original receipt")
    return git_snapshot


def install_git_mirror(
    plan: GitMirrorPlan,
    journal_path: Path,
    *,
    sql_store,
    git_store,
    receipt: RollbackReceipt,
    original_host_bytes: bytes,
    proposed_host: dict,
) -> GitMirrorPlan:
    """Resume exact local mirror writes while SQL remains the selected authority.

    The private journal is durable before the first file. Each file may be at
    its captured old digest or already at the signed export digest; any third
    value aborts. A process death can therefore resume without reverting or
    silently overwriting an unrelated writer.
    """
    if (plan.source_revision, plan.export_revision) != (
        receipt.source_revision,
        receipt.export_revision,
    ):
        raise SeedError("Git mirror plan and export receipt differ")
    git_snapshot = _qualified_export_now(
        sql_store, git_store, receipt, original_host_bytes, proposed_host
    )
    documents = {item.path: item.content.encode("utf-8") for item in git_snapshot.documents}
    if set(documents) != {row.document for row in plan.targets}:
        raise SeedError("Git mirror document set changed")
    path = _intent_path(journal_path)
    with _intent_lock(path):
        record = _read_mirror_record(path)
        proposed_record = _mirror_record(plan)
        if record is None:
            _write_intent(path, proposed_record, create=True, max_bytes=65536)
        elif record != proposed_record:
            raise SeedError("Git mirror plan conflicts with durable original")
        for target in plan.targets:
            destination = target.path
            if destination.is_symlink() or destination.parent.is_symlink():
                raise SeedError("Git mirror path custody changed")
            current = destination.read_bytes() if destination.exists() else None
            current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
            if current_hash not in (target.original_sha256, target.export_sha256):
                raise SeedError("Git mirror source changed after approved plan")
            if current_hash == target.export_sha256:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(documents[target.document])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(
                    temporary, destination.stat().st_mode & 0o777 if current is not None else 0o600
                )
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        verify_git_mirror(plan, git_snapshot.documents)
        _qualified_export_now(sql_store, git_store, receipt, original_host_bytes, proposed_host)
    return plan
