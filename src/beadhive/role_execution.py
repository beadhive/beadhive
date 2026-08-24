"""Fail-closed planning for explicit ``bh role`` headless launches.

An artifact name is not provider evidence.  Explicit Claude Code/Codex requests use only a
provider-qualified BAML artifact and its adjacent, BAML-owned manifest.  This module validates
that immutable pair without launching anything or touching beads; :mod:`beadhive.cli` performs
the lifecycle and process work only after a plan is accepted.

The unqualified ``bh-<seat>`` binary remains a provider-unspecified compatibility path.  It is
deliberately outside this resolver and can never satisfy ``baml_required``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
BAML_DRIVER = "baml"
BAML_MANIFEST_DRIVER = "baml-harness"
PROVIDER_FOR_HARNESS = {"claude": "claude-code", "codex": "codex"}
SUPPORTED_LIVE_MECHANISMS = {
    "claude-stream-json",
    "codex-jsonl",
    "provider-json",
    "provider-jsonl",
}


class RoleLaunchRefused(ValueError):
    """A launch request failed a pre-claim contract gate."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class QualifiedArtifact:
    """One validated provider-qualified packed seat and its immutable evidence."""

    binary: Path
    manifest_path: Path
    artifact_digest: str
    manifest_digest: str
    seat: str
    provider: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class RoleLaunchPlan:
    """The resolved driver/provider decision consumed by the role launcher."""

    backend: str
    detail: str
    provider: str | None
    artifact: QualifiedArtifact | None = None

    @property
    def driver(self) -> str:
        return BAML_DRIVER if self.backend == "baml" else self.backend


def qualified_artifact_name(seat: str, provider: str) -> str:
    return f"bh-{seat}-{provider}"


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise RoleLaunchRefused(
            "artifact_unreadable", f"cannot read qualified artifact {path.name!r}"
        ) from exc
    return f"sha256:{hasher.hexdigest()}"


