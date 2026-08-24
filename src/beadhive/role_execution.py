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
import shlex
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
BAML_DRIVER = "baml"
BAML_MANIFEST_DRIVER = "baml-harness"
EXPLAIN_SCHEMA_VERSION = 1
EXPLAIN_COMMAND = "role explain"
REDACTED_INSTRUCTIONS = "<redacted:instructions>"
REDACTED_RUN_CONTEXT = "<redacted:run-context>"
PROVIDER_FOR_HARNESS = {"claude": "claude-code", "codex": "codex"}
HITCH_TARGET_FOR_HARNESS = {"claude": "claude-code", "codex": "codex", "opencode": "opencode"}
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
    hitch_target: str | None = None
    hitch_profile: str | None = None

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
            return RoleLaunchPlan(
                backend="hitch",
                provider=provider,
                detail=detail,
                hitch_target=HITCH_TARGET_FOR_HARNESS.get(harness),
                hitch_profile=seat,
            )
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
    return RoleLaunchPlan(
        backend=backend,
        provider=provider,
        detail=detail,
        hitch_target=HITCH_TARGET_FOR_HARNESS.get(harness) if backend == "hitch" else None,
        hitch_profile=seat if backend == "hitch" else None,
    )


def _safe_authority(artifact: QualifiedArtifact | None) -> dict[str, Any] | None:
    """Return only launch-authority facts, never credentials or raw rule values."""

    if artifact is None:
        return None
    authority = artifact.manifest["authority"]
    capabilities = artifact.manifest["capabilities"]
    permissions = authority.get("permissions") or {}
    references = authority.get("config_references")
    return {
        "provider": authority["provider"],
        "permission_mode": authority.get("permission_mode"),
        "permission_rule_counts": {
            name: len(permissions.get(name) or []) for name in ("allow", "ask", "deny")
        }
        if permissions
        else None,
        "sandbox": authority.get("sandbox"),
        "approval": authority.get("approval"),
        "mcp": {
            "enabled": authority["mcp"]["enabled"],
            "servers": list(authority["mcp"]["servers"]),
        },
        "config_references": {
            "declared": "config_references" in authority,
            "count": len(references) if isinstance(references, list) else 0,
        },
        "ambient_inheritance": {
            "user_config": authority["inherit_user_config"],
            "capability_user": capabilities["inherit_user"],
        },
    }


def _artifact_report(
    artifact: QualifiedArtifact | None, *, seat: str, provider: str | None
) -> dict[str, Any]:
    """Redacted evidence for a validated artifact or the exact expected basename."""

    requested = qualified_artifact_name(seat, provider) if provider else None
    if artifact is None:
        return {
            "requested_name": requested,
            "validated": False,
            "binary_path": None,
            "manifest_path": None,
            "artifact_digest": None,
            "manifest_digest": None,
            "manifest_version": None,
            "baked": None,
            "seat": None,
            "provider": None,
            "profile": None,
            "profile_digest": None,
            "packs_digest": None,
            "compatibility_contract": None,
        }
    manifest = artifact.manifest
    return {
        "requested_name": requested,
        "validated": True,
        "binary_path": str(artifact.binary),
        "manifest_path": str(artifact.manifest_path),
        "artifact_digest": artifact.artifact_digest,
        "manifest_digest": artifact.manifest_digest,
        "manifest_version": manifest["version"],
        "baked": manifest["baked"],
        "seat": manifest["seat"],
        "provider": manifest["provider"],
        "profile": manifest["profile"],
        "profile_digest": manifest["profile_digest"],
        "packs_digest": manifest["packs_digest"],
        "compatibility_contract": {
            "version": manifest["contract_version"],
            "framing": dict(manifest["framing"]),
        },
    }


