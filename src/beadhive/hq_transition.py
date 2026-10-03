"""Pure, exact config-authority export contract for a SQL-to-Git rollback.

The two adapters authenticate their own current snapshots. This module compares
their ordered documents and canonical HQ identity without reading HOST, Git, SQL,
or credentials. A historical seed receipt is never a current export receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

from .beadyard_identity import BeadyardIdentityError, identity_in_documents
from .hq_document_validation import DocumentValidationError, validate_documents
from .modules.config.domain.ports import FleetConfigDocument, ordered_documents_digest


class TransitionError(ValueError):
    """A current cross-backend export or selector transition is unqualified."""


@dataclass(frozen=True)
class WriterSuspensionEvidence:
    """A reference to a trusted server-local revocation and denial-probe artifact.

    This value binds the operator's evidence to one backend, generation, and
    original HEAD. The artifact itself must prove publisher DML and version-
    control calls denied after in-flight writes drained. A local HOST lock does
    not enforce that remote server custody.
    """

    backend_identity: str
    generation: str
    original_head: str
    principal: str
    artifact_sha256: str
    observed_at: float
    valid_until: float
    artifact_path: Path
    signature_path: Path


_SUSPENSION_KEYS = {
    "backend_identity",
    "generation",
    "original_head",
    "principal",
    "observed_at",
    "valid_until",
    "drained",
    "status_clean",
    "denied",
}
_DENIED = ("dolt_add", "dolt_commit", "config_table_dml")


def verify_writer_suspension(
    evidence: WriterSuspensionEvidence, *, operator_signers: str, ssh_keygen: str
) -> None:
    """Authenticate the bounded server-local denial probe using Git operator trust."""

    def protected(path: Path, limit: int) -> bytes:
        path = Path(path)
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > limit
        ):
            raise TransitionError("trusted writer suspension artifact custody invalid")
        return path.read_bytes()

    try:
        body = protected(evidence.artifact_path, 4096)
        protected(evidence.signature_path, 8192)
        if hashlib.sha256(body).hexdigest() != evidence.artifact_sha256:
            raise TransitionError("trusted writer suspension artifact changed")
        claim = json.loads(body)
        if (
            not isinstance(claim, dict)
            or set(claim) != _SUSPENSION_KEYS
            or claim["drained"] is not True
            or claim["status_clean"] is not True
            or claim["denied"] != list(_DENIED)
            or any(
                claim[key] != getattr(evidence, key)
                for key in (
                    "backend_identity",
                    "generation",
                    "original_head",
                    "principal",
                    "observed_at",
                    "valid_until",
                )
            )
        ):
            raise TransitionError("trusted writer suspension artifact content invalid")
        result = subprocess.run(
            [
                ssh_keygen,
                "-Y",
                "verify",
                "-f",
                operator_signers,
                "-I",
                "operator",
                "-n",
                "beadhive-hq-writer-suspension",
                "-s",
                str(evidence.signature_path),
            ],
            input=body,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if result.returncode:
            raise TransitionError("trusted writer suspension signature invalid")
    except (OSError, json.JSONDecodeError, TypeError, subprocess.TimeoutExpired):
        raise TransitionError("trusted writer suspension unavailable or invalid") from None


@dataclass(frozen=True)
class RollbackReceipt:
    source_backend: str
    source_generation: str
    source_revision: str
    source_documents_sha256: str
    export_backend: str
    export_generation: str
    export_revision: str
    export_expected_parent: str
    export_publication_id: str
    export_documents_sha256: str
    beadyard_id: str
    original_host_sha256: str
    proposed_host_sha256: str
    suspension: WriterSuspensionEvidence


@dataclass(frozen=True)
class MirrorTarget:
    document: str
    path: Path
    original_sha256: str | None
    export_sha256: str


@dataclass(frozen=True)
class GitMirrorPlan:
    source_revision: str
    export_revision: str
    plan_sha256: str
    targets: tuple[MirrorTarget, ...]


def verify_git_mirror(plan: GitMirrorPlan, documents) -> None:
    """Require every selected Git working source to equal the signed export."""
    contents = {item.path: item.content.encode("utf-8") for item in documents}
    if set(contents) != {target.document for target in plan.targets}:
        raise TransitionError("Git mirror document selection differs from signed export")
    for target in plan.targets:
        try:
            if target.path.is_symlink() or not target.path.is_file():
                raise TransitionError("Git mirror selected path unavailable")
            body = target.path.read_bytes()
        except OSError:
            raise TransitionError("Git mirror selected path unavailable") from None
        if (
            hashlib.sha256(body).hexdigest() != target.export_sha256
            or body != contents[target.document]
        ):
            raise TransitionError("Git mirror differs from signed latest export")


def project_latest_to_git(documents) -> tuple[FleetConfigDocument, ...]:
    """Change the one backend mode in the latest SQL raw document stream."""
    source = tuple(documents)
    try:
        validate_documents(source)
        fleet = next(item for item in source if item.path == "fleet.yaml")
        yaml = YAML()
        yaml.preserve_quotes = True
        parsed = yaml.load(fleet.content)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("hq"), dict):
            raise TransitionError("latest SQL fleet document has no typed HQ mode")
        if parsed["hq"].get("mode") != "dolt-server":
            raise TransitionError("latest SQL fleet document is not SQL selected")
        parsed["hq"]["mode"] = "git"
        output = StringIO()
        yaml.dump(parsed, output)
        projected = tuple(
            FleetConfigDocument(
                item.path, output.getvalue() if item.path == "fleet.yaml" else item.content
            )
            for item in source
        )
        validate_documents(projected)
        if identity_in_documents(source, required=True) != identity_in_documents(
            projected, required=True
        ):
            raise TransitionError("portable HQ identity changed during export")
        return projected
    except (StopIteration, DocumentValidationError, BeadyardIdentityError, ValueError, TypeError):
        raise TransitionError("latest SQL documents cannot be projected to signed Git") from None


def receipt_for_export(
    sql_snapshot,
    git_snapshot,
    *,
    original_host_bytes: bytes,
    proposed_host: dict,
    suspension: WriterSuspensionEvidence,
    export_expected_parent: str,
    export_publication_id: str,
    now: float | None = None,
) -> RollbackReceipt:
    """Require exact current signed Git bytes corresponding to latest SQL bytes."""
    try:
        now = time.time() if now is None else now
        if not isinstance(suspension, WriterSuspensionEvidence) or not all(
            isinstance(value, str) and value
            for value in (
                suspension.backend_identity,
                suspension.generation,
                suspension.original_head,
                suspension.principal,
            )
        ):
            raise TransitionError("trusted writer suspension evidence missing")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", suspension.artifact_sha256)
            or not math.isfinite(suspension.observed_at)
            or not math.isfinite(suspension.valid_until)
            or not suspension.observed_at <= now < suspension.valid_until
            or suspension.valid_until - suspension.observed_at > 300
            or (
                suspension.backend_identity,
                suspension.generation,
                suspension.original_head,
            )
            != (
                sql_snapshot.backend_identity,
                sql_snapshot.generation,
                sql_snapshot.commit_revision,
            )
        ):
            raise TransitionError("trusted writer suspension does not bind current SQL head")
        if sql_snapshot.valid_until <= now or git_snapshot.valid_until <= now:
            raise TransitionError("current config snapshot validity expired")
        try:
            publication = uuid.UUID(export_publication_id)
        except (ValueError, TypeError, AttributeError):
            raise TransitionError("signed Git export publication ID invalid") from None
        if publication.version != 4 or str(publication) != export_publication_id:
            raise TransitionError("signed Git export publication ID invalid")
        expected = project_latest_to_git(sql_snapshot.documents)
        if tuple(git_snapshot.documents) != expected:
            raise TransitionError("signed Git export differs from latest SQL authority")
        owner = identity_in_documents(expected, required=True)
        if not owner:
            raise TransitionError("portable HQ identity missing from export")
        return RollbackReceipt(
            sql_snapshot.backend_identity,
            sql_snapshot.generation,
            sql_snapshot.commit_revision,
            ordered_documents_digest(sql_snapshot.documents),
            git_snapshot.backend_identity,
            git_snapshot.generation,
            git_snapshot.commit_revision,
            export_expected_parent,
            export_publication_id,
            ordered_documents_digest(git_snapshot.documents),
            owner,
            hashlib.sha256(original_host_bytes).hexdigest(),
            hashlib.sha256(
                json.dumps(proposed_host, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            suspension,
        )
    except (AttributeError, TypeError, BeadyardIdentityError):
        raise TransitionError("current rollback export witness invalid") from None


def verify_git_export_lineage(git_store, receipt: RollbackReceipt) -> None:
    """Qualify a signed protected Git export against its original parent and UUID."""
    try:
        git_store.load_snapshot(revision=receipt.export_revision)
        sha = receipt.export_revision
        parents = git_store.git(git_store.plane.hq_dir, "rev-list", "--parents", "-n", "1", sha)
        message = git_store.git(git_store.plane.hq_dir, "show", "-s", "--format=%B", sha)
    except Exception:
        raise TransitionError("signed Git export ancestry unavailable") from None
    if parents.split() != [sha, receipt.export_expected_parent]:
        raise TransitionError("signed Git export original parent changed")
    marker = "HQ export publication ID: " + receipt.export_publication_id
    if message.count("HQ export publication ID: ") != 1 or not message.rstrip().endswith(marker):
        raise TransitionError("signed Git export publication ID changed")


def receipt_json(receipt: RollbackReceipt) -> str:
    """Canonical private journal carrier with explicit path serialization."""
    record = asdict(receipt)
    record["suspension"]["artifact_path"] = str(receipt.suspension.artifact_path)
    record["suspension"]["signature_path"] = str(receipt.suspension.signature_path)
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def parse_receipt_json(body: str | bytes) -> RollbackReceipt:
    """Reconstruct only the exact typed carrier; live checks remain mandatory."""
    try:
        row = json.loads(body)
        expected = set(RollbackReceipt.__dataclass_fields__)
        suspension_keys = set(WriterSuspensionEvidence.__dataclass_fields__)
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError()
        claim = row.pop("suspension")
        if not isinstance(claim, dict) or set(claim) != suspension_keys:
            raise ValueError()
        if any(not isinstance(value, str) or not value for value in row.values()):
            raise ValueError()
        if any(
            not isinstance(claim[key], str) or not claim[key]
            for key in (
                "backend_identity",
                "generation",
                "original_head",
                "principal",
                "artifact_sha256",
                "artifact_path",
                "signature_path",
            )
        ):
            raise ValueError()
        if any(type(claim[key]) not in (int, float) for key in ("observed_at", "valid_until")):
            raise ValueError()
        for key in ("artifact_path", "signature_path"):
            if not Path(claim[key]).is_absolute():
                raise ValueError()
            claim[key] = Path(claim[key])
        evidence = WriterSuspensionEvidence(**claim)
        return RollbackReceipt(**row, suspension=evidence)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise TransitionError("private rollback receipt invalid") from None
