"""Authenticated factory projection and exact host-wide run directory.

This module is deliberately a composition boundary.  Registry rows, named-hive source
descriptors, bead-state reads, host leases, and public run journals keep their own authorities;
the daemon projects their explicit results without copying private locators into the wire model.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import (
    config,
    dolt_health,
    host_lease,
    hosts,
    registry,
    source_descriptors,
    store_locator,
)
from .daemon_contract import (
    DaemonStatus,
    DependencyStatus,
    FactoryAssignment,
    FactoryCapability,
    FactoryCoverage,
    FactoryHost,
    FactoryRelationship,
    FactoryResponse,
    FactorySourceCoverage,
    HiveDescriptor,
    ReadinessStatus,
    encode_hive_id,
)
from .public_readers import (
    Coverage,
    RunDirectoryInventory,
)

FactoryCoverageState = Literal["complete", "partial", "unavailable"]
AssignmentState = Literal["held", "renewal-due", "expired", "unassigned", "unknown"]
DEFAULT_RUN_JOURNAL_STALE_AFTER_SECONDS = 900.0
_HQ_PROBE_POLL_SECONDS = 0.01
_HQ_PROBE_INPUT_LIMIT = 16_384


@dataclass(frozen=True)
class HiveSourceObservation:
    readiness: ReadinessStatus | str
    coverage: FactoryCoverageState
    reason_code: str | None = None


@dataclass(frozen=True)
class AssignmentObservation:
    host_id: str
    role: str
    state: AssignmentState
    lease_expires_at: int | None = None


@dataclass(frozen=True)
class DependencyObservation:
    status: ReadinessStatus | str
    reason_code: str | None = None


def _hq_probe_command() -> tuple[str, ...]:
    return (sys.executable, "-m", "beadhive.daemon_hq_probe")


def _stop_probe(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.communicate(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def _bounded_hq_readiness(
    path: Path,
    *,
    timeout: float,
    cancellation_event: threading.Event | None,
) -> DependencyObservation:
    """Observe local HQ readiness in a killable process without remote fetch side effects."""

    request = json.dumps({"path": str(path)}, separators=(",", ":"))
    if len(request.encode()) > _HQ_PROBE_INPUT_LIMIT:
        return DependencyObservation("unavailable", "hq_path_unavailable")
    try:
        process = subprocess.Popen(
            _hq_probe_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            close_fds=True,
        )
    except OSError:
        return DependencyObservation("unavailable", "hq_status_unavailable")

    deadline = time.monotonic() + timeout
    pending_input: str | None = request
    try:
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                _stop_probe(process)
                return DependencyObservation("unavailable", "hq_status_cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_probe(process)
                return DependencyObservation("unavailable", "hq_status_timeout")
            try:
                output, _stderr = process.communicate(
                    input=pending_input,
                    timeout=min(remaining, _HQ_PROBE_POLL_SECONDS),
                )
                break
            except subprocess.TimeoutExpired:
                pending_input = None
        if process.returncode != 0:
            return DependencyObservation("unavailable", "hq_status_unavailable")
        state = output.strip()
        if state == "ready":
            return DependencyObservation("ready")
        if state in {"hq_not_initialized", "hq_path_unavailable"}:
            return DependencyObservation("unavailable", state)
        return DependencyObservation("unavailable", "hq_status_unavailable")
    finally:
        if process.poll() is None:
            _stop_probe(process)


def _millis(value: str | float | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return max(0, int(float(value) * 1_000))
    try:
        return max(0, int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000))
    except ValueError:
        return None


def _coverage_state(value: Coverage) -> FactoryCoverageState:
    if value is Coverage.COMPLETE:
        return "complete"
    if value is Coverage.PARTIAL:
        return "partial"
    return "unavailable"


class FactoryDirectory:
    """Compose exact source facts into the strict v1 factory response."""

    def __init__(
        self,
        *,
        sources,
        host_id: str,
        service_instance_id: str,
        started_at: int,
        clock_millis: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        describe_hive: Callable[[object], HiveSourceObservation] | None = None,
        load_run_directory: Callable[[Sequence[object]], RunDirectoryInventory] | None = None,
        load_assignment: Callable[[object], AssignmentObservation] | None = None,
        hq_status: Callable[[], DependencyObservation] | None = None,
        dolt_status: Callable[[Sequence[object]], DependencyObservation] | None = None,
        journal_stale_after_seconds: float = DEFAULT_RUN_JOURNAL_STALE_AFTER_SECONDS,
        dolt_probe_timeout_seconds: float = dolt_health.DEFAULT_PROBE_TIMEOUT,
        activity_publish_configured: bool = False,
    ) -> None:
        if not math.isfinite(journal_stale_after_seconds) or journal_stale_after_seconds <= 0:
            raise ValueError("journal stale threshold must be finite and greater than zero")
        if not math.isfinite(dolt_probe_timeout_seconds) or dolt_probe_timeout_seconds <= 0:
            raise ValueError("Dolt probe timeout must be finite and greater than zero")
        self.sources = sources
        self.host_id = host_id
        self.service_instance_id = service_instance_id
        self.started_at = started_at
        self.clock_millis = clock_millis
        self._describe_hive = describe_hive or self._default_describe_hive
        self._load_run_directory = load_run_directory or self._default_run_directory
        self._load_assignment = load_assignment or self._default_assignment
        self._hq_status = hq_status
        self._dolt_status = dolt_status or self._default_dolt_status
        self.journal_stale_after_seconds = float(journal_stale_after_seconds)
        self.dolt_probe_timeout_seconds = float(dolt_probe_timeout_seconds)
        self.dependency_probe_timeout_seconds = self.dolt_probe_timeout_seconds
        self.activity_publish_configured = bool(activity_publish_configured)

    def _activity_publish_capability(
        self, inventory: RunDirectoryInventory, *, accepting_work: bool
    ) -> FactoryCapability:
        if not self.activity_publish_configured:
            return FactoryCapability(
                name="activity-publish", available=False, reason_code="not_implemented"
            )
        if not accepting_work:
            return FactoryCapability(
                name="activity-publish",
                available=False,
                reason_code="daemon_not_accepting",
            )
        if not inventory.exact_lookup_available:
            return FactoryCapability(
                name="activity-publish",
                available=False,
                reason_code=inventory.coverage_reason or "run_directory_unavailable",
            )
        return FactoryCapability(name="activity-publish", available=True)

    def _default_describe_hive(self, hive) -> HiveSourceObservation:
        resolution = source_descriptors.resolve_named_hive_sources(
            hive.identity,
            cfg=self.sources.cfg,
            requested_host_id=self.host_id,
        )
        if resolution.decision is not source_descriptors.ResolutionDecision.AVAILABLE:
            reason = resolution.reasons[0].value if resolution.reasons else "source_unavailable"
            return HiveSourceObservation("unavailable", "unavailable", reason)
        try:
            self.sources.refresh_hive_state(hive)
        except Exception as exc:
            reason = getattr(exc, "code", "bead_state_unavailable")
            return HiveSourceObservation("unavailable", "unavailable", str(reason))
        descriptor = resolution.descriptor
        if descriptor is None:
            return HiveSourceObservation("unavailable", "unavailable", "source_unavailable")
        summary = descriptor.runtime.summary.observation
        if summary.coverage in {"partial", "degraded", "unknown"}:
            return HiveSourceObservation(
                "ready", "partial", summary.coverage_reason or "runtime_coverage_unknown"
            )
        if summary.freshness == "stale":
            return HiveSourceObservation("degraded", "partial", "stale_data")
        return HiveSourceObservation("ready", "complete")

    def _default_run_directory(self, hives: Sequence[object]) -> RunDirectoryInventory:
        return self.sources.run_directory(tuple(hives))

    def _default_assignment(self, hive) -> AssignmentObservation:
        hq_dir = config.hq_dir()
        try:
            lease = host_lease.read_cached(str(hive.entry["prefix"]), cwd=hq_dir)
        except Exception:
            return AssignmentObservation(self.host_id, "unknown", "unknown")
        if lease is None or lease.is_tombstone:
            return AssignmentObservation(self.host_id, "unknown", "unassigned")
        try:
            role = hosts.load(hq_dir, lease.host_id).role
        except Exception:
            role = "unknown"
        if lease.is_expired():
            state: AssignmentState = "expired"
        elif host_lease.lease_state(lease) == "expiring":
            state = "renewal-due"
        else:
            state = "held"
        return AssignmentObservation(
            lease.host_id,
            role,
            state,
            _millis(lease.expires_at),
        )

    def _default_hq_status(
        self, cancellation_event: threading.Event | None = None
    ) -> DependencyObservation:
        entries = [
            entry
            for entry in self.sources.cfg.get("managed_repos", ())
            if str(entry.get("kind", "")) == registry.HQ_KIND
        ]
        if len(entries) > 1:
            return DependencyObservation("unavailable", "hq_identity_collision")
        if not entries:
            return DependencyObservation("unknown", "hq_not_registered")
        try:
            hq_dir = config.hq_dir().absolute()
        except (OSError, RuntimeError):
            return DependencyObservation("unavailable", "hq_path_unavailable")
        return _bounded_hq_readiness(
            hq_dir,
            timeout=self.dependency_probe_timeout_seconds,
            cancellation_event=cancellation_event,
        )

    def _default_dolt_status(self, hives: Sequence[object]) -> DependencyObservation:
        """Observe each hive's persisted engine mode without starting or opening a store.

        Embedded mode is a filesystem observation. Every configured server flavor is checked
        through bd's already-owned endpoint settings with one bounded protocol probe. The read
        path never invokes bd and therefore cannot auto-start a shared server or manufacture an
        embedded shadow when the configured server is unavailable.
        """

        if not hives:
            return DependencyObservation("unknown", "no_registered_hives")

        ready = 0
        failures: list[str] = []
        server_hives = 0
        for hive in hives:
            try:
                hive_dir = registry.hive_dir(hive.entry)
                mode = store_locator.dolt_mode(hive_dir)
            except (KeyError, OSError, TypeError, ValueError):
                failures.append("dolt_mode_unknown")
                continue
            if mode == "embedded":
                if dolt_health.mismatch_reason(hive_dir):
                    failures.append("dolt_mode_mismatch")
                else:
                    database_name = store_locator.dolt_database(hive_dir)
                    database_path = Path(database_name)
                    if (
                        database_path.is_absolute()
                        or len(database_path.parts) != 1
                        or database_path.parts[0] in {"", ".", ".."}
                        or "\\" in database_name
                    ):
                        failures.append("dolt_embedded_database_invalid")
                        continue
                    metadata = dolt_health.probe_embedded_schema_version(
                        store_locator.database_dir(hive_dir, database=database_name),
                        timeout=self.dolt_probe_timeout_seconds,
                        metadata_only=True,
                    )
                    if metadata.metadata_valid:
                        ready += 1
                    else:
                        failures.append(
                            metadata.reason_code or "dolt_embedded_metadata_unavailable"
                        )
            elif mode == "server":
                server_hives += 1
            else:
                failures.append("dolt_mode_unknown")

        if server_hives:
            host, port = dolt_health.server_endpoint()
            probe = dolt_health.probe_endpoint(
                host,
                port,
                timeout=self.dolt_probe_timeout_seconds,
            )
            if probe.reachable:
                ready += server_hives
            else:
                failures.extend("dolt_server_unavailable" for _ in range(server_hives))

        if ready == len(hives):
            return DependencyObservation("ready")
        if ready:
            return DependencyObservation("degraded", "partial_dolt_availability")
        if failures and all(reason == "dolt_mode_unknown" for reason in failures):
            return DependencyObservation("unknown", "dolt_mode_unknown")
        reason = next(
            (item for item in failures if item != "dolt_mode_unknown"), "dolt_unavailable"
        )
        return DependencyObservation("unavailable", reason)

    def _journal_status(
        self, inventory: RunDirectoryInventory, *, generated_at: int
    ) -> tuple[FactoryCoverageState, str | None, DependencyObservation]:
        state = _coverage_state(inventory.coverage)
        reason = inventory.coverage_reason
        observed_mtimes = tuple(
            float(entry.modified_at)
            for entry in inventory.entries
            if entry.state == "available"
            and entry.modified_at is not None
            and math.isfinite(float(entry.modified_at))
        )
        oldest_age = (
            max(0.0, generated_at / 1_000 - min(observed_mtimes)) if observed_mtimes else None
        )
        if (
            state == "complete"
            and oldest_age is not None
            and oldest_age > self.journal_stale_after_seconds
        ):
            state = "partial"
            reason = "run_journals_stale"
        status = {
            "complete": "ready",
            "partial": "degraded",
            "unavailable": "unavailable",
        }[state]
        return state, reason, DependencyObservation(status, reason)

    @staticmethod
    def _aggregate_hives(observations: Sequence[HiveSourceObservation]) -> DependencyObservation:
        if not observations:
            return DependencyObservation("unknown", "no_registered_hives")
        ready = sum(str(item.readiness) == ReadinessStatus.READY for item in observations)
        if ready == len(observations):
            return DependencyObservation("ready")
        if ready:
            return DependencyObservation("degraded", "partial_hive_availability")
        return DependencyObservation("unavailable", "hive_sources_unavailable")

    @staticmethod
    def _relationships(hives: Sequence[object]) -> tuple[FactoryRelationship, ...]:
        identities = {hive.identity for hive in hives}
        result = []
        for hive in hives:
            upstream = hive.entry.get("upstream")
            if not isinstance(upstream, str) or not upstream:
                continue
            candidate = f"{hive.entry['provider']}/{upstream}"
            result.append(
                FactoryRelationship(
                    from_hive_id=hive.identity,
                    to_hive_id=candidate if candidate in identities else None,
                    kind="upstream" if candidate in identities else "upstream-unknown",
                )
            )
        return tuple(result)

    def snapshot(
        self,
        *,
        ready: bool,
        accepting_work: bool,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, object]:
        generated_at = self.clock_millis()
        hives = self.sources.registered_hives()
        observations = tuple(self._describe_hive(hive) for hive in hives)
        inventory = self._load_run_directory(hives)
        hive_dependency = self._aggregate_hives(observations)
        journal_state, journal_reason, journal_dependency = self._journal_status(
            inventory, generated_at=generated_at
        )
        hq_dependency = (
            self._hq_status()
            if self._hq_status is not None
            else self._default_hq_status(cancellation_event)
        )
        dependencies = (
            hq_dependency,
            self._dolt_status(hives),
            hive_dependency,
            journal_dependency,
        )
        dependency_models = tuple(
            DependencyStatus(name=name, status=item.status, reason_code=item.reason_code)
            for name, item in zip(
                ("hq", "dolt", "bead-state", "run-journals"), dependencies, strict=True
            )
        )
        if not ready:
            overall: ReadinessStatus | str = "unavailable"
        elif all(item.status == ReadinessStatus.READY for item in dependency_models):
            overall = "ready"
        else:
            overall = "degraded"

        source_states = {
            "registry": FactorySourceCoverage(
                state="complete", detail=None, generated_at=generated_at
            ),
            "source-descriptors": FactorySourceCoverage(
                state=(
                    "complete"
                    if observations and all(item.coverage == "complete" for item in observations)
                    else "unavailable"
                    if observations and all(item.coverage == "unavailable" for item in observations)
                    else "partial"
                ),
                detail=next((item.reason_code for item in observations if item.reason_code), None),
                generated_at=generated_at,
            ),
            "run-journals": FactorySourceCoverage(
                state=journal_state,
                detail=journal_reason,
                generated_at=generated_at,
            ),
        }
        coverage_states = {item.state for item in source_states.values()}
        coverage_state: FactoryCoverageState = (
            "complete"
            if coverage_states == {"complete"}
            else "unavailable"
            if coverage_states == {"unavailable"}
            else "partial"
        )
        response = FactoryResponse(
            generated_at=generated_at,
            host=FactoryHost(
                host_id=self.host_id,
                service_instance_id=self.service_instance_id,
            ),
            status=DaemonStatus(
                readiness=overall,
                accepting_work=accepting_work,
                started_at=self.started_at,
                dependencies=dependency_models,
            ),
            hives=tuple(
                HiveDescriptor(
                    hive_id=hive.identity,
                    encoded_hive_id=encode_hive_id(hive.identity),
                    provider=str(hive.entry["provider"]),
                    org=str(hive.entry["org"]),
                    repo=str(hive.entry["repo"]),
                    prefix=str(hive.entry["prefix"]),
                    kind=str(hive.entry.get("kind", "")),
                    readiness=observation.readiness,
                )
                for hive, observation in zip(hives, observations, strict=True)
            ),
            relationships=self._relationships(hives),
            assignments=tuple(
                FactoryAssignment(
                    hive_id=hive.identity,
                    host_id=assignment.host_id,
                    role=assignment.role,
                    state=assignment.state,
                    lease_expires_at=assignment.lease_expires_at,
                )
                for hive in hives
                for assignment in (self._load_assignment(hive),)
            ),
            capabilities=(
                FactoryCapability(name="snapshot", available=True),
                FactoryCapability(name="events", available=True),
                FactoryCapability(
                    name="activity-read",
                    available=inventory.exact_lookup_available,
                    reason_code=(
                        None if inventory.exact_lookup_available else inventory.coverage_reason
                    ),
                ),
                self._activity_publish_capability(inventory, accepting_work=accepting_work),
                FactoryCapability(
                    name="terminal", available=False, reason_code="pty_verdict_pending"
                ),
            ),
            coverage=FactoryCoverage(
                state=coverage_state,
                generated_at=generated_at,
                sources=source_states,
            ),
        )
        return response.to_wire()


__all__ = [
    "AssignmentObservation",
    "DependencyObservation",
    "FactoryDirectory",
    "HiveSourceObservation",
]