def _candidate_artifact_report(seat: str, provider: str | None) -> dict[str, Any]:
    """Best-effort, hash-only evidence when validation refused a present candidate.

    Failed validation means no manifest content is trusted enough to print: even nominal identity
    fields such as profile, seat, and provider can contain credentials. Only request-derived paths
    and content hashes survive this boundary.
    """

    report = _artifact_report(None, seat=seat, provider=provider)
    if provider is None:
        return report
    name = qualified_artifact_name(seat, provider)
    found = shutil.which(name)
    if found is None:
        return report
    binary = Path(found).absolute()
    manifest_path = binary.with_name(f"{name}.manifest.json")
    report["binary_path"] = str(binary)
    report["manifest_path"] = str(manifest_path)
    try:
        report["artifact_digest"] = _sha256_file(binary)
    except RoleLaunchRefused:
        pass
    try:
        raw = manifest_path.read_bytes()
    except OSError:
        return report
    report["manifest_digest"] = _sha256_bytes(raw)
    return report


def _redacted_argv(
    plan: RoleLaunchPlan | None,
    *,
    seat: str,
    workspace: str,
    bead: str,
    detached: bool,
    outer_run_id: str | None,
    provider_session_id: str | None,
    cfg,
    entry,
) -> list[str]:
    if plan is None:
        return []
    if plan.backend == "hitch":
        from . import config, hitch_plugin

        repo = config.hitch_repo(cfg)
        if repo is None or plan.hitch_target is None or plan.hitch_profile is None:
            return []
        return hitch_plugin._hitch_argv(
            cfg,
            plan.hitch_target,
            plan.hitch_profile,
            command=config.hitch_command(cfg),
            repo=repo,
            workspace=workspace,
            task=REDACTED_INSTRUCTIONS,
            detached=detached,
            role_=seat,
        )

    from . import config, localloop

    qualified = plan.artifact
    command = str(qualified.binary) if qualified else config.dispatch_seat_command(cfg, entry)
    bundle = "" if qualified else config.dispatch_seat_bundle(cfg, entry)
    argv = list(
        localloop.seat_argv(
            command,
            seat,
            workspace=workspace,
            bead=bead,
            instructions=REDACTED_INSTRUCTIONS,
            session_id=provider_session_id or "<proposed-provider-session>",
            bundle=bundle,
        )
    )
    if qualified is None:
        configured_head = shlex.split(command.format(role=seat))
        if configured_head:
            argv[: len(configured_head)] = [
                configured_head[0],
                *("<redacted:configured-argument>" for _value in configured_head[1:]),
            ]
    if qualified is not None:
        argv += [
            "--outer_attempt_id",
            outer_run_id or "<proposed-outer-run>",
            "--journal_context",
            REDACTED_RUN_CONTEXT,
            "--artifact_path",
            str(qualified.binary),
            "--artifact_manifest",
            str(qualified.manifest_path),
        ]
    return argv


