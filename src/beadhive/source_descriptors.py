"""Exact, versioned named-hive stream and host-local runtime source descriptors.

This is a discovery boundary, not another reader implementation.  Registry identity is resolved
once, the landed public readers supply runtime observations, and consumers receive exact launch
arguments and opaque cursor contracts without learning registry/path internals.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import config, dispatch_log, host, public_readers, registry, run_journal, state_stream

SCHEMA_VERSION = 1
CONTRACT_VERSION = "beadhive.named-hive-sources/v1"
STREAM_CONTRACT_VERSION = "beadhive.stream/v1"
SUMMARY_CONTRACT_VERSION = "beadhive.agent-run-summary/v1"


class ResolutionDecision(StrEnum):
    AVAILABLE = "available"
    REFUSED = "refused"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNKNOWN = "unknown"


class ResolutionReason(StrEnum):
    HIVE_MISSING = "hive_missing"
    HIVE_AMBIGUOUS = "hive_ambiguous"
    HIVE_NOT_INSTALLED_ON_HOST = "hive_not_installed_on_host"
    HOST_IDENTITY_UNAVAILABLE = "host_identity_unavailable"
    WRONG_HOST = "wrong_host"
    CLI_UNINSTALLED = "cli_uninstalled"
    CLI_VERSION_UNKNOWN = "cli_version_unknown"
    WRITER_COLOCATION_UNVERIFIED = "writer_colocation_unverified"
    RUN_ID_REQUIRED = "run_id_required"


@dataclass(frozen=True)
class InstalledFeature:
    name: str
    contract_version: str


@dataclass(frozen=True)
class InstalledCli:
    executable: str
    version: str
    features: tuple[InstalledFeature, ...]


@dataclass(frozen=True)
class HiveIdentity:
    registered_identity: str
    provider: str
    org: str
    repo: str
    repo_slug: str


@dataclass(frozen=True)
class HiveCorrelation:
    registered_identity: str
    stream_repo_slug: str
    rule: str = "exact_registry_identity_to_repo_slug"


@dataclass(frozen=True)
class CursorContract:
    field: str
    opaque: bool = True
    resume_argument: str | None = None


@dataclass(frozen=True)
class StreamLaunchDescriptor:
    source_instance_id: str
    availability: Availability
    availability_reason: str | None
    contract_version: str
    schema_version: int
    scope: str
    format: str
    hive_argument: str
    emitted_hive: str
    argv: tuple[str, ...]
    cursor: CursorContract


@dataclass(frozen=True)
class RuntimeObservation:
    revision: str | None
    coverage: str
    coverage_reason: str | None
    freshness: str
    freshness_as_of: float | None
    freshness_detail: str | None
    since_revision: str | None = None
    resync_reason: str | None = None


@dataclass(frozen=True)
class SummarySourceDescriptor:
    source_instance_id: str
    availability: Availability
    availability_reason: ResolutionReason
    contract_version: str
    host_id: str
    scope: str
    locator: str
    cursor: CursorContract
    observation: RuntimeObservation


@dataclass(frozen=True)
class JournalSourceDescriptor:
    source_instance_id: str
    availability: Availability
    availability_reason: ResolutionReason
    contract_version: str
    host_id: str
    scope: str
    run_id: str | None
    locator_root: str
    locator: str | None
    cursor: CursorContract
    observation: RuntimeObservation


@dataclass(frozen=True)
class RuntimeSourcesDescriptor:
    host_id: str
    availability: Availability
    availability_reason: ResolutionReason
    summary: SummarySourceDescriptor
    journal: JournalSourceDescriptor


@dataclass(frozen=True)
class NamedHiveSources:
    schema_version: int
    contract_version: str
    identity: HiveIdentity
    correlation: HiveCorrelation
    cli: InstalledCli
    stream: StreamLaunchDescriptor
    runtime: RuntimeSourcesDescriptor


@dataclass(frozen=True)
class DescriptorResolution:
    schema_version: int
    contract_version: str
    decision: ResolutionDecision
    reasons: tuple[ResolutionReason, ...]
    candidates: tuple[str, ...]
    descriptor: NamedHiveSources | None

    def payload(self) -> dict[str, Any]:
        return _json_value(self)


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _refused(reason: ResolutionReason, *, candidates: tuple[str, ...] = ()) -> DescriptorResolution:
    return DescriptorResolution(
        schema_version=SCHEMA_VERSION,
        contract_version=CONTRACT_VERSION,
        decision=ResolutionDecision.REFUSED,
        reasons=(reason,),
        candidates=candidates,
        descriptor=None,
    )


def _installed_version() -> str:
    return importlib.metadata.version("beadhive")


def _runtime_observation(snapshot: public_readers.AgentRunSnapshot) -> RuntimeObservation:
    return RuntimeObservation(
        revision=snapshot.revision,
        coverage=snapshot.coverage.value,
        coverage_reason=snapshot.coverage_reason,
        freshness=snapshot.freshness.state,
        freshness_as_of=snapshot.freshness.as_of,
        freshness_detail=snapshot.freshness.detail,
    )


def _journal_observation(
    frame: public_readers.RunJournalFrame | None,
) -> RuntimeObservation:
    if frame is None:
        return RuntimeObservation(
            revision=None,
            coverage=public_readers.Coverage.UNKNOWN.value,
            coverage_reason=ResolutionReason.RUN_ID_REQUIRED.value,
            freshness="unknown",
            freshness_as_of=None,
            freshness_detail="run_id required for a run-scoped journal observation",
        )
    return RuntimeObservation(
        revision=frame.source_revision,
        coverage=frame.coverage.value,
        coverage_reason=frame.coverage_reason,
        freshness=frame.freshness.state,
        freshness_as_of=frame.freshness.as_of,
        freshness_detail=frame.freshness.detail,
        since_revision=frame.since_revision,
        resync_reason=frame.resync_reason.value if frame.resync_reason is not None else None,
    )


def _identity(entry: Mapping[str, object]) -> HiveIdentity:
    correlation = public_readers.HiveCorrelation.from_registry_entry(entry)
    return HiveIdentity(
        registered_identity=correlation.registered_identity,
        provider=str(entry["provider"]),
        org=str(entry["org"]),
        repo=str(entry["repo"]),
        repo_slug=correlation.repo_slug,
    )


def _features() -> tuple[InstalledFeature, ...]:
    return (
        InstalledFeature("state_stream", STREAM_CONTRACT_VERSION),
        InstalledFeature("agent_run_summary", SUMMARY_CONTRACT_VERSION),
        InstalledFeature("run_journal", run_journal.VERSION),
        InstalledFeature("named_hive_sources", CONTRACT_VERSION),
    )


def _build_descriptor(
    entry: Mapping[str, object],
    *,
    host_id: str,
    executable: str,
    version: str,
    run_id: str | None,
    stream_since: str | None,
    journal_since: str | None,
    copied_runtime: bool,
    observe_runtime: bool,
) -> NamedHiveSources:
    identity = _identity(entry)
    stream_argv = [
        executable,
        "stream",
        "--scope",
        "hive",
        "--hive",
        identity.registered_identity,
        "--format",
        "ndjson",
    ]
    if stream_since is not None:
        stream_argv.extend(("--since", stream_since))

    summary_locator = dispatch_log.sink_path({}, dict(entry))
    summary_source_id = f"beadhive.dispatch-summary:{host_id}:{identity.registered_identity}"
    if observe_runtime:
        summary_snapshot = public_readers.read_agent_run_snapshot(
            summary_locator,
            host_id=host_id,
            source_id=summary_source_id,
            copied=copied_runtime,
        )
        summary_observation = _runtime_observation(summary_snapshot)
    else:
        summary_observation = RuntimeObservation(
            None, "unknown", "not_observed", "unknown", None, "source not read by explain"
        )

    journal_root = run_journal.journal_root_for_hive(identity.registered_identity)
    journal_locator = (
        run_journal.journal_path_for_hive(identity.registered_identity, run_id)
        if run_id is not None
        else None
    )
    journal_frame = None
    if observe_runtime and journal_locator is not None:
        frames = public_readers.RunJournalTailReader(
            journal_locator,
            run_id=run_id,
            host_id=host_id,
            source_id=f"beadhive.run-journal:{host_id}:{identity.registered_identity}:{run_id}",
            copied=copied_runtime,
        ).snapshot(since_revision=journal_since)
        journal_frame = frames[0]

    return NamedHiveSources(
        schema_version=SCHEMA_VERSION,
        contract_version=CONTRACT_VERSION,
        identity=identity,
        correlation=HiveCorrelation(
            registered_identity=identity.registered_identity,
            stream_repo_slug=identity.repo_slug,
        ),
        cli=InstalledCli(executable=executable, version=version, features=_features()),
        stream=StreamLaunchDescriptor(
            source_instance_id=f"beadhive.stream:{host_id}:{identity.registered_identity}",
            availability=Availability.AVAILABLE,
            availability_reason=None,
            contract_version=STREAM_CONTRACT_VERSION,
            schema_version=state_stream.SCHEMA_VERSION,
            scope="hive",
            format="ndjson",
            hive_argument=identity.registered_identity,
            emitted_hive=identity.repo_slug,
            argv=tuple(stream_argv),
            cursor=CursorContract("revision", resume_argument="--since"),
        ),
        runtime=RuntimeSourcesDescriptor(
            host_id=host_id,
            availability=Availability.UNKNOWN,
            availability_reason=ResolutionReason.WRITER_COLOCATION_UNVERIFIED,
            summary=SummarySourceDescriptor(
                source_instance_id=summary_source_id,
                availability=Availability.UNKNOWN,
                availability_reason=ResolutionReason.WRITER_COLOCATION_UNVERIFIED,
                contract_version=SUMMARY_CONTRACT_VERSION,
                host_id=host_id,
                scope="host_hive",
                locator=str(summary_locator),
                cursor=CursorContract("revision"),
                observation=summary_observation,
            ),
            journal=JournalSourceDescriptor(
                source_instance_id=(
                    f"beadhive.run-journal:{host_id}:{identity.registered_identity}:{run_id}"
                    if run_id is not None
                    else f"beadhive.run-journal:{host_id}:{identity.registered_identity}:<run_id>"
                ),
                availability=Availability.UNKNOWN,
                availability_reason=ResolutionReason.WRITER_COLOCATION_UNVERIFIED,
                contract_version=run_journal.VERSION,
                host_id=host_id,
                scope="run",
                run_id=run_id,
                locator_root=str(journal_root),
                locator=str(journal_locator) if journal_locator is not None else None,
                cursor=CursorContract("source_revision"),
                observation=_journal_observation(journal_frame),
            ),
        ),
    )


def resolve_named_hive_sources(
    hive_name: str,
    *,
    cfg: dict | None = None,
    requested_host_id: str | None = None,
    run_id: str | None = None,
    stream_since: str | None = None,
    journal_since: str | None = None,
    copied_runtime: bool = False,
    executable_locator: Callable[[str], str | None] = shutil.which,
    version_loader: Callable[[], str] = _installed_version,
    host_id_loader: Callable[[], str] = host.host_id,
    hive_dir_loader: Callable[[Mapping[str, object]], Path] = registry.hive_dir,
) -> DescriptorResolution:
    """Resolve one named registered hive, refusing every ambiguous or unproved prerequisite."""

    cfg = cfg if cfg is not None else config.load()
    matches = registry.hive_matches(cfg, hive_name)
    candidates = tuple(sorted(registry.hive_key(entry) for entry in matches))
    if not matches:
        return _refused(ResolutionReason.HIVE_MISSING)
    if len(matches) != 1:
        return _refused(ResolutionReason.HIVE_AMBIGUOUS, candidates=candidates)
    entry = matches[0]
    try:
        current_host_id = host_id_loader()
    except (KeyError, OSError, ValueError):
        return _refused(ResolutionReason.HOST_IDENTITY_UNAVAILABLE, candidates=candidates)
    if not current_host_id:
        return _refused(ResolutionReason.HOST_IDENTITY_UNAVAILABLE, candidates=candidates)
    if requested_host_id is not None and requested_host_id != current_host_id:
        return _refused(ResolutionReason.WRONG_HOST, candidates=candidates)
    if not hive_dir_loader(entry).is_dir():
        return _refused(ResolutionReason.HIVE_NOT_INSTALLED_ON_HOST, candidates=candidates)
    executable = executable_locator(config.BINARY_ALIAS)
    if not executable:
        return _refused(ResolutionReason.CLI_UNINSTALLED, candidates=candidates)
    executable_path = Path(executable).absolute()
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        return _refused(ResolutionReason.CLI_UNINSTALLED, candidates=candidates)
    try:
        version = version_loader()
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        return _refused(ResolutionReason.CLI_VERSION_UNKNOWN, candidates=candidates)
    if not version:
        return _refused(ResolutionReason.CLI_VERSION_UNKNOWN, candidates=candidates)

    descriptor = _build_descriptor(
        entry,
        host_id=current_host_id,
        executable=str(executable_path),
        version=version,
        run_id=run_id,
        stream_since=stream_since,
        journal_since=journal_since,
        copied_runtime=copied_runtime,
        observe_runtime=True,
    )
    return DescriptorResolution(
        schema_version=SCHEMA_VERSION,
        contract_version=CONTRACT_VERSION,
        decision=ResolutionDecision.AVAILABLE,
        reasons=(),
        candidates=candidates,
        descriptor=descriptor,
    )


def role_explain_sources(entry: Mapping[str, object] | None, hive: str | None) -> dict[str, Any]:
    """Side-effect-free/redacted source facts for ``bh role --explain``.

    Host-local locators and observations are deliberately omitted: explain discloses their
    contracts and locator *mechanisms*, not environment/path values or source contents.
    """

    if entry is None or hive is None:
        return _refused(ResolutionReason.HIVE_MISSING).payload()
    executable = shutil.which(config.BINARY_ALIAS)
    if not executable:
        return _refused(ResolutionReason.CLI_UNINSTALLED).payload()
    executable_path = Path(executable).absolute()
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        return _refused(ResolutionReason.CLI_UNINSTALLED).payload()
    try:
        version = _installed_version()
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        return _refused(ResolutionReason.CLI_VERSION_UNKNOWN).payload()
    try:
        host_id = host.host_id()
    except (KeyError, OSError, ValueError):
        host_id = "<host-unavailable>"
    descriptor = _build_descriptor(
        entry,
        host_id=host_id,
        executable=str(executable_path),
        version=version,
        run_id=None,
        stream_since=None,
        journal_since=None,
        copied_runtime=False,
        observe_runtime=False,
    )
    payload = DescriptorResolution(
        SCHEMA_VERSION,
        CONTRACT_VERSION,
        ResolutionDecision.AVAILABLE,
        (),
        (hive,),
        descriptor,
    ).payload()
    runtime = payload["descriptor"]["runtime"]
    runtime["summary"]["locator"] = "<host-local:dispatch-summary>"
    runtime["journal"]["locator_root"] = "<host-local:run-journals>"
    runtime["journal"]["locator"] = None
    runtime["journal"]["locator_environment"] = "BH_RUN_JOURNAL_PATH"
    return payload