def _require_string(document: dict[str, Any], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise RoleLaunchRefused("manifest_invalid", f"manifest field {field!r} is required")
    return value


def _require_digest(document: dict[str, Any], field: str) -> str:
    value = _require_string(document, field)
    if len(value) != 71 or not value.startswith("sha256:"):
        raise RoleLaunchRefused("manifest_invalid", f"manifest field {field!r} is not sha256")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise RoleLaunchRefused(
            "manifest_invalid", f"manifest field {field!r} is not sha256"
        ) from exc
    if value[7:] != value[7:].lower():
        raise RoleLaunchRefused("manifest_invalid", f"manifest field {field!r} is not sha256")
    return value


def _validate_provenance(document: dict[str, Any]) -> None:
    _require_string(document, "profile")
    _require_digest(document, "profile_digest")
    _require_digest(document, "packs_digest")
    version = document.get("contract_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RoleLaunchRefused(
            "manifest_invalid", "manifest contract_version must be a positive integer"
        )
    packs = document.get("packs")
    if not isinstance(packs, list):
        raise RoleLaunchRefused("manifest_invalid", "manifest packs must be a list")
    for pack in packs:
        if not isinstance(pack, dict):
            raise RoleLaunchRefused("manifest_invalid", "manifest pack entry must be an object")
        _require_string(pack, "name")
        _require_string(pack, "version")
        _require_digest(pack, "digest")


def _validate_authority(document: dict[str, Any], provider: str, seat: str) -> None:
    authority = document.get("authority")
    if not isinstance(authority, dict):
        raise RoleLaunchRefused(
            "authority_unsupported", "manifest must disclose provider-native authority"
        )
    if authority.get("provider") != provider:
        raise RoleLaunchRefused(
            "authority_unsupported", "manifest authority provider disagrees with the request"
        )
    if authority.get("inherit_user_config") is not False:
        raise RoleLaunchRefused(
            "authority_unsupported", "ambient user configuration must be explicitly disabled"
        )
    mcp = authority.get("mcp")
    if not isinstance(mcp, dict) or not isinstance(mcp.get("enabled"), bool):
        raise RoleLaunchRefused(
            "authority_unsupported", "manifest must disclose effective MCP authority"
        )
    if not isinstance(mcp.get("servers"), list) or not all(
        isinstance(server, str) and server for server in mcp["servers"]
    ):
        raise RoleLaunchRefused(
            "authority_unsupported", "manifest MCP server names must be an explicit list"
        )

    if provider == "claude-code":
        mode = authority.get("permission_mode")
        rules = authority.get("permissions")
        if not isinstance(mode, str) or not mode or not isinstance(rules, dict):
            raise RoleLaunchRefused(
                "authority_unsupported", "Claude artifact lacks its effective permission posture"
            )
        for bucket in ("allow", "ask", "deny"):
            if not isinstance(rules.get(bucket), list) or not all(
                isinstance(rule, str) and rule for rule in rules[bucket]
            ):
                raise RoleLaunchRefused(
                    "authority_unsupported", f"Claude permission bucket {bucket!r} is missing"
                )
    elif provider == "codex":
        if authority.get("sandbox") != "read-only" or authority.get("approval") != "untrusted":
            raise RoleLaunchRefused(
                "authority_unsupported",
                "Codex artifacts must disclose the conservative read-only/untrusted posture",
            )
    else:  # defensive: callers should have rejected this before reading an artifact
        raise RoleLaunchRefused("provider_unsupported", f"unsupported BAML provider {provider!r}")

    capabilities = document.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("name") != seat:
        raise RoleLaunchRefused(
            "authority_unsupported", "manifest capabilities do not describe the requested seat"
        )
    baked = capabilities.get("baked")
    if (
        not isinstance(baked, dict)
        or baked.get("baked") is not True
        or baked.get("provider") != provider
    ):
        raise RoleLaunchRefused(
            "authority_unsupported", "manifest capabilities do not carry the requested bake"
        )
    if capabilities.get("inherit_user") is not False:
        raise RoleLaunchRefused(
            "authority_unsupported", "capability manifest must disable ambient user authority"
        )
    if capabilities.get("has_mcp") is not mcp["enabled"]:
        raise RoleLaunchRefused(
            "authority_unsupported", "MCP posture disagrees with the capability manifest"
        )
    if capabilities.get("mcp_servers") != mcp["servers"]:
        raise RoleLaunchRefused(
            "authority_unsupported", "MCP server roster disagrees with the capability manifest"
        )
    if provider == "claude-code" and (
        capabilities.get("permission_mode") != authority["permission_mode"]
        or capabilities.get("permissions") != authority["permissions"]
    ):
        raise RoleLaunchRefused(
            "authority_unsupported", "Claude authority disagrees with the capability manifest"
        )
    if baked.get("profile") != document.get("profile") or baked.get(
        "contract_version"
    ) != document.get("contract_version"):
        raise RoleLaunchRefused(
            "manifest_invalid", "baked profile provenance disagrees with the artifact manifest"
        )
    if capabilities.get("packs") != document.get("packs"):
        raise RoleLaunchRefused(
            "manifest_invalid", "pack provenance disagrees with the capability manifest"
        )


def _validate_transport(document: dict[str, Any]) -> None:
    framing = document.get("framing")
    if (
        not isinstance(framing, dict)
        or framing.get("input") != "stream-json"
        or framing.get("output") != "seat-run-jsonl"
    ):
        raise RoleLaunchRefused(
            "framing_unsupported",
            "manifest framing must be stream-json input and seat-run-jsonl output",
        )
    mechanism = document.get("live_event_mechanism")
    if mechanism not in SUPPORTED_LIVE_MECHANISMS:
        raise RoleLaunchRefused(
            "live_mechanism_unsupported",
            f"unsupported live-event mechanism {mechanism!r}",
        )


def validate_qualified_artifact(
    binary: Path, manifest_path: Path, *, seat: str, provider: str
) -> QualifiedArtifact:
    """Validate the exact binary/adjacent-manifest pair without executing it."""

    expected = qualified_artifact_name(seat, provider)
    if binary.name != expected or manifest_path != binary.with_name(f"{expected}.manifest.json"):
        raise RoleLaunchRefused(
            "artifact_mismatch", f"provider request requires exact artifact {expected!r}"
        )
    try:
        raw_manifest = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise RoleLaunchRefused(
            "manifest_missing", f"qualified artifact {expected!r} has no adjacent manifest"
        ) from exc
    except OSError as exc:
        raise RoleLaunchRefused(
            "manifest_unreadable", f"cannot read manifest for qualified artifact {expected!r}"
        ) from exc
    try:
        document = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RoleLaunchRefused(
            "manifest_invalid", f"qualified artifact {expected!r} has an invalid manifest"
        ) from exc
    if not isinstance(document, dict):
        raise RoleLaunchRefused("manifest_invalid", "provider artifact manifest must be an object")
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != MANIFEST_VERSION:
        raise RoleLaunchRefused(
            "manifest_version_unsupported",
            f"unsupported provider artifact manifest version {version!r}",
        )
    if document.get("artifact") != expected or document.get("seat") != seat:
        raise RoleLaunchRefused("artifact_mismatch", "manifest names a different artifact or seat")
    if document.get("provider") != provider:
        raise RoleLaunchRefused("provider_mismatch", "manifest provider disagrees with the request")
    if document.get("driver") != BAML_MANIFEST_DRIVER:
        raise RoleLaunchRefused(
            "driver_mismatch", "qualified artifact manifest driver must be 'baml-harness'"
        )
    if document.get("baked") is not True:
        raise RoleLaunchRefused(
            "artifact_unbaked", "provider-qualified artifact must carry baked authority"
        )
    if document.get("runnable") is not True:
        raise RoleLaunchRefused(
            "provider_unavailable", f"BAML provider {provider!r} is not runnable in this artifact"
        )

    artifact_digest = _sha256_file(binary)
    if _require_digest(document, "artifact_digest") != artifact_digest:
        raise RoleLaunchRefused("digest_mismatch", f"artifact digest mismatch for {expected!r}")
    _validate_provenance(document)
    _validate_authority(document, provider, seat)
    _validate_transport(document)
    return QualifiedArtifact(
        # Preserve the qualified basename when an installation uses a symlink; the packed seat's
        # own pre-spawn guard receives this exact path and independently checks the same name.
        binary=binary.absolute(),
        manifest_path=manifest_path.absolute(),
        artifact_digest=artifact_digest,
        manifest_digest=_sha256_bytes(raw_manifest),
        seat=seat,
        provider=provider,
        manifest=document,
    )


def resolve_qualified_artifact(seat: str, provider: str) -> QualifiedArtifact | None:
    """Resolve only the exact provider-qualified basename; never consult an alias."""

    name = qualified_artifact_name(seat, provider)
    found = shutil.which(name)
    if found is None:
        return None
    binary = Path(found)
    return validate_qualified_artifact(
        binary, binary.with_name(f"{name}.manifest.json"), seat=seat, provider=provider
    )


def resolve_headless_plan(
    seat: str,
    harness: str,
    cfg,
    *,
    explicit_harness: bool,
    baml_required: bool,
    no_hitch: bool,
) -> RoleLaunchPlan:
    """Resolve a headless launch before any worktree claim or process creation.

    Provider-specified calls bypass the unqualified BAML alias.  A valid exact artifact wins;
    when BAML is optional and no exact artifact exists, direct Hitch may satisfy the request.
    An artifact that exists but fails validation is never bypassed by a fallback.
    """

    from . import hitch_plugin

    if unsuitable := hitch_plugin.headless_unsuitable(seat):
        raise RoleLaunchRefused("seat_unsuitable", unsuitable)
    exact_required = explicit_harness or baml_required
    if exact_required:
        provider = PROVIDER_FOR_HARNESS.get(harness)
        if provider is None:
            if baml_required:
                raise RoleLaunchRefused(
                    "provider_unsupported",
                    f"BAML is required but harness {harness!r} has no BAML provider contract",
                )
        else:
            artifact = resolve_qualified_artifact(seat, provider)
            if artifact is not None:
                return RoleLaunchPlan(
                    backend="baml",
                    provider=provider,
                    artifact=artifact,
                    detail=f"validated {artifact.binary.name} and adjacent manifest",
                )
            if baml_required:
                raise RoleLaunchRefused(
                    "artifact_missing",
                    "BAML is required but exact artifact "
                    f"{qualified_artifact_name(seat, provider)!r} is unavailable",
                )
        if no_hitch:
            raise RoleLaunchRefused(
                "backend_unavailable",
                "the exact BAML artifact is unavailable and direct Hitch was disabled",
            )
        backend, detail = hitch_plugin.headless_hitch_plan(seat, harness, cfg)
        if backend == "hitch":
            return RoleLaunchPlan(backend="hitch", provider=provider, detail=detail)
        missing = (
            qualified_artifact_name(seat, provider)
            if provider is not None
            else f"BAML provider for {harness}"
        )
        raise RoleLaunchRefused(
            "backend_unavailable", f"no exact {missing!r} artifact and {detail}"
        )

    backend, detail = hitch_plugin.headless_plan(seat, harness, cfg)
    if backend is None:
        raise RoleLaunchRefused("backend_unavailable", detail)
    if backend == "hitch" and no_hitch:
        raise RoleLaunchRefused(
            "backend_unavailable", f"--no-hitch, and the only headless backend here is {detail}"
        )
    # A provider-unspecified BAML alias deliberately contributes no provider fact.  Direct Hitch
    # does know its target from the explicit/configured harness, but its manifest/context remains
    # agent-hitch-owned and is outside this BAML artifact resolver.
    provider = PROVIDER_FOR_HARNESS.get(harness) if backend == "hitch" else None
    return RoleLaunchPlan(backend=backend, provider=provider, detail=detail)


def create_role_journal(artifact: QualifiedArtifact, *, hive: str, bead: str):
    """Create a role-owned journal using the shared run-journal implementation.

    Imported lazily so this bead can remain isolated from the parallel LocalLoop delivery; the
    epic assembly supplies the shared module before the completed feature is runnable.
    """

    from . import run_journal

    identity = run_journal.RunIdentity(
        hive=hive,
        bead=bead or None,
        driver=BAML_DRIVER,
        provider=artifact.provider,
        manifest_digest=artifact.manifest_digest,
    )
    return run_journal.RunJournal.create(identity, writer="beadhive.role")
