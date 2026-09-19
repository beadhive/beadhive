"""Offline assembly and integrity checking for the official v1 contract bundle.

The domain modules named by ``source_owner`` remain the contract authorities.  This module is a
publication boundary: it snapshots their deterministic projections, gives every artifact a
stable release identity, and exposes a checksum-verified resource lookup for consumers.  It
never imports a server composition root or performs network/process I/O.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, fields
from fractions import Fraction
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from . import daemon_openapi, gateway_wire_contracts, transport_inventory
from .kernel import operations
from .kernel.lifecycle.contracts import (
    ALL_LIFECYCLE_EVENTS,
    CompensationMode,
    Criticality,
    Idempotency,
)
from .kernel.plugins import builtin_manifest_documents
from .kernel.telemetry import (
    EventEnvelope,
    EventIdentity,
    EventPhase,
    Outcome,
    SemanticEventName,
    TelemetryAttribute,
    event_envelope_schema,
)
from .modules.agents.domain import (
    AbortReceiptV1,
    AdapterAction,
    AdapterCommitResultV1,
    AdapterOutcome,
    AgentLaunchProfile,
    AgentLaunchReceipt,
    CompensationStatus,
    Harness,
    LaunchReceiptV1,
    PreparedLaunchV1,
    WorkspaceBindingKind,
    WorkspaceBindingV1,
    WorkspaceTargetV1,
    portable_digest,
    resolve_agent_launch_profile,
    seat_contract,
)
from .modules.agents.domain.profile import KNOWN_SEATS
from .modules.config.application.schema_artifacts import (
    generate_config_json_schema,
    plugin_fragment_artifacts,
)

RELEASE_VERSION = "1.0.0"
RELEASE_MAJOR = 1
OFFICIAL_V1_FAMILIES = frozenset(
    {
        "config",
        "plugin-manifest",
        "plugin-fragment",
        "operation-catalog",
        "cli",
        "mcp",
        "openapi",
        "gateway",
        "lifecycle",
        "telemetry",
        "seat",
        "launch",
        "workspace",
        "prepared",
        "commit",
        "abort",
        "receipt",
    }
)

_SCHEMA_POLICY = "json-schema-additive-v1"
_CATALOG_POLICY = "append-only-catalog-v1"
_OPENAPI_POLICY = "openapi-additive-v1"
_PUBLISHED_BASELINE_VERSION = "1.0.0"
_PUBLISHED_BASELINE_SHA256 = (
    "sha256:842ae163daf23f090af8bbc11f50145adcfb22c15185c5e24fec3795119d1037"
)
_MANIFEST_SCHEMA = Path("docs/schemas/wire/v1.4.0/plugin-manifest-v1.schema.json")
_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
_INVENTORY_KEYS = frozenset(
    {
        "format_version",
        "release_version",
        "schema_draft",
        "artifacts",
        "conformance_fixtures",
    }
)
_ARTIFACT_ROW_KEYS = frozenset(
    {
        "id",
        "family",
        "version",
        "kind",
        "path",
        "source_owner",
        "compatibility_policy",
        "examples",
        "sha256",
    }
)
_CONFORMANCE_KEYS = frozenset({"format_version", "release_version", "cases"})
_CONFORMANCE_CASE_KEYS = frozenset({"name", "artifact_id", "assertion", "input"})
_OFFICIAL_V1_ARTIFACT_IDENTITIES = (
    ("urn:beadhive:wire-schema:abort-receipt:1", "artifacts/abort-receipt-v1.schema.json"),
    (
        "urn:beadhive:wire-schema:adapter-commit-result:1",
        "artifacts/adapter-commit-result-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:agent-launch-profile:1",
        "artifacts/agent-launch-profile-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:agent-launch-receipt:1",
        "artifacts/agent-launch-receipt-v1.schema.json",
    ),
    ("urn:beadhive:wire-catalog:cli-projections:1", "artifacts/cli-projections-v1.json"),
    ("urn:beadhive:wire-schema:config:1", "artifacts/config-v1.schema.json"),
    (
        "urn:beadhive:wire-catalog:gateway-contracts:1",
        "artifacts/gateway-contracts-v1.json",
    ),
    ("urn:beadhive:wire-contract:host-openapi:1", "artifacts/host-openapi-v1.json"),
    (
        "urn:beadhive:wire-schema:launch-receipt:1",
        "artifacts/launch-receipt-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-catalog:lifecycle-events:1",
        "artifacts/lifecycle-events-v1.json",
    ),
    ("urn:beadhive:wire-catalog:mcp-projections:1", "artifacts/mcp-projections-v1.json"),
    ("urn:beadhive:wire-catalog:operations:1", "artifacts/operation-catalog-v1.json"),
    (
        "urn:beadhive:wire-schema:plugin-config:herdr:1",
        "artifacts/plugin-config-herdr-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:plugin-config:hitch:1",
        "artifacts/plugin-config-hitch-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:plugin-config:observaloop:1",
        "artifacts/plugin-config-observaloop-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:plugin-config:orca:1",
        "artifacts/plugin-config-orca-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:plugin-config:repowise:1",
        "artifacts/plugin-config-repowise-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:plugin-manifest:1",
        "artifacts/plugin-manifest-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:prepared-launch:1",
        "artifacts/prepared-launch-v1.schema.json",
    ),
    ("urn:beadhive:wire-catalog:seat-contracts:1", "artifacts/seat-contracts-v1.json"),
    (
        "urn:beadhive:wire-schema:telemetry.event-envelope:1",
        "artifacts/telemetry-event-envelope-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:workspace-binding:1",
        "artifacts/workspace-binding-v1.schema.json",
    ),
    (
        "urn:beadhive:wire-schema:workspace-target:1",
        "artifacts/workspace-target-v1.schema.json",
    ),
)
_OFFICIAL_V1_CASE_IDENTITIES = tuple(
    (PurePosixPath(path).stem.removesuffix(".schema") + "-valid", artifact_id)
    for artifact_id, path in _OFFICIAL_V1_ARTIFACT_IDENTITIES
)


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def release_root() -> Path:
    """Return the installed, package-owned official-v1 release directory."""

    return Path(resources.files("beadhive").joinpath("schemas", "contracts", f"v{RELEASE_VERSION}"))


def published_baseline_root() -> Path:
    """Return the immutable, package-owned compatibility baseline for official v1."""

    return Path(
        resources.files("beadhive").joinpath(
            "schemas", "contracts", "baselines", f"v{_PUBLISHED_BASELINE_VERSION}"
        )
    )


def _stable_document(document: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    rendered = deepcopy(document)
    current = rendered.get("$id")
    if current is not None and current != artifact_id:
        raise ValueError(f"canonical owner returned conflicting artifact id {current!r}")
    rendered["$id"] = artifact_id
    rendered["version"] = RELEASE_MAJOR
    return rendered


def _model_schema(model: Any, artifact_id: str) -> dict[str, Any]:
    return _stable_document(model.model_json_schema(by_alias=True, mode="validation"), artifact_id)


def _artifact(
    *,
    family: str,
    artifact_id: str,
    path: str,
    source_owner: str,
    policy: str,
    document: dict[str, Any],
    example: object,
    assertion: str,
    kind: str = "json-schema",
    preserve_document: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if family not in OFFICIAL_V1_FAMILIES:
        raise ValueError(f"unknown official-v1 family {family!r}")
    name = PurePosixPath(path).stem.removesuffix(".schema") + "-valid"
    row = {
        "id": artifact_id,
        "family": family,
        "version": RELEASE_MAJOR,
        "kind": kind,
        "path": path,
        "source_owner": source_owner,
        "compatibility_policy": policy,
        "examples": [name],
        "document": deepcopy(document)
        if preserve_document
        else _stable_document(document, artifact_id),
    }
    case = {
        "name": name,
        "artifact_id": artifact_id,
        "assertion": assertion,
        "input": example,
    }
    return row, case


def _plugin_manifest_schema() -> dict[str, Any]:
    path = _repository_root() / _MANIFEST_SCHEMA
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{_MANIFEST_SCHEMA}: expected a JSON object")
    return value


def _projection_document(surface: str) -> dict[str, Any]:
    inventory = transport_inventory.document()
    projections = [
        row
        for row in inventory["projections"]
        if row["surface"] == surface or row["surface"].startswith(f"{surface}-")
    ]
    return {
        "format_version": 1,
        "projection_version": inventory["inventory_version"],
        "policy": inventory["policy"],
        "projections": projections,
    }


def _gateway_document() -> dict[str, Any]:
    # Candidate generation may only inherit history after the complete published
    # snapshot has crossed its pinned integrity boundary.  The loader reconstructs
    # stored release documents and never calls build_release(), so this does not
    # create a candidate-generation cycle.
    published_release = load_published_baseline()
    published = next(
        (
            artifact["document"]
            for artifact in published_release["artifacts"]
            if artifact["id"] == "urn:beadhive:wire-catalog:gateway-contracts:1"
        ),
        None,
    )
    if not isinstance(published, dict) or not isinstance(published.get("contracts"), list):
        raise ValueError("published gateway contract catalog is incompatible")
    contracts = deepcopy(published["contracts"])
    published_ids = {contract.get("$id") for contract in contracts if isinstance(contract, dict)}
    if len(published_ids) != len(contracts) or not all(
        isinstance(contract_id, str) for contract_id in published_ids
    ):
        raise ValueError("published gateway contract catalog identities are incompatible")
    contracts.extend(
        contract
        for contract in gateway_wire_contracts.documents()
        if contract["$id"] not in published_ids
    )
    return {
        "format_version": 1,
        "contracts": contracts,
    }


def _lifecycle_document() -> dict[str, Any]:
    events = []
    for event in ALL_LIFECYCLE_EVENTS:
        events.append(
            {
                "id": event.id,
                "family": event.family.value,
                "phase": event.phase.value,
                "order": event.order,
                "context": {
                    "type": event.context_type.__name__,
                    "fields": [field.name for field in fields(event.context_type)],
                },
            }
        )
    return {
        "format_version": 1,
        "events": events,
        "closed_unions": {
            "criticality": [item.value for item in Criticality],
            "idempotency": [item.value for item in Idempotency],
            "compensation": [item.value for item in CompensationMode],
        },
    }


def _seat_document() -> dict[str, Any]:
    contracts = []
    for name in sorted(KNOWN_SEATS):
        contract = seat_contract(name)
        contracts.append(
            {
                "seat": contract.seat,
                "version": contract.version,
                "instructions": contract.instructions,
                "digest": contract.digest,
            }
        )
    return {"format_version": 1, "contracts": contracts}


def _examples() -> dict[str, object]:
    profile = AgentLaunchProfile(
        managed_bead=False,
        initial_seat="planner",
        harness=Harness.CODEX,
    )
    profile_receipt = AgentLaunchReceipt.from_resolved(resolve_agent_launch_profile(profile))
    target = WorkspaceTargetV1(
        hive_id="github/beadhive/beadhive",
        binding_kind=WorkspaceBindingKind.BEADLESS_SEAT,
    )
    binding = WorkspaceBindingV1(
        hive_id=target.hive_id,
        binding_kind=target.binding_kind,
        branch="planning/example",
        worktree_id="sha256:" + "1" * 64,
    )
    prepared = PreparedLaunchV1(
        launch_id="launch-example",
        prepare_operation_id="prepare-example",
        profile_receipt=profile_receipt,
        workspace_binding=binding,
        binding_digest=portable_digest(binding),
        adapter_kind="codex",
        adapter_action=AdapterAction.ALLOCATE,
        adapter_plan_digest="sha256:" + "2" * 64,
    )
    commit = AdapterCommitResultV1(
        launch_id=prepared.launch_id,
        commit_operation_id="commit-example",
        binding_digest=prepared.binding_digest,
        adapter_kind=prepared.adapter_kind,
        outcome=AdapterOutcome.COMMITTED,
        allocation_id="allocation-example",
        generation=1,
    )
    receipt = LaunchReceiptV1(
        launch_id=prepared.launch_id,
        prepare_operation_id=prepared.prepare_operation_id,
        commit_operation_id=commit.commit_operation_id,
        profile_receipt=profile_receipt,
        workspace_binding=binding,
        adapter_kind=prepared.adapter_kind,
        allocation_id=commit.allocation_id,
        generation=commit.generation,
    )
    abort = AbortReceiptV1(
        launch_id="launch-aborted-example",
        abort_operation_id="abort-example",
        reason_code="adapter.refused",
        compensation=CompensationStatus.NOT_NEEDED,
    )
    telemetry = EventEnvelope(
        event_name=SemanticEventName.OPERATION_EXECUTION,
        event_version=1,
        event_id="018f47b1-2948-7c2a-b0f1-92d5be4d92ce",
        occurred_at="2026-09-10T00:00:00Z",
        phase=EventPhase.COMPLETED,
        correlation_id="018f47b1-2948-7c2a-b0f1-92d5be4d92cf",
        identity=EventIdentity(service="bh", instance_id="release-example"),
        outcome=Outcome.SUCCEEDED,
        duration_ms=1,
        attributes=(TelemetryAttribute("surface", "cli"),),
    )
    return {
        "launch": profile.model_dump(mode="json"),
        "profile_receipt": profile_receipt.model_dump(mode="json"),
        "workspace_target": target.model_dump(mode="json"),
        "workspace_binding": binding.model_dump(mode="json"),
        "prepared": prepared.model_dump(mode="json"),
        "commit": commit.model_dump(mode="json"),
        "receipt": receipt.model_dump(mode="json"),
        "abort": abort.model_dump(mode="json"),
        "telemetry": telemetry.to_document(),
    }


def build_release() -> dict[str, Any]:
    """Assemble the release exclusively from canonical, deterministic contract owners."""

    artifacts: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []

    def add(**kwargs: Any) -> None:
        artifact, case = _artifact(**kwargs)
        artifacts.append(artifact)
        cases.append(case)

    examples = _examples()
    add(
        family="config",
        artifact_id="urn:beadhive:wire-schema:config:1",
        path="artifacts/config-v1.schema.json",
        source_owner=(
            "beadhive.modules.config.application.schema_artifacts.generate_config_json_schema"
        ),
        policy=_SCHEMA_POLICY,
        document=generate_config_json_schema(),
        example={},
        assertion="valid-json-schema-instance",
    )
    manifest_example = json.loads(builtin_manifest_documents()[0].payload)
    add(
        family="plugin-manifest",
        artifact_id="urn:beadhive:wire-schema:plugin-manifest:1",
        path="artifacts/plugin-manifest-v1.schema.json",
        source_owner=_MANIFEST_SCHEMA.as_posix(),
        policy=_SCHEMA_POLICY,
        document=_plugin_manifest_schema(),
        example=manifest_example,
        assertion="valid-json-schema-instance",
    )
    for fragment, payload in plugin_fragment_artifacts():
        add(
            family="plugin-fragment",
            artifact_id=fragment.artifact_id,
            path=f"artifacts/plugin-config-{fragment.plugin_id}-v1.schema.json",
            source_owner=(
                "beadhive.modules.config.application.schema_artifacts.plugin_fragment_artifacts"
            ),
            policy=_SCHEMA_POLICY,
            document=json.loads(payload),
            example={},
            assertion="valid-json-schema-instance",
        )
    catalog = operations.document()
    add(
        family="operation-catalog",
        artifact_id=operations.CATALOG_INSTANCE_ARTIFACT_ID,
        path="artifacts/operation-catalog-v1.json",
        source_owner="beadhive.kernel.operations.document",
        policy=_CATALOG_POLICY,
        document=catalog,
        example=catalog["operations"][0],
        assertion="catalog-row-present",
        kind="catalog",
        preserve_document=True,
    )
    for family in ("cli", "mcp"):
        document = _projection_document(family)
        add(
            family=family,
            artifact_id=f"urn:beadhive:wire-catalog:{family}-projections:1",
            path=f"artifacts/{family}-projections-v1.json",
            source_owner="beadhive.transport_inventory.document",
            policy=_CATALOG_POLICY,
            document=document,
            example=document["projections"][0],
            assertion="projection-row-present",
            kind="catalog",
        )
    openapi = daemon_openapi.generate_openapi_document()
    first_openapi_path = sorted(openapi["paths"])[0]
    add(
        family="openapi",
        artifact_id="urn:beadhive:wire-contract:host-openapi:1",
        path="artifacts/host-openapi-v1.json",
        source_owner="beadhive.daemon_openapi.generate_openapi_document",
        policy=_OPENAPI_POLICY,
        document=openapi,
        example={
            "path": first_openapi_path,
            "operations": sorted(openapi["paths"][first_openapi_path]),
        },
        assertion="openapi-path-present",
        kind="openapi",
        preserve_document=True,
    )
    gateway = _gateway_document()
    add(
        family="gateway",
        artifact_id="urn:beadhive:wire-catalog:gateway-contracts:1",
        path="artifacts/gateway-contracts-v1.json",
        source_owner="beadhive.gateway_wire_contracts.documents",
        policy=_CATALOG_POLICY,
        document=gateway,
        example={"contractVersion": gateway["contracts"][0]["contractVersion"]},
        assertion="gateway-contract-present",
        kind="catalog",
    )
    lifecycle = _lifecycle_document()
    add(
        family="lifecycle",
        artifact_id="urn:beadhive:wire-catalog:lifecycle-events:1",
        path="artifacts/lifecycle-events-v1.json",
        source_owner="beadhive.kernel.lifecycle.contracts.ALL_LIFECYCLE_EVENTS",
        policy=_CATALOG_POLICY,
        document=lifecycle,
        example=lifecycle["events"][0],
        assertion="lifecycle-event-present",
        kind="catalog",
    )
    add(
        family="telemetry",
        artifact_id="urn:beadhive:wire-schema:telemetry.event-envelope:1",
        path="artifacts/telemetry-event-envelope-v1.schema.json",
        source_owner="beadhive.kernel.telemetry.event_envelope_schema",
        policy=_SCHEMA_POLICY,
        document=event_envelope_schema(),
        example=examples["telemetry"],
        assertion="valid-json-schema-instance",
    )
    seats = _seat_document()
    add(
        family="seat",
        artifact_id="urn:beadhive:wire-catalog:seat-contracts:1",
        path="artifacts/seat-contracts-v1.json",
        source_owner="beadhive.modules.agents.domain.seat.seat_contract",
        policy=_CATALOG_POLICY,
        document=seats,
        example=seats["contracts"][0],
        assertion="seat-contract-present",
        kind="catalog",
    )

    model_artifacts = (
        ("launch", AgentLaunchProfile, "agent-launch-profile", examples["launch"]),
        ("workspace", WorkspaceTargetV1, "workspace-target", examples["workspace_target"]),
        ("workspace", WorkspaceBindingV1, "workspace-binding", examples["workspace_binding"]),
        ("prepared", PreparedLaunchV1, "prepared-launch", examples["prepared"]),
        ("commit", AdapterCommitResultV1, "adapter-commit-result", examples["commit"]),
        ("abort", AbortReceiptV1, "abort-receipt", examples["abort"]),
        ("receipt", AgentLaunchReceipt, "agent-launch-receipt", examples["profile_receipt"]),
        ("receipt", LaunchReceiptV1, "launch-receipt", examples["receipt"]),
    )
    for family, model, slug, example in model_artifacts:
        artifact_id = f"urn:beadhive:wire-schema:{slug}:1"
        add(
            family=family,
            artifact_id=artifact_id,
            path=f"artifacts/{slug}-v1.schema.json",
            source_owner=f"{model.__module__}.{model.__name__}",
            policy=_SCHEMA_POLICY,
            document=_model_schema(model, artifact_id),
            example=example,
            assertion="valid-json-schema-instance",
        )

    artifacts.sort(key=lambda row: row["path"])
    cases.sort(key=lambda row: row["name"])
    ids = [row["id"] for row in artifacts]
    if len(ids) != len(set(ids)):
        raise ValueError("canonical sources produced duplicate artifact ids")
    if {row["family"] for row in artifacts} != OFFICIAL_V1_FAMILIES:
        raise ValueError("canonical sources did not cover every official-v1 family")
    return {
        "format_version": 1,
        "release_version": RELEASE_VERSION,
        "artifacts": artifacts,
        "conformance_cases": cases,
    }


def render_release(release: dict[str, Any] | None = None) -> dict[Path, bytes]:
    """Render byte-canonical files without writing or consulting external state."""

    release = build_release() if release is None else release
    files: dict[Path, bytes] = {}
    inventory_rows = []
    for artifact in sorted(release["artifacts"], key=lambda row: row["path"]):
        path = _safe_relative_path(artifact["path"])
        payload = _canonical_bytes(artifact["document"])
        files[path] = payload
        inventory_rows.append(
            {key: deepcopy(value) for key, value in artifact.items() if key != "document"}
            | {"sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}"}
        )
    files[Path("conformance.json")] = _canonical_bytes(
        {
            "format_version": 1,
            "release_version": release["release_version"],
            "cases": release["conformance_cases"],
        }
    )
    files[Path("inventory.json")] = _canonical_bytes(
        {
            "format_version": 1,
            "release_version": release["release_version"],
            "schema_draft": "https://json-schema.org/draft/2020-12/schema",
            "artifacts": inventory_rows,
            "conformance_fixtures": "conformance.json",
        }
    )
    return dict(sorted(files.items(), key=lambda item: item[0].as_posix()))


def _safe_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("artifact path must be a non-empty string")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise ValueError(f"artifact path escapes release root: {value!r}")
    normalized = pure.as_posix()
    if normalized != value or pure.parts[0] != "artifacts":
        raise ValueError(f"artifact path escapes release root: {value!r}")
    return Path(*pure.parts)


def _assert_safe_root(root: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"release target is a symlink: {root}")
    cursor = root
    while cursor != cursor.parent:
        if cursor.exists() and cursor.is_symlink():
            raise ValueError(f"release target contains a symlink: {cursor}")
        cursor = cursor.parent


def _symlink_component(root: Path, relative: Path) -> Path | None:
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            return cursor
    return None


def _remove_staged_tree(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _atomic_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _publish_staged_release(staging: Path, target: Path) -> None:
    """Publish one validated directory, restoring the old target on swap failure."""

    if not target.exists():
        _atomic_replace(staging, target)
        return
    backup = staging.with_name(f"{staging.name}.previous")
    _atomic_replace(target, backup)
    try:
        _atomic_replace(staging, target)
    except BaseException:
        _atomic_replace(backup, target)
        raise
    try:
        _remove_staged_tree(backup)
    except OSError:
        # Publication already committed atomically. A hidden, uniquely named prior tree is safe
        # to leave for later housekeeping; surfacing cleanup as a failed publication would lie.
        pass


def write_release(root: Path | None = None) -> None:
    """Compatibility-gate and atomically publish one exact, pre-validated release tree."""

    target = release_root() if root is None else Path(root)
    _assert_safe_root(target)
    if target.exists() and not target.is_dir():
        raise ValueError(f"release target is not a directory: {target}")
    candidate = build_release()
    compatibility = _published_compatibility_errors(candidate)
    if compatibility:
        raise ValueError(
            "candidate violates published compatibility baseline: " + "; ".join(compatibility)
        )
    files = render_release(candidate)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_root(target.parent)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        for relative, payload in files.items():
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        staged_errors = _validate_release_candidate(staging, candidate, check_compatibility=False)
        if staged_errors:
            raise ValueError("staged contract release is invalid: " + "; ".join(staged_errors))
        _publish_staged_release(staging, target)
    finally:
        _remove_staged_tree(staging)


@dataclass(frozen=True)
class _ReleaseSnapshot:
    files: dict[Path, bytes]
    inventory: object | None
    conformance: object | None
    documents: dict[Path, object]
    entries: dict[Path, str]


def _snapshot_digest(snapshot: _ReleaseSnapshot) -> str:
    digest = hashlib.sha256()
    for relative, payload in sorted(snapshot.files.items(), key=lambda item: item[0].as_posix()):
        encoded_path = relative.as_posix().encode()
        digest.update(encoded_path)
        digest.update(b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_no_follow(path: Path) -> int:
    """Open every path component once without following symlinks."""

    absolute = path.absolute()
    current = os.open(os.sep, _directory_open_flags())
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, _directory_open_flags(), dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _read_snapshot_file(
    root_descriptor: int, relative: Path, label: str
) -> tuple[bytes | None, str | None]:
    """Read a stable regular file through already-verified directory descriptors."""

    directory = os.dup(root_descriptor)
    descriptor: int | None = None
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, _directory_open_flags(), dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            relative.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None, f"{label}: symlink or non-regular release file is forbidden"
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        )
        if before_identity != after_identity:
            return None, f"{label}: release file changed while being read"
        return b"".join(chunks), None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
            return None, f"{label}: symlink or unsafe release path is forbidden"
        return None, f"{label}: cannot read JSON: {exc}"
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _scan_release_tree(root_descriptor: int) -> tuple[dict[Path, str], list[str]]:
    """Inventory one release tree without following any directory or file symlink."""

    entries: dict[Path, str] = {}
    errors: list[str] = []

    def walk(directory: int, prefix: Path) -> None:
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            errors.append(f"{prefix.as_posix() or '.'}: cannot list release directory: {exc}")
            return
        for name in names:
            relative = prefix / name
            try:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError as exc:
                errors.append(f"{relative.as_posix()}: cannot inspect release entry: {exc}")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                entries[relative] = "directory"
                try:
                    child = os.open(name, _directory_open_flags(), dir_fd=directory)
                except OSError as exc:
                    errors.append(
                        f"{relative.as_posix()}: cannot open release directory "
                        f"without following: {exc}"
                    )
                    continue
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                entries[relative] = "file"
            elif stat.S_ISLNK(metadata.st_mode):
                entries[relative] = "symlink"
                errors.append(f"{relative.as_posix()}: symlink release entry is forbidden")
            else:
                entries[relative] = "non-regular"
                errors.append(f"{relative.as_posix()}: non-regular release entry is forbidden")

    root_copy = os.dup(root_descriptor)
    try:
        walk(root_copy, Path())
    finally:
        os.close(root_copy)
    return entries, errors


def _canonical_payload(payload: bytes | None, label: str) -> tuple[object | None, list[str]]:
    errors: list[str] = []
    if payload is None:
        return None, errors
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        return None, [f"{label}: cannot read JSON: {exc}"]
    if payload != _canonical_bytes(value):
        errors.append(f"{label}: canonical JSON bytes drifted")
    return value, errors


def _read_release_snapshot(root: Path) -> tuple[_ReleaseSnapshot, list[str]]:
    files: dict[Path, bytes] = {}
    documents: dict[Path, object] = {}
    entries: dict[Path, str] = {}
    errors: list[str] = []
    root_descriptor: int | None = None
    try:
        root_descriptor = _open_directory_no_follow(root)
    except OSError as exc:
        message = (
            "release root contains a symlink or unsafe path component"
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}
            else f"cannot open release root: {exc}"
        )
        return _ReleaseSnapshot(files, None, None, documents, entries), [f"{root}: {message}"]
    try:
        entries, tree_errors = _scan_release_tree(root_descriptor)
        errors.extend(tree_errors)
        inventory_payload, inventory_read_error = _read_snapshot_file(
            root_descriptor, Path("inventory.json"), "inventory.json"
        )
        if inventory_read_error:
            errors.append(inventory_read_error)
        elif inventory_payload is not None:
            files[Path("inventory.json")] = inventory_payload
        conformance_payload, conformance_read_error = _read_snapshot_file(
            root_descriptor, Path("conformance.json"), "conformance.json"
        )
        if conformance_read_error:
            errors.append(conformance_read_error)
        elif conformance_payload is not None:
            files[Path("conformance.json")] = conformance_payload
        inventory_value, inventory_errors = _canonical_payload(inventory_payload, "inventory.json")
        conformance_value, conformance_errors = _canonical_payload(
            conformance_payload, "conformance.json"
        )
        errors.extend(inventory_errors)
        errors.extend(conformance_errors)
        rows = inventory_value.get("artifacts") if isinstance(inventory_value, dict) else None
        seen_paths: set[Path] = set()
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    relative = _safe_relative_path(row.get("path"))
                except ValueError:
                    continue
                if relative in seen_paths:
                    continue
                seen_paths.add(relative)
                payload, read_error = _read_snapshot_file(
                    root_descriptor, relative, relative.as_posix()
                )
                if read_error:
                    errors.append(read_error)
                    continue
                assert payload is not None
                files[relative] = payload
                document, document_errors = _canonical_payload(payload, relative.as_posix())
                errors.extend(document_errors)
                if document is not None:
                    documents[relative] = document
        return (
            _ReleaseSnapshot(files, inventory_value, conformance_value, documents, entries),
            errors,
        )
    finally:
        os.close(root_descriptor)


def _check_exact_keys(value: dict[str, Any], expected: frozenset[str], label: str) -> str | None:
    actual = set(value)
    if actual == expected:
        return None
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    return f"{label}: exact keys required (missing={missing!r}, extra={extra!r})"


def _exact_tree_errors(entries: dict[Path, str], expected_files: set[Path]) -> list[str]:
    expected_directories: set[Path] = set()
    for expected_file in expected_files:
        for parent in expected_file.parents:
            if parent == Path("."):
                break
            expected_directories.add(parent)
    expected_entries = expected_files | expected_directories
    errors = [
        f"{relative.as_posix()}: unlisted release entry ({entries[relative]})"
        for relative in sorted(entries.keys() - expected_entries, key=lambda path: path.as_posix())
    ]
    for relative in sorted(expected_files, key=lambda path: path.as_posix()):
        kind = entries.get(relative)
        if kind is not None and kind != "file":
            errors.append(f"{relative.as_posix()}: expected regular release file, found {kind}")
    for relative in sorted(expected_directories, key=lambda path: path.as_posix()):
        kind = entries.get(relative)
        if kind is not None and kind != "directory":
            errors.append(f"{relative.as_posix()}: expected release directory, found {kind}")
    return errors


def _integrity_errors(
    snapshot: _ReleaseSnapshot,
    *,
    expected_release_version: str = RELEASE_VERSION,
    enforce_current_inventory: bool = True,
) -> list[str]:
    errors: list[str] = []
    inventory_value = snapshot.inventory
    fixtures_value = snapshot.conformance
    if not isinstance(inventory_value, dict) or not isinstance(fixtures_value, dict):
        return errors
    for document, expected_keys, label in (
        (inventory_value, _INVENTORY_KEYS, "inventory.json"),
        (fixtures_value, _CONFORMANCE_KEYS, "conformance.json"),
    ):
        if key_error := _check_exact_keys(document, expected_keys, label):
            errors.append(key_error)
    for document, label in (
        (inventory_value, "inventory.json"),
        (fixtures_value, "conformance.json"),
    ):
        if document.get("format_version") != 1:
            errors.append(f"{label}: format_version must be 1")
        if document.get("release_version") != expected_release_version:
            errors.append(f"{label}: release_version must be {expected_release_version}")
    if inventory_value.get("schema_draft") != "https://json-schema.org/draft/2020-12/schema":
        errors.append("inventory.json: unsupported schema_draft")
    if inventory_value.get("conformance_fixtures") != "conformance.json":
        errors.append("inventory.json: conformance_fixtures must stay release-local")
    rows = inventory_value.get("artifacts")
    cases_value = fixtures_value.get("cases")
    if not isinstance(rows, list) or not isinstance(cases_value, list):
        return errors + ["inventory artifacts and conformance cases must be arrays"]
    actual_row_identities = [
        (row.get("id"), row.get("path"))
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and isinstance(row.get("path"), str)
    ]
    if enforce_current_inventory:
        if set(actual_row_identities) != set(_OFFICIAL_V1_ARTIFACT_IDENTITIES):
            errors.append("inventory.json: canonical artifact set drifted")
        elif tuple(actual_row_identities) != _OFFICIAL_V1_ARTIFACT_IDENTITIES:
            errors.append("inventory.json: canonical artifact order drifted")
    actual_case_identities = [
        (case.get("name"), case.get("artifact_id"))
        for case in cases_value
        if isinstance(case, dict)
        and isinstance(case.get("name"), str)
        and isinstance(case.get("artifact_id"), str)
    ]
    if enforce_current_inventory:
        if set(actual_case_identities) != set(_OFFICIAL_V1_CASE_IDENTITIES):
            errors.append("conformance.json: canonical case set drifted")
        elif tuple(actual_case_identities) != _OFFICIAL_V1_CASE_IDENTITIES:
            errors.append("conformance.json: canonical case order drifted")
    case_rows: dict[str, dict[str, Any]] = {}
    seen_case_artifact_ids: set[str] = set()
    for index, case in enumerate(cases_value):
        label = f"conformance.cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{label}: expected object")
            continue
        if key_error := _check_exact_keys(case, _CONFORMANCE_CASE_KEYS, label):
            errors.append(key_error)
        name = case.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{label}.name: expected non-empty string")
        elif name in case_rows:
            errors.append(f"{label}.name: duplicate conformance case name {name!r}")
        else:
            case_rows[name] = case
        artifact_id = case.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            errors.append(f"{label}.artifact_id: expected non-empty string")
        elif artifact_id in seen_case_artifact_ids:
            errors.append(f"{label}.artifact_id: duplicate conformance artifact id {artifact_id!r}")
        else:
            seen_case_artifact_ids.add(artifact_id)
        if not isinstance(case.get("assertion"), str) or not case["assertion"]:
            errors.append(f"{label}.assertion: expected non-empty string")
        if "input" not in case:
            errors.append(f"{label}.input: required")
    case_names = set(case_rows)
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, row in enumerate(rows):
        label = f"inventory.artifacts[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label}: expected object")
            continue
        if key_error := _check_exact_keys(row, _ARTIFACT_ROW_KEYS, label):
            errors.append(key_error)
        artifact_id = row.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            errors.append(f"{label}.id: expected non-empty string")
        elif artifact_id in seen_ids:
            errors.append(f"{label}.id: duplicate artifact id {artifact_id!r}")
        else:
            seen_ids.add(artifact_id)
        try:
            relative = _safe_relative_path(row.get("path"))
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if relative.as_posix() in seen_paths:
            errors.append(f"{label}.path: duplicate path {relative.as_posix()!r}")
        seen_paths.add(relative.as_posix())
        document = snapshot.documents.get(relative)
        if not isinstance(document, dict):
            continue
        if row.get("kind") == "json-schema" and document.get("$id") != artifact_id:
            errors.append(f"{label}.id: document identity does not match")
        if row.get("kind") == "json-schema" and document.get("version") != row.get("version"):
            errors.append(f"{label}.version: document version does not match")
        if row.get("kind") == "catalog" and "$id" in document and document["$id"] != artifact_id:
            errors.append(f"{label}.id: document identity does not match")
        family = row.get("family")
        if family not in OFFICIAL_V1_FAMILIES:
            errors.append(f"{label}.family: unknown official-v1 family")
        if row.get("version") != RELEASE_MAJOR:
            errors.append(f"{label}.version: expected {RELEASE_MAJOR}")
        kind = row.get("kind")
        expected_policy = {
            "json-schema": _SCHEMA_POLICY,
            "catalog": _CATALOG_POLICY,
            "openapi": _OPENAPI_POLICY,
        }.get(kind)
        if expected_policy is None:
            errors.append(f"{label}.kind: unsupported artifact kind")
        elif row.get("compatibility_policy") != expected_policy:
            errors.append(f"{label}.compatibility_policy: incompatible policy for {kind}")
        payload = snapshot.files.get(relative)
        if payload is None:
            continue
        actual = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if row.get("sha256") != actual:
            errors.append(f"{label}.sha256: checksum mismatch")
        for field in ("source_owner", "compatibility_policy"):
            if not isinstance(row.get(field), str) or not row[field]:
                errors.append(f"{label}.{field}: expected non-empty string")
        owner = row.get("source_owner")
        if isinstance(owner, str) and not (
            owner.startswith("beadhive.") or owner.startswith("docs/schemas/wire/")
        ):
            errors.append(f"{label}.source_owner: external source references are forbidden")
        examples = row.get("examples")
        if (
            not isinstance(examples, list)
            or not examples
            or any(not isinstance(name, str) for name in examples)
        ):
            errors.append(f"{label}.examples: expected non-empty fixture references")
        elif not set(examples) <= case_names:
            errors.append(f"{label}.examples: unknown conformance fixture")
        else:
            for name in examples:
                case = case_rows[name]
                if case.get("artifact_id") != artifact_id:
                    errors.append(f"{label}.examples: fixture artifact identity drifted")
                if not isinstance(case.get("assertion"), str) or not case["assertion"]:
                    errors.append(f"{label}.examples: fixture assertion is missing")
                if "input" not in case:
                    errors.append(f"{label}.examples: fixture input is missing")
    families = {
        row.get("family")
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("family"), str)
    }
    if enforce_current_inventory and families != OFFICIAL_V1_FAMILIES:
        errors.append("inventory.json: official-v1 family coverage drifted")
    fixture_ids = {
        case.get("artifact_id")
        for case in cases_value
        if isinstance(case, dict) and isinstance(case.get("artifact_id"), str)
    }
    if fixture_ids != seen_ids:
        errors.append("conformance.json: artifact coverage drifted")
    expected_files = {Path("inventory.json"), Path("conformance.json")}
    expected_files.update(Path(*PurePosixPath(path).parts) for path in seen_paths)
    errors.extend(_exact_tree_errors(snapshot.entries, expected_files))
    return errors


def _validate_release_candidate(
    target: Path, candidate: dict[str, Any], *, check_compatibility: bool = True
) -> tuple[str, ...]:
    expected = render_release(candidate)
    snapshot, errors = _read_release_snapshot(target)
    errors.extend(_exact_tree_errors(snapshot.entries, set(expected)))
    errors.extend(_integrity_errors(snapshot))
    if check_compatibility:
        try:
            errors.extend(
                f"compatibility baseline: {error}"
                for error in _published_compatibility_errors(candidate)
            )
        except ValueError as exc:
            errors.append(f"compatibility baseline is invalid: {exc}")
    for relative, payload in expected.items():
        actual = snapshot.files.get(relative)
        if actual is None:
            errors.append(f"{relative.as_posix()}: missing generated file")
            continue
        if actual != payload:
            if relative == Path("inventory.json"):
                expected_inventory = json.loads(payload)
                actual_inventory = (
                    snapshot.inventory if isinstance(snapshot.inventory, dict) else {}
                )
                for index, expected_row in enumerate(expected_inventory["artifacts"]):
                    actual_rows = actual_inventory.get("artifacts", [])
                    if index >= len(actual_rows) or not isinstance(actual_rows[index], dict):
                        break
                    for field in ("source_owner", "compatibility_policy", "examples", "sha256"):
                        if actual_rows[index].get(field) != expected_row[field]:
                            errors.append(
                                f"inventory.artifacts[{index}].{field}: generated metadata drift"
                            )
            errors.append(f"{relative.as_posix()}: generated bytes drifted")
    return tuple(dict.fromkeys(errors))


def validate_release(root: Path | None = None) -> tuple[str, ...]:
    """Check the published baseline, integrity, exact tree, and current-owner drift."""

    target = release_root() if root is None else Path(root)
    return _validate_release_candidate(target, build_release())


def _release_from_snapshot(snapshot: _ReleaseSnapshot) -> dict[str, Any]:
    assert isinstance(snapshot.inventory, dict)
    assert isinstance(snapshot.conformance, dict)
    inventory = snapshot.inventory
    conformance = snapshot.conformance
    artifacts = []
    for row in inventory["artifacts"]:
        relative = _safe_relative_path(row["path"])
        artifacts.append(
            {key: deepcopy(value) for key, value in row.items() if key != "sha256"}
            | {"document": deepcopy(snapshot.documents[relative])}
        )
    return {
        "format_version": inventory["format_version"],
        "release_version": inventory["release_version"],
        "artifacts": artifacts,
        "conformance_cases": conformance["cases"],
    }


def _load_release_snapshot(
    root: Path,
    *,
    expected_digest: str | None = None,
    expected_release_version: str = RELEASE_VERSION,
    enforce_current_inventory: bool = True,
) -> dict[str, Any]:
    snapshot, errors = _read_release_snapshot(root)
    errors.extend(
        _integrity_errors(
            snapshot,
            expected_release_version=expected_release_version,
            enforce_current_inventory=enforce_current_inventory,
        )
    )
    if expected_digest is not None:
        actual_digest = _snapshot_digest(snapshot)
        if actual_digest != expected_digest:
            errors.append(
                "published snapshot digest mismatch: "
                f"expected {expected_digest}, got {actual_digest}"
            )
    if errors:
        raise ValueError("invalid contract release: " + "; ".join(errors))
    return _release_from_snapshot(snapshot)


def load_release(root: Path | None = None) -> dict[str, Any]:
    """Load a checksum-verified release into the same public shape as ``build_release``."""

    target = release_root() if root is None else Path(root)
    return _load_release_snapshot(target)


def load_published_baseline() -> dict[str, Any]:
    """Load the pinned prior-published v1 bundle through one no-follow snapshot."""

    return _load_release_snapshot(
        published_baseline_root(),
        expected_digest=_PUBLISHED_BASELINE_SHA256,
        expected_release_version=_PUBLISHED_BASELINE_VERSION,
        enforce_current_inventory=False,
    )


def load_artifact(artifact_id: str, root: Path | None = None) -> dict[str, Any]:
    """Return one artifact only after path, canonical-byte, identity, and checksum validation."""

    release = load_release(root)
    matches = [row for row in release["artifacts"] if row["id"] == artifact_id]
    if len(matches) != 1:
        raise KeyError(f"unknown or ambiguous contract artifact: {artifact_id}")
    return matches[0]["document"]


_MISSING = object()

_SCHEMA_ANNOTATION_KEYWORDS = frozenset(
    {
        "$comment",
        "contentEncoding",
        "contentMediaType",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "version",
        "writeOnly",
    }
)
_SCHEMA_ASSERTION_KEYWORDS = frozenset(
    {
        "$anchor",
        "$defs",
        "$dynamicAnchor",
        "$dynamicRef",
        "$id",
        "$recursiveAnchor",
        "$recursiveRef",
        "$ref",
        "$schema",
        "$vocabulary",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "contains",
        "contentSchema",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "if",
        "items",
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "patternProperties",
        "prefixItems",
        "properties",
        "propertyNames",
        "required",
        "then",
        "type",
        "unevaluatedItems",
        "unevaluatedProperties",
        "uniqueItems",
    }
)
_SCHEMA_KNOWN_KEYWORDS = _SCHEMA_ANNOTATION_KEYWORDS | _SCHEMA_ASSERTION_KEYWORDS | {"definitions"}


def _schema_types(value: object) -> set[str] | None:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return None


def _same_json(left: object, right: object) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return _canonical_bytes(left) == _canonical_bytes(right)


def _numeric_fraction(value: object) -> Fraction | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None


def _lower_bound_errors(
    old: dict[str, Any],
    candidate: dict[str, Any],
    keyword: str,
    path: str,
    errors: list[str],
    *,
    default: int | None = None,
) -> None:
    old_value = old.get(keyword, default if default is not None else _MISSING)
    new_value = candidate.get(keyword, default if default is not None else _MISSING)
    if new_value is _MISSING:
        return
    if old_value is _MISSING:
        errors.append(f"{path}.{keyword}: new lower bound narrows the schema")
        return
    old_number = _numeric_fraction(old_value)
    new_number = _numeric_fraction(new_value)
    if old_number is None or new_number is None:
        if not _same_json(old_value, new_value):
            errors.append(f"{path}.{keyword}: lower-bound assertion changed")
    elif new_number > old_number:
        errors.append(f"{path}.{keyword}: lower bound narrowed")


def _upper_bound_errors(
    old: dict[str, Any],
    candidate: dict[str, Any],
    keyword: str,
    path: str,
    errors: list[str],
) -> None:
    old_value = old.get(keyword, _MISSING)
    new_value = candidate.get(keyword, _MISSING)
    if new_value is _MISSING:
        return
    if old_value is _MISSING:
        errors.append(f"{path}.{keyword}: new upper bound narrows the schema")
        return
    old_number = _numeric_fraction(old_value)
    new_number = _numeric_fraction(new_value)
    if old_number is None or new_number is None:
        if not _same_json(old_value, new_value):
            errors.append(f"{path}.{keyword}: upper-bound assertion changed")
    elif new_number < old_number:
        errors.append(f"{path}.{keyword}: upper bound narrowed")


def _schema_compatibility_errors(
    old: object, candidate: object, path: str, errors: list[str]
) -> None:
    """Reject candidate JSON Schemas that remove stable shape or narrow accepted values."""

    if isinstance(old, bool):
        if old and candidate is not True:
            errors.append(f"{path}: JSON Schema narrowed from true")
        return
    if not isinstance(old, dict):
        return
    if candidate is True:
        return
    if not isinstance(candidate, dict):
        errors.append(f"{path}: JSON Schema was removed or narrowed")
        return

    unknown_keywords = {
        keyword
        for keyword in old.keys() | candidate.keys()
        if keyword not in _SCHEMA_KNOWN_KEYWORDS and not keyword.startswith("x-")
    }
    for keyword in sorted(unknown_keywords):
        if not _same_json(old.get(keyword, _MISSING), candidate.get(keyword, _MISSING)):
            errors.append(f"{path}.{keyword}: unknown JSON Schema assertion changed; fail closed")

    for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
        old_ref = old.get(keyword, _MISSING)
        new_ref = candidate.get(keyword, _MISSING)
        if old_ref is not _MISSING and new_ref is not _MISSING and new_ref != old_ref:
            errors.append(f"{path}.{keyword}: schema reference changed")
        elif old_ref is _MISSING and new_ref is not _MISSING:
            errors.append(f"{path}.{keyword}: schema reference narrowed an unconstrained value")
    for keyword in (
        "$schema",
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$recursiveAnchor",
        "$vocabulary",
    ):
        if not _same_json(old.get(keyword, _MISSING), candidate.get(keyword, _MISSING)):
            errors.append(f"{path}.{keyword}: schema identity or dialect changed")

    old_types = _schema_types(old.get("type"))
    new_types = _schema_types(candidate.get("type"))
    if old_types is None and new_types is not None:
        errors.append(f"{path}.type: type narrowed from unconstrained")
    elif old_types is not None and new_types is not None and not old_types <= new_types:
        errors.append(
            f"{path}.type: type narrowed from {sorted(old_types)!r} to {sorted(new_types)!r}"
        )

    old_required = old.get("required", [])
    new_required = candidate.get("required", [])
    if isinstance(old_required, list) and isinstance(new_required, list):
        added_required = set(new_required) - set(old_required)
        if added_required:
            errors.append(
                f"{path}.required: required properties changed; newly required "
                f"{sorted(added_required)!r}"
            )

    old_enum = old.get("enum", _MISSING)
    new_enum = candidate.get("enum", _MISSING)
    if old_enum is not _MISSING:
        if new_enum is _MISSING or _canonical_bytes(old_enum) != _canonical_bytes(new_enum):
            errors.append(f"{path}.enum: closed-union members changed")
    elif old_enum is _MISSING and new_enum is not _MISSING:
        errors.append(f"{path}.enum: closed-union constraint was added")

    old_const = old.get("const", _MISSING)
    new_const = candidate.get("const", _MISSING)
    if old_const is not _MISSING and new_const not in (_MISSING, old_const):
        errors.append(f"{path}.const: constant value changed")
    elif old_const is _MISSING and new_const is not _MISSING:
        errors.append(f"{path}.const: constant constraint was added")

    for keyword in ("properties", "$defs", "definitions"):
        old_mapping = old.get(keyword)
        if not isinstance(old_mapping, dict):
            continue
        new_mapping = candidate.get(keyword)
        if not isinstance(new_mapping, dict):
            errors.append(f"{path}.{keyword}: schema mapping was removed")
            continue
        for name in sorted(old_mapping.keys() - new_mapping.keys()):
            noun = "property" if keyword == "properties" else "schema"
            errors.append(f"{path}.{keyword}.{name}: {noun} was removed or renamed")
        for name in sorted(old_mapping.keys() & new_mapping.keys()):
            _schema_compatibility_errors(
                old_mapping[name], new_mapping[name], f"{path}.{keyword}.{name}", errors
            )

    old_patterns = old.get("patternProperties")
    new_patterns = candidate.get("patternProperties")
    if isinstance(old_patterns, dict) or isinstance(new_patterns, dict):
        if not isinstance(old_patterns, dict) or not isinstance(new_patterns, dict):
            errors.append(f"{path}.patternProperties: pattern assertion mapping changed")
        elif old_patterns.keys() != new_patterns.keys():
            errors.append(f"{path}.patternProperties: pattern assertion members changed")
        else:
            for pattern in sorted(old_patterns):
                _schema_compatibility_errors(
                    old_patterns[pattern],
                    new_patterns[pattern],
                    f"{path}.patternProperties.{pattern}",
                    errors,
                )

    old_dependencies = old.get("dependentSchemas")
    new_dependencies = candidate.get("dependentSchemas")
    if isinstance(old_dependencies, dict):
        if new_dependencies is not None and not isinstance(new_dependencies, dict):
            errors.append(f"{path}.dependentSchemas: dependency schema mapping changed")
        elif isinstance(new_dependencies, dict):
            for name in sorted(new_dependencies.keys() - old_dependencies.keys()):
                errors.append(f"{path}.dependentSchemas.{name}: dependency assertion was added")
            for name in sorted(old_dependencies.keys() & new_dependencies.keys()):
                _schema_compatibility_errors(
                    old_dependencies[name],
                    new_dependencies[name],
                    f"{path}.dependentSchemas.{name}",
                    errors,
                )
    elif isinstance(new_dependencies, dict) and new_dependencies:
        errors.append(f"{path}.dependentSchemas: dependency assertions were added")

    for keyword in (
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "items",
        "contains",
        "propertyNames",
        "contentSchema",
    ):
        old_value = old.get(keyword, _MISSING)
        new_value = candidate.get(keyword, _MISSING)
        if old_value is _MISSING:
            if new_value is not _MISSING and new_value is not True:
                errors.append(f"{path}.{keyword}: new constraint narrows the schema")
            continue
        if new_value is _MISSING:
            continue
        _schema_compatibility_errors(old_value, new_value, f"{path}.{keyword}", errors)

    old_all_of = old.get("allOf", _MISSING)
    new_all_of = candidate.get("allOf", _MISSING)
    if old_all_of is _MISSING and new_all_of is not _MISSING:
        errors.append(f"{path}.allOf: schema composition constraint was added")
    elif isinstance(old_all_of, list) and isinstance(new_all_of, list):
        if len(new_all_of) > len(old_all_of):
            errors.append(f"{path}.allOf: conjunctive schema constraint was added")
        for index, new_branch in enumerate(new_all_of[: len(old_all_of)]):
            _schema_compatibility_errors(
                old_all_of[index], new_branch, f"{path}.allOf[{index}]", errors
            )
    elif new_all_of is not _MISSING and not _same_json(old_all_of, new_all_of):
        errors.append(f"{path}.allOf: schema composition changed")

    old_any_of = old.get("anyOf", _MISSING)
    new_any_of = candidate.get("anyOf", _MISSING)
    if old_any_of is _MISSING and new_any_of is not _MISSING:
        errors.append(f"{path}.anyOf: schema composition constraint was added")
    elif isinstance(old_any_of, list) and isinstance(new_any_of, list):
        if len(new_any_of) < len(old_any_of):
            errors.append(f"{path}.anyOf: alternative schema constraint was removed")
        for index, old_branch in enumerate(old_any_of[: len(new_any_of)]):
            _schema_compatibility_errors(
                old_branch, new_any_of[index], f"{path}.anyOf[{index}]", errors
            )
    elif new_any_of is not _MISSING and not _same_json(old_any_of, new_any_of):
        errors.append(f"{path}.anyOf: schema composition changed")

    for keyword in ("oneOf", "prefixItems"):
        old_value = old.get(keyword, _MISSING)
        new_value = candidate.get(keyword, _MISSING)
        if old_value is _MISSING and new_value is not _MISSING:
            errors.append(f"{path}.{keyword}: schema composition constraint was added")
        elif old_value is not _MISSING and not _same_json(old_value, new_value):
            errors.append(f"{path}.{keyword}: schema composition changed")

    old_condition = tuple(old.get(keyword, _MISSING) for keyword in ("if", "then", "else"))
    new_condition = tuple(candidate.get(keyword, _MISSING) for keyword in ("if", "then", "else"))
    if any(value is not _MISSING for value in new_condition) and any(
        not _same_json(old_value, new_value)
        for old_value, new_value in zip(old_condition, new_condition, strict=True)
    ):
        errors.append(f"{path}.if/then/else: conditional schema assertion changed")

    for keyword in ("pattern", "format"):
        old_value = old.get(keyword, _MISSING)
        new_value = candidate.get(keyword, _MISSING)
        if old_value is _MISSING and new_value is not _MISSING:
            errors.append(f"{path}.{keyword}: new constraint narrows the schema")
        elif old_value is not _MISSING and new_value not in (_MISSING, old_value):
            errors.append(f"{path}.{keyword}: constraint changed")

    old_not = old.get("not", _MISSING)
    new_not = candidate.get("not", _MISSING)
    if old_not is _MISSING and new_not is not _MISSING:
        errors.append(f"{path}.not: negated schema constraint was added")
    elif new_not is not _MISSING and not _same_json(old_not, new_not):
        errors.append(f"{path}.not: negated schema constraint changed")

    for keyword in ("minimum", "exclusiveMinimum"):
        _lower_bound_errors(old, candidate, keyword, path, errors)
    for keyword in ("minLength", "minItems", "minProperties"):
        _lower_bound_errors(old, candidate, keyword, path, errors, default=0)
    _lower_bound_errors(
        old,
        candidate,
        "minContains",
        path,
        errors,
        default=1 if "contains" in old else None,
    )
    for keyword in (
        "maximum",
        "exclusiveMaximum",
        "maxLength",
        "maxItems",
        "maxProperties",
        "maxContains",
    ):
        _upper_bound_errors(old, candidate, keyword, path, errors)

    old_multiple = old.get("multipleOf", _MISSING)
    new_multiple = candidate.get("multipleOf", _MISSING)
    if new_multiple is not _MISSING:
        old_factor = _numeric_fraction(old_multiple)
        new_factor = _numeric_fraction(new_multiple)
        if old_multiple is _MISSING:
            errors.append(f"{path}.multipleOf: new divisibility assertion narrows the schema")
        elif old_factor is None or new_factor is None or new_factor <= 0:
            if not _same_json(old_multiple, new_multiple):
                errors.append(f"{path}.multipleOf: divisibility assertion changed")
        elif (old_factor / new_factor).denominator != 1:
            errors.append(f"{path}.multipleOf: divisibility assertion narrowed")

    old_unique = old.get("uniqueItems", False)
    new_unique = candidate.get("uniqueItems", False)
    if old_unique is not True and new_unique is True:
        errors.append(f"{path}.uniqueItems: uniqueness assertion narrows the schema")
    elif not isinstance(new_unique, bool):
        errors.append(f"{path}.uniqueItems: uniqueness assertion is invalid")

    old_dependent_required = old.get("dependentRequired", {})
    new_dependent_required = candidate.get("dependentRequired", {})
    if isinstance(old_dependent_required, dict) and isinstance(new_dependent_required, dict):
        for name in sorted(new_dependent_required.keys() - old_dependent_required.keys()):
            errors.append(f"{path}.dependentRequired.{name}: dependency assertion was added")
        for name in sorted(old_dependent_required.keys() & new_dependent_required.keys()):
            old_names = old_dependent_required[name]
            new_names = new_dependent_required[name]
            if not isinstance(old_names, list) or not isinstance(new_names, list):
                if not _same_json(old_names, new_names):
                    errors.append(f"{path}.dependentRequired.{name}: dependency assertion changed")
                continue
            added_names = set(new_names) - set(old_names)
            if added_names:
                errors.append(
                    f"{path}.dependentRequired.{name}: newly required dependencies "
                    f"{sorted(added_names)!r}"
                )
    elif not _same_json(old_dependent_required, new_dependent_required):
        errors.append(f"{path}.dependentRequired: dependency assertions changed")


def _append_only_errors(
    old: object,
    candidate: object,
    path: str,
    errors: list[str],
    *,
    extension_key_context: bool = True,
) -> None:
    if isinstance(old, dict):
        if not isinstance(candidate, dict):
            errors.append(f"{path}: append-only catalog object was removed or changed")
            return
        for key in sorted(old.keys() - candidate.keys()):
            if extension_key_context and _is_known_access_extension_key(key):
                continue
            errors.append(f"{path}.{key}: append-only catalog member was removed")
        for key in sorted(candidate.keys() - old.keys()):
            if extension_key_context and _is_extension_like_key(key) and not _is_extension_key(key):
                errors.append(
                    f"{path}.{key}: non-canonical extension metadata key is not permitted; "
                    "use an exact lowercase ASCII 'x-' prefix"
                )
            elif (
                extension_key_context
                and _is_extension_key(key)
                and not _is_known_access_extension_key(key)
            ):
                errors.append(
                    f"{path}.{key}: extension metadata was added to an existing "
                    "append-only catalog member"
                )
            elif (
                extension_key_context
                and _is_access_metadata_key(key)
                and not _access_value_is_unrestricted(candidate[key])
            ):
                errors.append(
                    f"{path}.{key}: required access metadata was added to an existing "
                    "append-only catalog member"
                )
            else:
                _extension_tree_presence_errors(candidate[key], f"{path}.{key}", "added", errors)
        for key in sorted(old.keys() & candidate.keys()):
            if extension_key_context and _is_extension_like_key(key) and not _is_extension_key(key):
                errors.append(
                    f"{path}.{key}: non-canonical extension metadata key is not permitted; "
                    "use an exact lowercase ASCII 'x-' prefix"
                )
            elif extension_key_context and _is_known_access_extension_key(key):
                if not _access_value_is_no_stricter(old[key], candidate[key]):
                    errors.append(f"{path}.{key}: required access metadata tightened")
            elif extension_key_context and _is_extension_key(key):
                if key in _SEMANTIC_EXTENSION_CONTAINERS:
                    _extension_value_errors(
                        old[key],
                        candidate[key],
                        f"{path}.{key}",
                        errors,
                        semantic_access=True,
                    )
                elif not _same_json(old[key], candidate[key]):
                    errors.append(f"{path}.{key}: extension metadata changed")
            else:
                _append_only_errors(
                    old[key],
                    candidate[key],
                    f"{path}.{key}",
                    errors,
                    extension_key_context=key not in _NAMED_CONTRACT_MAP_KEYS,
                )
        return
    if isinstance(old, list):
        if not isinstance(candidate, list):
            errors.append(f"{path}: append-only catalog list was removed or changed")
            return
        if len(candidate) < len(old):
            errors.append(
                f"{path}: append-only catalog members were removed ({len(old)} -> {len(candidate)})"
            )
        for index, old_item in enumerate(old[: len(candidate)]):
            _append_only_errors(old_item, candidate[index], f"{path}[{index}]", errors)
        for index in range(len(old), len(candidate)):
            _extension_tree_presence_errors(candidate[index], f"{path}[{index}]", "added", errors)
        return
    if old != candidate:
        errors.append(f"{path}: append-only catalog value changed or members were reordered")


_ANONYMOUS_SECURITY = ((),)
_PUBLIC_ACCESS_VALUES = frozenset(
    {"", "anonymous", "none", "public", "public-liveness", "unrestricted"}
)
_KNOWN_ACCESS_METADATA_KEYS = frozenset(
    {
        "privilege",
        "transportPrivilege",
        "x-beadhive-auth-required",
        "x-beadhive-required-privilege",
        "x-beadhive-required-scope",
        "x-beadhive-required-scopes",
    }
)
_SEMANTIC_EXTENSION_CONTAINERS = frozenset(
    {"x-beadhive-catalog-projection", "x-beadhive-websocket"}
)
# Keys in these maps name public schemas, fields, or headers; they are data, not metadata keys.
# Their values immediately return to normal extension-key checking through _append_only_errors.
_NAMED_CONTRACT_MAP_KEYS = frozenset(
    {
        "$defs",
        "definitions",
        "dependentSchemas",
        "headers",
        "patternProperties",
        "properties",
        "schemas",
    }
)
_NON_NORMALIZING_X_CONFUSABLES = frozenset({"Х", "х"})
_NON_NORMALIZING_MINUS_CONFUSABLES = frozenset({"−"})


def _is_extension_key(key: object) -> bool:
    return isinstance(key, str) and key.startswith("x-")


def _is_extension_like_key(key: object) -> bool:
    """Classify confusable prefixes without rewriting contract-visible key bytes.

    NFKC is intentionally bounded to the single leading scalar so mathematical, modifier, and
    width variants of Latin x cannot bypass the canonical lowercase ASCII ``x-`` spelling.
    Cyrillic ha and MINUS SIGN do not have useful NFKC mappings, so the visually confusable
    scalars supported by this policy are listed explicitly.  The original key remains the sole
    value used in paths, equality, ordering, and diagnostics.
    """

    if not isinstance(key, str) or len(key) < 2:
        return False
    leader = key[0]
    separator = key[1]
    leader_is_x = (
        unicodedata.normalize("NFKC", leader).casefold() == "x"
        or leader in _NON_NORMALIZING_X_CONFUSABLES
    )
    separator_is_hyphen = (
        separator == "-"
        or unicodedata.category(separator) == "Pd"
        or separator in _NON_NORMALIZING_MINUS_CONFUSABLES
    )
    return leader_is_x and separator_is_hyphen


def _is_known_access_extension_key(key: object) -> bool:
    return _is_extension_key(key) and key in _KNOWN_ACCESS_METADATA_KEYS


def _is_access_metadata_key(key: object) -> bool:
    return isinstance(key, str) and key in _KNOWN_ACCESS_METADATA_KEYS


def _access_value_is_unrestricted(value: object) -> bool:
    if value is _MISSING or value is None or value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PUBLIC_ACCESS_VALUES
    return isinstance(value, (list, tuple, set, frozenset)) and not value


def _access_value_is_no_stricter(old: object, candidate: object) -> bool:
    if _access_value_is_unrestricted(candidate):
        return True
    if _access_value_is_unrestricted(old):
        return False
    if isinstance(old, (list, tuple)) and isinstance(candidate, (list, tuple)):
        if all(isinstance(item, str) for item in (*old, *candidate)):
            return set(candidate) <= set(old)
    return _same_json(old, candidate)


def _extension_tree_presence_errors(
    value: object, path: str, action: str, errors: list[str]
) -> None:
    """Report extension-like keys hidden anywhere in a newly added or removed tree."""

    if isinstance(value, dict):
        for key in sorted(value):
            child_path = f"{path}.{key}"
            if _is_extension_like_key(key):
                if _is_extension_key(key):
                    errors.append(f"{child_path}: extension metadata was {action}")
                else:
                    errors.append(
                        f"{child_path}: non-canonical extension metadata key is not permitted; "
                        "use an exact lowercase ASCII 'x-' prefix"
                    )
            else:
                _extension_tree_presence_errors(value[key], child_path, action, errors)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _extension_tree_presence_errors(item, f"{path}[{index}]", action, errors)


def _nested_extension_errors(old: object, candidate: object, path: str, errors: list[str]) -> None:
    """Exact-compare extension-like descendants while leaving ordinary metadata additive."""

    if old is _MISSING:
        _extension_tree_presence_errors(candidate, path, "added", errors)
        return
    if candidate is _MISSING:
        _extension_tree_presence_errors(old, path, "removed", errors)
        return
    if isinstance(old, dict) and isinstance(candidate, dict):
        for key in sorted(old.keys() | candidate.keys()):
            child_path = f"{path}.{key}"
            old_value = old.get(key, _MISSING)
            new_value = candidate.get(key, _MISSING)
            if _is_extension_like_key(key):
                if not _is_extension_key(key):
                    errors.append(
                        f"{child_path}: non-canonical extension metadata key is not permitted; "
                        "use an exact lowercase ASCII 'x-' prefix"
                    )
                elif old_value is _MISSING or new_value is _MISSING:
                    errors.append(f"{child_path}: extension metadata was added or removed")
                elif not _same_json(old_value, new_value):
                    errors.append(f"{child_path}: extension metadata changed")
            else:
                _nested_extension_errors(old_value, new_value, child_path, errors)
        return
    if isinstance(old, list) and isinstance(candidate, list):
        for index in range(max(len(old), len(candidate))):
            old_item = old[index] if index < len(old) else _MISSING
            new_item = candidate[index] if index < len(candidate) else _MISSING
            _nested_extension_errors(old_item, new_item, f"{path}[{index}]", errors)
        return
    if type(old) is not type(candidate):
        _extension_tree_presence_errors(old, path, "removed", errors)
        _extension_tree_presence_errors(candidate, path, "added", errors)


def _extension_value_errors(
    old: object,
    candidate: object,
    path: str,
    errors: list[str],
    *,
    semantic_access: bool = False,
    semantic_security: bool = False,
) -> None:
    """Require exact extension-map preservation, excluding reviewed semantic access leaves."""

    if isinstance(old, dict) and isinstance(candidate, dict):
        for key in sorted(old.keys() | candidate.keys()):
            if semantic_security and key == "security":
                continue
            old_value = old.get(key, _MISSING)
            new_value = candidate.get(key, _MISSING)
            if semantic_access and _is_access_metadata_key(key):
                if not _access_value_is_no_stricter(old_value, new_value):
                    errors.append(f"{path}.{key}: required access metadata tightened")
            elif old_value is _MISSING or new_value is _MISSING:
                errors.append(f"{path}.{key}: extension metadata was added or removed")
            elif _is_extension_key(key) and key not in _SEMANTIC_EXTENSION_CONTAINERS:
                if not _same_json(old_value, new_value):
                    errors.append(f"{path}.{key}: extension metadata changed")
            else:
                _extension_value_errors(old_value, new_value, f"{path}.{key}", errors)
        return
    if isinstance(old, list) and isinstance(candidate, list):
        if len(old) != len(candidate):
            errors.append(f"{path}: extension metadata members changed")
            return
        for index, (old_item, new_item) in enumerate(zip(old, candidate, strict=True)):
            _extension_value_errors(old_item, new_item, f"{path}[{index}]", errors)
        return
    if not _same_json(old, candidate):
        errors.append(f"{path}: extension metadata changed")


def _openapi_extension_errors(
    old: dict[str, Any],
    candidate: dict[str, Any],
    path: str,
    errors: list[str],
    *,
    nested_exclusions: frozenset[str] = frozenset(),
) -> None:
    """Fail closed for extension metadata on an already-published OpenAPI member."""

    extension_keys = {key for key in old.keys() | candidate.keys() if _is_extension_like_key(key)}
    for key in sorted(extension_keys):
        if not _is_extension_key(key):
            errors.append(
                f"{path}.{key}: non-canonical extension metadata key is not permitted; "
                "use an exact lowercase ASCII 'x-' prefix"
            )
            continue
        if _is_known_access_extension_key(key):
            continue
        old_value = old.get(key, _MISSING)
        new_value = candidate.get(key, _MISSING)
        if old_value is _MISSING or new_value is _MISSING:
            errors.append(f"{path}.{key}: extension metadata was added or removed")
        elif key in _SEMANTIC_EXTENSION_CONTAINERS:
            _extension_value_errors(
                old_value,
                new_value,
                f"{path}.{key}",
                errors,
                semantic_access=True,
                semantic_security=key == "x-beadhive-websocket",
            )
        elif not _same_json(old_value, new_value):
            errors.append(f"{path}.{key}: extension metadata changed")
    for key in sorted((old.keys() | candidate.keys()) - extension_keys - nested_exclusions):
        _nested_extension_errors(
            old.get(key, _MISSING), candidate.get(key, _MISSING), f"{path}.{key}", errors
        )


def _local_access_metadata(value: dict[str, Any]) -> dict[str, object]:
    metadata = {key: item for key, item in value.items() if _is_access_metadata_key(key)}
    projection = value.get("x-beadhive-catalog-projection")

    def collect(item: object, item_path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                child_path = f"{item_path}.{key}"
                if _is_access_metadata_key(key):
                    metadata[child_path] = child
                else:
                    collect(child, child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                collect(child, f"{item_path}[{index}]")

    if isinstance(projection, dict):
        collect(projection, "x-beadhive-catalog-projection")
    return metadata


def _effective_access_metadata(
    parent: dict[str, object], value: dict[str, Any]
) -> dict[str, object]:
    return parent | _local_access_metadata(value)


def _normalize_openapi_security(
    value: object,
) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], ...] | None:
    if not isinstance(value, list):
        return None
    if not value:
        return _ANONYMOUS_SECURITY
    alternatives: list[tuple[tuple[str, tuple[str, ...]], ...]] = []
    for requirement in value:
        if not isinstance(requirement, dict):
            return None
        schemes: list[tuple[str, tuple[str, ...]]] = []
        for name, scopes in requirement.items():
            if (
                not isinstance(name, str)
                or not isinstance(scopes, list)
                or not all(isinstance(scope, str) for scope in scopes)
            ):
                return None
            schemes.append((name, tuple(sorted(set(scopes)))))
        alternatives.append(tuple(sorted(schemes)))
    return tuple(alternatives)


def _effective_openapi_security(
    parent: tuple[tuple[tuple[str, tuple[str, ...]], ...], ...] | None,
    value: dict[str, Any],
) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], ...] | None:
    if "security" not in value:
        return parent
    return _normalize_openapi_security(value["security"])


def _security_alternative_is_no_stricter(
    old: tuple[tuple[str, tuple[str, ...]], ...],
    candidate: tuple[tuple[str, tuple[str, ...]], ...],
) -> bool:
    old_schemes = {name: set(scopes) for name, scopes in old}
    for name, scopes in candidate:
        if name not in old_schemes or not set(scopes) <= old_schemes[name]:
            return False
    return True


def _openapi_access_errors(
    old_security: tuple[tuple[tuple[str, tuple[str, ...]], ...], ...] | None,
    new_security: tuple[tuple[tuple[str, tuple[str, ...]], ...], ...] | None,
    old_metadata: dict[str, object],
    new_metadata: dict[str, object],
    path: str,
    errors: list[str],
) -> None:
    if old_security is None or new_security is None:
        if old_security != new_security:
            errors.append(f"{path}.security: OpenAPI access requirements are invalid or changed")
    else:
        for old_alternative in old_security:
            if not any(
                _security_alternative_is_no_stricter(old_alternative, new_alternative)
                for new_alternative in new_security
            ):
                errors.append(
                    f"{path}.security: OpenAPI access requirements tightened or an "
                    "accepted alternative was removed"
                )
                break
    for key in sorted(old_metadata.keys() | new_metadata.keys()):
        old_value = old_metadata.get(key, _MISSING)
        new_value = new_metadata.get(key, _MISSING)
        if not _access_value_is_no_stricter(old_value, new_value):
            errors.append(f"{path}.{key}: OpenAPI access metadata tightened")


def _openapi_security_scheme_errors(
    old: object, candidate: object, path: str, errors: list[str]
) -> None:
    if not isinstance(old, dict):
        return
    if not isinstance(candidate, dict):
        errors.append(f"{path}: OpenAPI named access schemes were removed")
        return
    for name in sorted(old.keys() - candidate.keys()):
        errors.append(f"{path}.{name}: OpenAPI named access scheme was removed")
    for name in sorted(old.keys() & candidate.keys()):
        old_scheme = old[name]
        new_scheme = candidate[name]
        if not isinstance(old_scheme, dict) or not isinstance(new_scheme, dict):
            if not _same_json(old_scheme, new_scheme):
                errors.append(f"{path}.{name}: OpenAPI named access scheme changed")
            continue
        for field in ("type", "scheme", "name", "in", "openIdConnectUrl"):
            if not _same_json(old_scheme.get(field, _MISSING), new_scheme.get(field, _MISSING)):
                errors.append(
                    f"{path}.{name}.{field}: OpenAPI named access scheme identity changed"
                )
        _openapi_access_errors(
            _ANONYMOUS_SECURITY,
            _ANONYMOUS_SECURITY,
            _local_access_metadata(old_scheme),
            _local_access_metadata(new_scheme),
            f"{path}.{name}",
            errors,
        )
        _openapi_extension_errors(old_scheme, new_scheme, f"{path}.{name}", errors)
        old_flows = old_scheme.get("flows")
        new_flows = new_scheme.get("flows")
        if not isinstance(old_flows, dict):
            continue
        if not isinstance(new_flows, dict):
            errors.append(f"{path}.{name}.flows: OpenAPI access flows were removed")
            continue
        for flow_name in sorted(old_flows.keys() - new_flows.keys()):
            errors.append(f"{path}.{name}.flows.{flow_name}: OpenAPI access flow was removed")
        for flow_name in sorted(old_flows.keys() & new_flows.keys()):
            old_flow = old_flows[flow_name]
            new_flow = new_flows[flow_name]
            if not isinstance(old_flow, dict) or not isinstance(new_flow, dict):
                if not _same_json(old_flow, new_flow):
                    errors.append(f"{path}.{name}.flows.{flow_name}: OpenAPI access flow changed")
                continue
            for field in ("authorizationUrl", "tokenUrl", "refreshUrl"):
                if not _same_json(old_flow.get(field, _MISSING), new_flow.get(field, _MISSING)):
                    errors.append(
                        f"{path}.{name}.flows.{flow_name}.{field}: OpenAPI access flow changed"
                    )
            old_scopes = old_flow.get("scopes", {})
            new_scopes = new_flow.get("scopes", {})
            if isinstance(old_scopes, dict) and isinstance(new_scopes, dict):
                for scope in sorted(old_scopes.keys() - new_scopes.keys()):
                    errors.append(
                        f"{path}.{name}.flows.{flow_name}.scopes.{scope}: "
                        "OpenAPI named access scope was removed"
                    )
            elif not _same_json(old_scopes, new_scopes):
                errors.append(
                    f"{path}.{name}.flows.{flow_name}.scopes: OpenAPI access scopes changed"
                )


def _openapi_content_errors(old: object, candidate: object, path: str, errors: list[str]) -> None:
    if not isinstance(old, dict):
        return
    if not isinstance(candidate, dict):
        errors.append(f"{path}: OpenAPI content was removed")
        return
    for media_type in sorted(old.keys() - candidate.keys()):
        errors.append(f"{path}.{media_type}: OpenAPI media type was removed")
    for media_type in sorted(old.keys() & candidate.keys()):
        old_media = old[media_type]
        new_media = candidate[media_type]
        if not isinstance(old_media, dict) or not isinstance(new_media, dict):
            if old_media != new_media:
                errors.append(f"{path}.{media_type}: OpenAPI content changed")
            continue
        old_schema = old_media.get("schema", _MISSING)
        new_schema = new_media.get("schema", _MISSING)
        if old_schema is not _MISSING and new_schema is _MISSING:
            errors.append(f"{path}.{media_type}.schema: OpenAPI schema was removed")
        elif old_schema is not _MISSING:
            _schema_compatibility_errors(
                old_schema, new_schema, f"{path}.{media_type}.schema", errors
            )


def _openapi_request_body_errors(
    old: object, candidate: object, path: str, errors: list[str]
) -> None:
    if not isinstance(old, dict) or not isinstance(candidate, dict):
        if old != candidate:
            errors.append(f"{path}: OpenAPI request body was removed or changed")
        return
    if old.get("$ref", _MISSING) != candidate.get("$ref", _MISSING):
        errors.append(f"{path}.$ref: OpenAPI request body reference changed")
    if not old.get("required", False) and candidate.get("required", False):
        errors.append(f"{path}.required: OpenAPI request body became required")
    _openapi_content_errors(old.get("content"), candidate.get("content"), f"{path}.content", errors)


def _openapi_response_errors(old: object, candidate: object, path: str, errors: list[str]) -> None:
    if not isinstance(old, dict) or not isinstance(candidate, dict):
        if old != candidate:
            errors.append(f"{path}: OpenAPI response was removed or changed")
        return
    if old.get("$ref", _MISSING) != candidate.get("$ref", _MISSING):
        errors.append(f"{path}.$ref: OpenAPI response reference changed")
    _openapi_content_errors(old.get("content"), candidate.get("content"), f"{path}.content", errors)
    old_headers = old.get("headers")
    new_headers = candidate.get("headers")
    if isinstance(old_headers, dict):
        if not isinstance(new_headers, dict):
            errors.append(f"{path}.headers: OpenAPI response headers were removed")
        else:
            for name in sorted(old_headers.keys() - new_headers.keys()):
                errors.append(f"{path}.headers.{name}: OpenAPI response header was removed")


def _parameter_identity(parameter: object, index: int) -> tuple[str, str]:
    if isinstance(parameter, dict) and isinstance(parameter.get("$ref"), str):
        return ("$ref", parameter["$ref"])
    if isinstance(parameter, dict):
        return (str(parameter.get("in")), str(parameter.get("name")))
    return ("index", str(index))


def _openapi_parameter_errors(old: object, candidate: object, path: str, errors: list[str]) -> None:
    if not isinstance(old, list):
        return
    if not isinstance(candidate, list):
        errors.append(f"{path}: OpenAPI parameters were removed")
        return
    old_parameters = {_parameter_identity(item, index): item for index, item in enumerate(old)}
    new_parameters = {
        _parameter_identity(item, index): item for index, item in enumerate(candidate)
    }
    for identity in sorted(old_parameters.keys() - new_parameters.keys()):
        errors.append(f"{path}: OpenAPI parameter {identity!r} was removed")
    for identity in sorted(old_parameters.keys() & new_parameters.keys()):
        old_parameter = old_parameters[identity]
        new_parameter = new_parameters[identity]
        if not isinstance(old_parameter, dict) or not isinstance(new_parameter, dict):
            continue
        if not old_parameter.get("required", False) and new_parameter.get("required", False):
            errors.append(f"{path}.{identity!r}: OpenAPI parameter became required")
        if "schema" in old_parameter:
            if "schema" not in new_parameter:
                errors.append(f"{path}.{identity!r}.schema: OpenAPI schema was removed")
            else:
                _schema_compatibility_errors(
                    old_parameter["schema"],
                    new_parameter["schema"],
                    f"{path}.{identity!r}.schema",
                    errors,
                )
        _openapi_content_errors(
            old_parameter.get("content"),
            new_parameter.get("content"),
            f"{path}.{identity!r}.content",
            errors,
        )


def _openapi_operation_errors(
    old: dict[str, Any], candidate: dict[str, Any], path: str, errors: list[str]
) -> None:
    if old.get("operationId", _MISSING) != candidate.get("operationId", _MISSING):
        errors.append(f"{path}.operationId: OpenAPI operation identity changed")
    _openapi_parameter_errors(
        old.get("parameters"), candidate.get("parameters"), f"{path}.parameters", errors
    )
    if "requestBody" in old:
        if "requestBody" not in candidate:
            errors.append(f"{path}.requestBody: OpenAPI request body was removed")
        else:
            _openapi_request_body_errors(
                old["requestBody"], candidate["requestBody"], f"{path}.requestBody", errors
            )
    elif isinstance(candidate.get("requestBody"), dict) and candidate["requestBody"].get(
        "required", False
    ):
        errors.append(f"{path}.requestBody: new required OpenAPI request body narrows the method")
    old_responses = old.get("responses")
    new_responses = candidate.get("responses")
    if isinstance(old_responses, dict):
        if not isinstance(new_responses, dict):
            errors.append(f"{path}.responses: OpenAPI responses were removed")
        else:
            for status in sorted(old_responses.keys() - new_responses.keys()):
                errors.append(f"{path}.responses.{status}: OpenAPI response was removed")
            for status in sorted(old_responses.keys() & new_responses.keys()):
                _openapi_response_errors(
                    old_responses[status],
                    new_responses[status],
                    f"{path}.responses.{status}",
                    errors,
                )


def _openapi_compatibility_errors(
    old: object, candidate: object, path: str, errors: list[str]
) -> None:
    if not isinstance(old, dict) or not isinstance(candidate, dict):
        errors.append(f"{path}: OpenAPI document was removed or changed")
        return
    old_root_security = _effective_openapi_security(_ANONYMOUS_SECURITY, old)
    new_root_security = _effective_openapi_security(_ANONYMOUS_SECURITY, candidate)
    old_root_metadata = _effective_access_metadata({}, old)
    new_root_metadata = _effective_access_metadata({}, candidate)
    _openapi_access_errors(
        old_root_security,
        new_root_security,
        old_root_metadata,
        new_root_metadata,
        path,
        errors,
    )
    _openapi_extension_errors(
        old,
        candidate,
        path,
        errors,
        nested_exclusions=frozenset({"components", "paths"}),
    )
    old_paths = old.get("paths")
    new_paths = candidate.get("paths")
    if not isinstance(old_paths, dict) or not isinstance(new_paths, dict):
        errors.append(f"{path}.paths: OpenAPI routes were removed")
        return
    for route in sorted(old_paths.keys() - new_paths.keys()):
        errors.append(f"{path}.paths.{route}: OpenAPI route was removed")
    for route in sorted(old_paths.keys() & new_paths.keys()):
        old_path = old_paths[route]
        new_path = new_paths[route]
        if not isinstance(old_path, dict) or not isinstance(new_path, dict):
            errors.append(f"{path}.paths.{route}: OpenAPI route changed")
            continue
        _openapi_parameter_errors(
            old_path.get("parameters"),
            new_path.get("parameters"),
            f"{path}.paths.{route}.parameters",
            errors,
        )
        old_path_security = _effective_openapi_security(old_root_security, old_path)
        new_path_security = _effective_openapi_security(new_root_security, new_path)
        old_path_metadata = _effective_access_metadata(old_root_metadata, old_path)
        new_path_metadata = _effective_access_metadata(new_root_metadata, new_path)
        _openapi_access_errors(
            old_path_security,
            new_path_security,
            old_path_metadata,
            new_path_metadata,
            f"{path}.paths.{route}",
            errors,
        )
        _openapi_extension_errors(
            old_path,
            new_path,
            f"{path}.paths.{route}",
            errors,
            nested_exclusions=frozenset(_HTTP_METHODS),
        )
        old_methods = {key.lower() for key in old_path if key.lower() in _HTTP_METHODS}
        new_methods = {key.lower() for key in new_path if key.lower() in _HTTP_METHODS}
        for method in sorted(old_methods - new_methods):
            errors.append(f"{path}.paths.{route}.{method}: OpenAPI method was removed")
        for method in sorted(old_methods & new_methods):
            old_operation = old_path.get(method)
            new_operation = new_path.get(method)
            if not isinstance(old_operation, dict) or not isinstance(new_operation, dict):
                errors.append(f"{path}.paths.{route}.{method}: OpenAPI method changed")
                continue
            old_operation_security = _effective_openapi_security(old_path_security, old_operation)
            new_operation_security = _effective_openapi_security(new_path_security, new_operation)
            old_operation_metadata = _effective_access_metadata(old_path_metadata, old_operation)
            new_operation_metadata = _effective_access_metadata(new_path_metadata, new_operation)
            _openapi_access_errors(
                old_operation_security,
                new_operation_security,
                old_operation_metadata,
                new_operation_metadata,
                f"{path}.paths.{route}.{method}",
                errors,
            )
            _openapi_extension_errors(
                old_operation,
                new_operation,
                f"{path}.paths.{route}.{method}",
                errors,
                nested_exclusions=frozenset({"parameters", "requestBody", "responses"}),
            )
            _openapi_operation_errors(
                old_operation, new_operation, f"{path}.paths.{route}.{method}", errors
            )
        for extension in ("x-beadhive-websocket",):
            old_operation = old_path.get(extension)
            new_operation = new_path.get(extension)
            if not isinstance(old_operation, dict):
                continue
            if not isinstance(new_operation, dict):
                errors.append(
                    f"{path}.paths.{route}.{extension}: OpenAPI access operation was removed"
                )
                continue
            _openapi_access_errors(
                _effective_openapi_security(old_path_security, old_operation),
                _effective_openapi_security(new_path_security, new_operation),
                _effective_access_metadata(old_path_metadata, old_operation),
                _effective_access_metadata(new_path_metadata, new_operation),
                f"{path}.paths.{route}.{extension}",
                errors,
            )

    old_components = old.get("components", {})
    new_components = candidate.get("components", {})
    if not isinstance(old_components, dict) or not isinstance(new_components, dict):
        errors.append(f"{path}.components: OpenAPI components were removed")
        return
    _openapi_security_scheme_errors(
        old_components.get("securitySchemes", {}),
        new_components.get("securitySchemes", {}),
        f"{path}.components.securitySchemes",
        errors,
    )
    old_schemas = old_components.get("schemas", {})
    new_schemas = new_components.get("schemas", {})
    if isinstance(old_schemas, dict):
        if not isinstance(new_schemas, dict):
            errors.append(f"{path}.components.schemas: OpenAPI schemas were removed")
        else:
            for name in sorted(old_schemas.keys() - new_schemas.keys()):
                errors.append(f"{path}.components.schemas.{name}: OpenAPI schema was removed")
            for name in sorted(old_schemas.keys() & new_schemas.keys()):
                _schema_compatibility_errors(
                    old_schemas[name],
                    new_schemas[name],
                    f"{path}.components.schemas.{name}",
                    errors,
                )
    for section, comparator in (
        ("responses", _openapi_response_errors),
        ("requestBodies", _openapi_request_body_errors),
    ):
        old_rows = old_components.get(section, {})
        new_rows = new_components.get(section, {})
        if not isinstance(old_rows, dict):
            continue
        if not isinstance(new_rows, dict):
            errors.append(f"{path}.components.{section}: OpenAPI component section was removed")
            continue
        for name in sorted(old_rows.keys() - new_rows.keys()):
            errors.append(f"{path}.components.{section}.{name}: OpenAPI component was removed")
        for name in sorted(old_rows.keys() & new_rows.keys()):
            comparator(
                old_rows[name], new_rows[name], f"{path}.components.{section}.{name}", errors
            )


def compatibility_errors(old: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Apply each published v1 policy against an explicit prior release."""

    errors: list[str] = []
    old_artifacts = old.get("artifacts", [])
    new_artifacts = candidate.get("artifacts", [])
    if not isinstance(old_artifacts, list) or not isinstance(new_artifacts, list):
        return ["release artifacts must remain arrays"]
    old_rows = {
        row["id"]: row
        for row in old_artifacts
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    new_rows = {
        row["id"]: row
        for row in new_artifacts
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    if len(old_rows) != len(old_artifacts):
        errors.append("published baseline contains missing or duplicate artifact identities")
    if len(new_rows) != len(new_artifacts):
        errors.append("candidate contains missing or duplicate artifact identities")
    old_families = {row.get("family") for row in old_rows.values()}
    new_families = {row.get("family") for row in new_rows.values()}
    for family in sorted(str(item) for item in old_families - new_families):
        errors.append(f"family {family!r}: official contract family was removed or renamed")
    for artifact_id in sorted(old_rows.keys() - new_rows.keys()):
        errors.append(f"{artifact_id}: artifact was removed")
    for artifact_id in sorted(old_rows.keys() & new_rows.keys()):
        old_row = old_rows[artifact_id]
        new_row = new_rows[artifact_id]
        for field in ("family", "version", "kind", "path", "compatibility_policy"):
            if new_row.get(field) != old_row.get(field):
                errors.append(f"{artifact_id}.{field}: stable artifact metadata changed")
        old_document = old_row.get("document")
        new_document = new_row.get("document")
        if isinstance(old_document, dict):
            for identity_key in ("$id", "artifact_id"):
                if identity_key in old_document and (
                    not isinstance(new_document, dict)
                    or new_document.get(identity_key) != old_document[identity_key]
                ):
                    errors.append(f"{artifact_id}: stable artifact identity changed")
        policy = old_row.get("compatibility_policy")
        if policy == _SCHEMA_POLICY:
            _schema_compatibility_errors(old_document, new_document, artifact_id, errors)
        elif policy == _OPENAPI_POLICY:
            _openapi_compatibility_errors(old_document, new_document, artifact_id, errors)
        elif policy == _CATALOG_POLICY:
            _append_only_errors(old_document, new_document, artifact_id, errors)
        else:
            errors.append(f"{artifact_id}: unsupported compatibility policy {policy!r}")
        if old_row["family"] == "operation-catalog":
            old_names = {
                row["name"]
                for row in old_document.get("operations", [])
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            }
            new_names = {
                row["name"]
                for row in new_document.get("operations", [])
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            }
            for name in sorted(old_names - new_names):
                errors.append(f"{artifact_id}: operation identity {name!r} was removed or renamed")
        if old_row["family"] in {"cli", "mcp"}:
            old_projections = {
                (row["surface"], row["identifier"]): row
                for row in old_document.get("projections", [])
                if isinstance(row, dict)
            }
            new_projections = {
                (row["surface"], row["identifier"]): row
                for row in new_document.get("projections", [])
                if isinstance(row, dict)
            }
            for identity, old_projection in old_projections.items():
                new_projection = new_projections.get(identity)
                if new_projection is None:
                    errors.append(f"{artifact_id}: projection identity {identity!r} was removed")
                elif new_projection.get("privilege") != old_projection.get("privilege"):
                    errors.append(f"{artifact_id}: projection privilege changed for {identity!r}")
    return errors


def _published_compatibility_errors(candidate: dict[str, Any]) -> list[str]:
    return compatibility_errors(load_published_baseline(), candidate)


def main(argv: list[str] | None = None) -> int:
    """Check or regenerate the package-owned release without starting runtime services."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if generated files drifted")
    mode.add_argument("--write", action="store_true", help="rewrite generated release files")
    args = parser.parse_args(argv)
    if args.check:
        errors = validate_release()
        if not errors:
            print(f"official contract release v{RELEASE_VERSION}: current")
            return 0
        for error in errors:
            print(f"official contract release: {error}")
        return 1
    try:
        write_release()
    except ValueError as exc:
        print(f"official contract release: {exc}")
        return 1
    print(f"official contract release v{RELEASE_VERSION}: generated")
    return 0


__all__ = (
    "OFFICIAL_V1_FAMILIES",
    "RELEASE_VERSION",
    "build_release",
    "compatibility_errors",
    "load_artifact",
    "load_published_baseline",
    "load_release",
    "main",
    "published_baseline_root",
    "release_root",
    "render_release",
    "validate_release",
    "write_release",
)