def explain_report(
    *,
    seat: str,
    harness: str,
    cfg,
    entry,
    hive: str | None,
    workspace: str,
    bead: str,
    detached: bool,
    task_provided: bool,
    explicit_harness: bool,
    baml_required: bool,
    no_hitch: bool,
) -> dict[str, Any]:
    """Build the complete side-effect-free, redacted role execution plan."""

    from . import config as config_mod
    from . import run_journal, source_descriptors

    provider = PROVIDER_FOR_HARNESS.get(harness)
    plan: RoleLaunchPlan | None = None
    refusal: RoleLaunchRefused | None = None
    try:
        plan = resolve_headless_plan(
            seat,
            harness,
            cfg,
            explicit_harness=explicit_harness,
            baml_required=baml_required,
            no_hitch=no_hitch,
        )
    except RoleLaunchRefused as exc:
        refusal = exc

    if plan is not None and plan.backend == "hitch":
        hitch_command = config_mod.hitch_command(cfg)
        if shutil.which(hitch_command) is None:
            refusal = RoleLaunchRefused(
                "hitch_unavailable", f"direct Hitch executable {hitch_command!r} is unavailable"
            )
            plan = None

    runnable = plan is not None
    outer_run_id = f"run-{uuid.uuid4()}" if runnable else None
    provider_session_id = f"provider-{uuid.uuid4()}" if runnable else None
    artifact = plan.artifact if plan else None
    artifact_report = (
        _artifact_report(artifact, seat=seat, provider=provider)
        if artifact is not None
        else _candidate_artifact_report(seat, provider)
    )
    backend = plan.backend if plan else None
    driver = "baml-harness" if backend == "baml" else "hitch-direct" if backend == "hitch" else None
    live_source = artifact.manifest.get("live_event_mechanism") if artifact else None
    live_mechanism = "provider-json" if live_source in SUPPORTED_LIVE_MECHANISMS else "none"
    execution_provider = plan.provider if plan else provider
    suitability_mode = (
        "attached-required"
        if refusal is not None and refusal.code == "seat_unsuitable"
        else "headless-safe"
    )
    summary = (
        f"mode={suitability_mode} "
        f"backend={backend or 'none'} — {plan.detail if plan else refusal.detail}"
    )
    return {
        "schema_version": EXPLAIN_SCHEMA_VERSION,
        "command": EXPLAIN_COMMAND,
        "decision": "runnable" if runnable else "refused",
        "refusal_reasons": []
        if refusal is None
        else [{"code": refusal.code, "detail": refusal.detail}],
        "summary": summary,
        "request": {
            "hive": hive,
            "bead": bead or None,
            "seat": seat,
            "provider": provider,
            "harness": harness,
            "tier": None,
            "model": None,
            "workspace": workspace,
            "bamlRequired": baml_required,
            "task": {
                "provided": task_provided,
                "content": REDACTED_INSTRUCTIONS if task_provided else None,
            },
        },
        "execution": {
            "driver": driver,
            "provider": execution_provider,
            "entry_point": str(artifact.binary)
            if artifact
            else config_entry_point(plan, cfg=cfg, entry=entry, seat=seat),
            "mode": "detached" if detached else "attached",
            "baml_driven": backend == "baml",
            "provider_qualified": artifact is not None,
            "detail": plan.detail if plan else None,
        },
        "artifact": artifact_report,
        "authority": _safe_authority(artifact),
        "correlation": {
            "proposed_outer_run_id": outer_run_id,
            "proposed_provider_session_id": provider_session_id,
            "source_instance_id": f"beadhive.role:{outer_run_id}" if outer_run_id else None,
            "bead_id": bead or None,
            "assignment": {
                "work_item_id": bead,
                "agent_ref": {"seat": seat, "run_id": outer_run_id},
            }
            if bead and outer_run_id
            else None,
            "state_channel": {
                "source": "bh stream --scope hive",
                "authority": "bead-lifecycle",
            },
            "runtime_summary_source": "AgentRunSummary",
            "operator_event_subscription": "OperatorEvent HTTP/SSE",
            "activity_channel": {
                "version": run_journal.VERSION,
                "locator_environment": "BH_RUN_JOURNAL_PATH",
            },
        },
        "observability": {
            "pre_exit_possible": bool(artifact and live_mechanism != "none"),
            "mechanism": live_mechanism,
            "source_mechanism": live_source,
            "meets_live_evidence_bar": bool(artifact and live_mechanism != "none"),
        },
        "sources": source_descriptors.role_explain_sources(entry, hive),
        "argv": {
            "values": _redacted_argv(
                plan,
                seat=seat,
                workspace=workspace,
                bead=bead,
                detached=detached,
                outer_run_id=outer_run_id,
                provider_session_id=provider_session_id,
                cfg=cfg,
                entry=entry,
            ),
            "environment_names": list(run_journal.ENV_FIELDS),
        },
    }


def config_entry_point(plan: RoleLaunchPlan | None, *, cfg, entry, seat: str) -> str | None:
    """The configured non-qualified entry point, without starting or probing it."""

    if plan is None:
        return None
    from . import config

    if plan.backend == "hitch":
        return config.hitch_command(cfg)
    configured = shlex.split(config.dispatch_seat_command(cfg, entry).format(role=seat))
    return configured[0] if configured else None


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
