"""Policy-neutral authoritative sources for the phase-one operator HTTP API."""

from __future__ import annotations

import concurrent.futures
import copy
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import (
    config,
    dispatch_log,
    operator_contract,
    public_readers,
    registry,
    run_journal,
    state_services,
)
from .modules.state import (
    AgentRunSnapshot,
    ProviderSnapshot,
    RunJournalFrame,
    StreamRequest,
    StreamScope,
)
from .public_readers import (
    RunDirectoryEntry,
    RunDirectoryInventory,
)
from .state_stream_polling import PollingStateStreamProvider
from .state_stream_process import StreamProcessScope

_IDENTITY_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
DEFAULT_PROCESS_TIMEOUT = 8.0
DEFAULT_PROCESS_TERM_GRACE = 0.5
DEFAULT_HIVE_SUMMARY_TTL = 60.0
HIVE_REFRESH_CONCURRENCY = 8


def process_limits_for_shutdown(shutdown_budget: float) -> tuple[float, float]:
    """Reserve daemon drain time after a polling timeout and process-tree termination."""

    if not 0 < shutdown_budget < float("inf"):
        raise ValueError("shutdown budget must be finite and greater than zero")
    return shutdown_budget * 0.6, shutdown_budget * 0.1


class OperatorSourceError(RuntimeError):
    """A stable, redacted HTTP-facing source refusal."""

    def __init__(
        self, code: str, message: str, *, status_code: int, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class ExactHive:
    identity: str
    entry: Mapping[str, object]


class RefreshingProvider(Protocol):
    def refresh(self, request: StreamRequest) -> ProviderSnapshot: ...


SummaryReader = Callable[[Path, str, str], AgentRunSnapshot]
JournalReader = Callable[[int, str, str, str], RunJournalFrame]


def _default_summary_reader(path: Path, host_id: str, source_id: str) -> AgentRunSnapshot:
    return public_readers.read_agent_run_snapshot(path, host_id=host_id, source_id=source_id)


def validate_canonical_identity(value: str) -> tuple[str, str, str]:
    """Accept only the unambiguous decoded provider/org/repo representation."""

    if not isinstance(value, str) or "\\" in value or any(ord(char) < 32 for char in value):
        raise OperatorSourceError(
            "invalid_hive_identity",
            "Hive identity must be one canonical provider/organization/repository triplet.",
            status_code=400,
        )
    parts = value.split("/")
    if len(parts) != 3 or any(
        not part or part in {".", ".."} or not _IDENTITY_SEGMENT.fullmatch(part) for part in parts
    ):
        raise OperatorSourceError(
            "invalid_hive_identity",
            "Hive identity must be one canonical provider/organization/repository triplet.",
            status_code=400,
        )
    canonical = "/".join(parts)
    if canonical != value:
        raise OperatorSourceError(
            "invalid_hive_identity",
            "Hive identity must use its canonical representation.",
            status_code=400,
        )
    return parts[0], parts[1], parts[2]


@dataclass(frozen=True)
class _CachedSummary:
    entry: Mapping[str, object]
    summary: Mapping[str, object]
    started: float
    observed: float
    observed_at: int


class HiveSummaryCache:
    """Per-hive factory directory summaries, refreshed in the background.

    Directory reads never wait on a per-hive source: :meth:`read` returns what is cached (or
    a pending entry for a hive never observed) and only *schedules* refreshes for missing,
    expired, dirty, or registry-changed hives.  At most one refresh per hive is in flight and
    a fixed worker pool caps global refresh concurrency.  Every per-hive read through
    :meth:`OperatorSources.refresh_hive_state` records its outcome here, so single-hive reads
    warm the directory too.  Summaries are cached per hive, never as a whole response, so one
    unhealthy hive stays stale without poisoning the directory.
    """

    def __init__(
        self,
        refresh: Callable[[ExactHive], object],
        *,
        ttl: float = DEFAULT_HIVE_SUMMARY_TTL,
        max_workers: int = HIVE_REFRESH_CONCURRENCY,
        clock: Callable[[], float] = time.monotonic,
        now_millis: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        if not 0 < ttl < float("inf"):
            raise ValueError("hive summary ttl must be finite and greater than zero")
        if max_workers < 1:
            raise ValueError("hive refresh concurrency must be positive")
        self._refresh = refresh
        self.ttl = ttl
        self.max_workers = max_workers
        self.clock = clock
        self._now_millis = now_millis
        self._condition = threading.Condition()
        self._entries: dict[str, _CachedSummary] = {}
        self._inflight: set[str] = set()
        # identity -> clock() time the dirty mark was raised, so a completing refresh can tell
        # a mark it already reflects (raised before it started reading) from one raised while it
        # was in flight (which it cannot reflect and so must not swallow).
        self._dirty: dict[str, float] = {}
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._closed = False

    def record(self, hive: ExactHive, summary: Mapping[str, object], *, started: float) -> None:
        """Store one observed summary unless a newer-started observation already landed."""

        with self._condition:
            current = self._entries.get(hive.identity)
            if current is not None and current.started > started:
                return
            self._entries[hive.identity] = _CachedSummary(
                entry=dict(hive.entry),
                summary=summary,
                started=started,
                observed=self.clock(),
                observed_at=self._now_millis(),
            )
            # A dirty mark raised before this refresh started is captured by its result; one
            # raised while it was in flight (or after) is not — leave it dirty so the next read
            # schedules a trailing refresh instead of silently losing the signal.
            dirty_at = self._dirty.get(hive.identity)
            if dirty_at is not None and dirty_at <= started:
                del self._dirty[hive.identity]
            self._condition.notify_all()

    def mark_dirty(self, identity: str) -> None:
        """Force the next directory read to schedule a refresh for *identity*."""

        with self._condition:
            self._dirty[identity] = self.clock()

    def read(self, hives: Sequence[ExactHive]) -> list[dict[str, object]]:
        """Return one summary per registered hive without awaiting any refresh."""

        now = self.clock()
        members = {hive.identity for hive in hives}
        scheduled: list[ExactHive] = []
        results: list[dict[str, object]] = []
        with self._condition:
            for identity in tuple(self._entries):
                if identity not in members:
                    del self._entries[identity]
            for identity in tuple(self._dirty):
                if identity not in members:
                    del self._dirty[identity]
            for hive in hives:
                cached = self._entries.get(hive.identity)
                expired = (
                    cached is None
                    or now - cached.observed >= self.ttl
                    or hive.identity in self._dirty
                    or dict(hive.entry) != cached.entry
                )
                if expired and hive.identity not in self._inflight and self._submit(hive):
                    scheduled.append(hive)
                refreshing = hive.identity in self._inflight
                if cached is None:
                    summary = operator_contract.factory_hive_pending_summary(hive.entry)
                    freshness = {
                        "state": "refreshing" if refreshing else "unknown",
                        "asOf": None,
                        "expiresAt": None,
                    }
                else:
                    summary = dict(cached.summary)
                    state = "refreshing" if refreshing else ("stale" if expired else "fresh")
                    freshness = {
                        "state": state,
                        "asOf": cached.observed_at,
                        "expiresAt": cached.observed_at + int(self.ttl * 1000),
                    }
                results.append({**summary, "freshness": freshness})
        return results

    def _submit(self, hive: ExactHive) -> bool:
        # Called with the condition held.
        if self._closed:
            return False
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="hive-summary-refresh"
            )
        self._inflight.add(hive.identity)
        try:
            self._executor.submit(self._run, hive)
        except RuntimeError:
            self._inflight.discard(hive.identity)
            return False
        return True

    def _run(self, hive: ExactHive) -> None:
        try:
            self._refresh(hive)
        except Exception:
            # The refresh records its own failure as an unavailable summary.
            pass
        finally:
            with self._condition:
                self._inflight.discard(hive.identity)
                self._condition.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no background refresh is in flight; return whether that happened."""

        with self._condition:
            return self._condition.wait_for(lambda: not self._inflight, timeout=timeout)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


class OperatorSources:
    """Exact registry/source adapter shared by REST and the later event relay.

    The polling provider is constructed once.  Its provider-instance revision domain and
    same-scope refresh coalescing therefore survive individual HTTP requests.
    """

    def __init__(
        self,
        *,
        cfg: dict | None = None,
        host_id: str,
        provider: RefreshingProvider | None = None,
        summary_reader: SummaryReader = _default_summary_reader,
        journal_reader: JournalReader | None = None,
        journal_base: Path | None = None,
        dispatch_sink_for_entry: Callable[[dict, Mapping[str, object]], Path] | None = None,
        process_timeout: float = DEFAULT_PROCESS_TIMEOUT,
        process_term_grace: float = DEFAULT_PROCESS_TERM_GRACE,
        max_records_per_read: int = public_readers.DEFAULT_MAX_JOURNAL_RECORDS,
        max_record_bytes: int = public_readers.DEFAULT_MAX_JOURNAL_RECORD_BYTES,
        max_read_bytes: int = public_readers.DEFAULT_MAX_JOURNAL_READ_BYTES,
        max_inventory_roots: int = public_readers.DEFAULT_MAX_INVENTORY_ROOTS,
        max_inventory_entries: int = public_readers.DEFAULT_MAX_INVENTORY_ENTRIES,
        max_inventory_bytes: int = public_readers.DEFAULT_MAX_INVENTORY_BYTES,
        hive_summary_ttl: float = DEFAULT_HIVE_SUMMARY_TTL,
        hive_refresh_concurrency: int = HIVE_REFRESH_CONCURRENCY,
    ) -> None:
        self.cfg = cfg if cfg is not None else config.load()
        self.hive_summaries = HiveSummaryCache(
            self._refresh_hive_summary,
            ttl=hive_summary_ttl,
            max_workers=hive_refresh_concurrency,
        )
        self.host_id = host_id
        self._summary_reader = summary_reader
        self._journal_reader = journal_reader
        self.max_records_per_read = max_records_per_read
        self.max_record_bytes = max_record_bytes
        self.max_read_bytes = max_read_bytes
        self.max_inventory_roots = max_inventory_roots
        self.max_inventory_entries = max_inventory_entries
        self.max_inventory_bytes = max_inventory_bytes
        self._journal_base = journal_base
        self._dispatch_sink_for_entry = dispatch_sink_for_entry or dispatch_log.sink_path
        self._process_scope: StreamProcessScope | None = None
        if provider is None:
            # Pin the provider to exact triplet matching even when the interactive CLI is
            # configured for prefix/flexible resolution.  The HTTP boundary resolves and
            # rejects duplicates before the provider sees the request.
            provider_cfg = copy.deepcopy(self.cfg)
            workspace = dict(provider_cfg.get("git_workspace") or {})
            workspace["hive_match"] = "triplet"
            provider_cfg["git_workspace"] = workspace
            self._process_scope = StreamProcessScope(
                timeout=process_timeout,
                term_grace=process_term_grace,
            )
            provider = PollingStateStreamProvider(
                provider_cfg,
                process_scope=self._process_scope,
            )
        self.provider = provider
        self._projections = state_services.read_projection_service(
            snapshot=self.provider.refresh,
            agent_runs=lambda source, host_id, source_id: self._summary_reader(
                Path(source), host_id, source_id
            ),
        )

    def close(self) -> None:
        """Cancel production polling process trees; injected providers remain caller-owned."""

        self.hive_summaries.close()
        if self._process_scope is not None:
            self._process_scope.close()

    def registered_hives(self) -> tuple[ExactHive, ...]:
        seen: dict[str, Mapping[str, object]] = {}
        result = []
        for raw_entry in registry.hives(self.cfg):
            entry = dict(raw_entry)
            try:
                identity = registry.hive_key(entry)
                validate_canonical_identity(identity)
                prefix = entry["prefix"]
            except (KeyError, TypeError, ValueError, OperatorSourceError) as exc:
                raise OperatorSourceError(
                    "invalid_registry",
                    "The hive registry contains an invalid operator identity.",
                    status_code=503,
                    retryable=True,
                ) from exc
            if not isinstance(prefix, str) or not prefix:
                raise OperatorSourceError(
                    "invalid_registry",
                    "The hive registry contains an invalid operator identity.",
                    status_code=503,
                    retryable=True,
                )
            if identity in seen:
                raise OperatorSourceError(
                    "ambiguous_hive_identity",
                    "The canonical hive identity maps to more than one registry entry.",
                    status_code=409,
                )
            seen[identity] = entry
            result.append(ExactHive(identity=identity, entry=entry))
        return tuple(sorted(result, key=lambda item: item.identity))

    def resolve_hive(self, identity: str) -> ExactHive:
        validate_canonical_identity(identity)
        matches = [hive for hive in self.registered_hives() if hive.identity == identity]
        if not matches:
            raise OperatorSourceError(
                "hive_not_found", "The exact registered hive was not found.", status_code=404
            )
        # registered_hives has already made duplicate equality a deterministic conflict.
        return matches[0]

    def refresh_hive(self, hive: ExactHive) -> tuple[ProviderSnapshot, AgentRunSnapshot]:
        bead_state = self.refresh_hive_state(hive)

        sink = self._dispatch_sink_for_entry(self.cfg, hive.entry)
        try:
            runtime_state = self._projections.agent_runs(
                str(sink),
                self.host_id,
                f"beadhive.dispatch-summary:{self.host_id}:{hive.identity}",
            )
        except Exception as exc:
            raise OperatorSourceError(
                "runtime_source_unavailable",
                "The host-local runtime summary source is unavailable.",
                status_code=503,
                retryable=True,
            ) from exc
        return bead_state, runtime_state

    def factory_hive_directory(self) -> list[dict[str, object]]:
        """Return every registered hive's cached summary; never waits on a per-hive read."""

        return self.hive_summaries.read(self.registered_hives())

    def _refresh_hive_summary(self, hive: ExactHive) -> None:
        self.refresh_hive_state(hive)

    def refresh_hive_state(self, hive: ExactHive) -> ProviderSnapshot:
        """Read only generic bead state for a registered hive.

        Factory inventory must not make a missing host-local dispatch journal turn an
        otherwise readable hive into an unavailable hive.  The richer per-hive operator
        snapshot composes this read with the runtime summary in :meth:`refresh_hive`.
        Every outcome also updates the directory's cached summary for this hive.
        """

        started = self.hive_summaries.clock()
        try:
            bead_state = self._read_hive_state(hive)
        except OperatorSourceError as exc:
            self._record_hive_summary(hive, None, reason=exc.code, started=started)
            raise
        self._record_hive_summary(hive, bead_state, reason=None, started=started)
        return bead_state

    def _record_hive_summary(
        self,
        hive: ExactHive,
        bead_state: ProviderSnapshot | None,
        *,
        reason: str | None,
        started: float,
    ) -> None:
        try:
            summary = operator_contract.factory_hive_summary(
                hive.entry, bead_state, unavailable_reason=reason
            )
        except Exception:
            summary = operator_contract.factory_hive_summary(
                hive.entry, None, unavailable_reason="snapshot_source_unavailable"
            )
        self.hive_summaries.record(hive, summary, started=started)

    def _read_hive_state(self, hive: ExactHive) -> ProviderSnapshot:
        request = StreamRequest(StreamScope.HIVE, hive=hive.identity)
        try:
            bead_state = self._projections.snapshot(request)
        except Exception as exc:
            raise OperatorSourceError(
                "snapshot_source_unavailable",
                "The authoritative hive snapshot source is unavailable.",
                status_code=503,
                retryable=True,
            ) from exc
        if bead_state.scope is not StreamScope.HIVE:
            raise OperatorSourceError(
                "snapshot_scope_mismatch",
                "The snapshot source returned a different scope.",
                status_code=409,
            )
        records: list[object] = [*bead_state.issues]
        for name in (
            "work_dependencies",
            "gate_requests",
            "epic_schedules",
            "assignments",
        ):
            records.extend(getattr(bead_state, name))
        if any(getattr(record, "hive", None) != hive.identity for record in records):
            raise OperatorSourceError(
                "snapshot_hive_mismatch",
                "The snapshot source returned an entity for a different hive.",
                status_code=409,
            )
        return bead_state

    def locate_run(self, run_id: str) -> tuple[ExactHive, RunDirectoryEntry]:
        # Reuse the writer's exact validation, then resolve through one host-wide inventory.
        try:
            run_journal.journal_path_for_hive("validation/only/hive", run_id)
        except ValueError as exc:
            raise OperatorSourceError(
                "invalid_run_id",
                "Run identity must be one path-safe outer run token.",
                status_code=400,
            ) from exc
        hives = self.registered_hives()
        inventory = self.run_directory(hives)
        try:
            entry = inventory.resolve(run_id)
        except public_readers.RunDirectoryError as exc:
            status_by_code = {
                "run_not_found": 404,
                "ambiguous_run_id": 409,
                "invalid_run_source": 409,
                "multiply_linked_run_source": 409,
                "hive_identity_mismatch": 409,
                "run_source_unavailable": 503,
                "run_directory_unavailable": 503,
                "journal_root_collision": 409,
            }
            messages = {
                "run_not_found": "The exact outer run was not found.",
                "ambiguous_run_id": "The outer run identity exists in more than one hive.",
                "invalid_run_source": ("The outer run source is not a regular host-local journal."),
                "multiply_linked_run_source": (
                    "The outer run source has more than one filesystem identity."
                ),
                "hive_identity_mismatch": (
                    "The outer run source disagrees with its authoritative hive identity."
                ),
                "run_source_unavailable": "The outer run source is unavailable.",
                "run_directory_unavailable": "The authoritative run directory is unavailable.",
                "journal_root_collision": (
                    "More than one hive identity maps to the same run-journal source."
                ),
            }
            code = exc.code
            raise OperatorSourceError(
                code,
                messages.get(code, "The authoritative run directory is unavailable."),
                status_code=status_by_code.get(code, 503),
                retryable=status_by_code.get(code, 503) == 503,
            ) from None
        by_identity = {hive.identity: hive for hive in hives}
        hive = by_identity.get(entry.hive_id)
        if hive is None:
            raise OperatorSourceError(
                "run_hive_unknown",
                "The outer run belongs to an unknown hive identity.",
                status_code=409,
            )
        return hive, entry

    def run_directory(self, hives: tuple[ExactHive, ...] | None = None) -> RunDirectoryInventory:
        """Read the public host-wide journal inventory outside per-hive sequence domains."""

        exact_hives = hives if hives is not None else self.registered_hives()
        roots = (
            (
                hive.identity,
                run_journal.journal_root_for_hive(hive.identity, base=self._journal_base),
            )
            for hive in exact_hives
        )
        return public_readers.read_run_directory(
            roots,
            max_records_per_read=self.max_records_per_read,
            max_record_bytes=self.max_record_bytes,
            max_read_bytes=self.max_read_bytes,
            max_inventory_roots=self.max_inventory_roots,
            max_inventory_entries=self.max_inventory_entries,
            max_inventory_bytes=self.max_inventory_bytes,
        )

    def read_run(self, hive: ExactHive, source: RunDirectoryEntry, run_id: str) -> RunJournalFrame:
        if (
            source.hive_id != hive.identity
            or source.run_id != run_id
            or source.device is None
            or source.inode is None
        ):
            raise OperatorSourceError(
                "activity_source_changed",
                "The authoritative run activity source changed after discovery.",
                status_code=503,
                retryable=True,
            )
        descriptor: int | None = None
        try:
            descriptor = public_readers.open_run_journal_descriptor(source)
            source_id = f"beadhive.run-journal:{self.host_id}:{hive.identity}:{run_id}"
            if self._journal_reader is None:
                frame = public_readers.read_run_journal_descriptor(
                    descriptor,
                    run_id=run_id,
                    host_id=self.host_id,
                    source_id=source_id,
                    expected_hive_id=hive.identity,
                    max_records_per_read=self.max_records_per_read,
                    max_record_bytes=self.max_record_bytes,
                    max_read_bytes=self.max_read_bytes,
                )
            else:
                frame = self._journal_reader(descriptor, run_id, self.host_id, source_id)
        except public_readers.RunJournalSourceChanged as exc:
            raise OperatorSourceError(
                "activity_source_changed",
                "The authoritative run activity source changed after discovery.",
                status_code=503,
                retryable=True,
            ) from exc
        except OSError as exc:
            raise OperatorSourceError(
                "activity_source_changed",
                "The authoritative run activity source changed after discovery.",
                status_code=503,
                retryable=True,
            ) from exc
        except Exception as exc:
            raise OperatorSourceError(
                "activity_source_unavailable",
                "The authoritative run activity source is unavailable.",
                status_code=503,
                retryable=True,
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if frame.run_id != run_id:
            raise OperatorSourceError(
                "activity_run_mismatch",
                "The activity source belongs to a different outer run.",
                status_code=409,
            )
        if frame.coverage_reason == "source_missing":
            raise OperatorSourceError(
                "run_not_found", "The exact outer run was not found.", status_code=404
            )
        if frame.coverage_reason == "source_unreadable":
            raise OperatorSourceError(
                "activity_source_unavailable",
                "The authoritative run activity source is unavailable.",
                status_code=503,
                retryable=True,
            )
        if frame.coverage_reason in {
            "run_id_mismatch",
            "hive_identity_mismatch",
            "identity_drift",
            "provider_continuation_aliases_run_id",
            "provider_continuation_drift",
        }:
            raise OperatorSourceError(
                "activity_identity_mismatch",
                "The activity source contains conflicting identity data.",
                status_code=409,
            )
        if not frame.records:
            raise OperatorSourceError(
                "activity_source_empty",
                "The outer run has no authoritative activity records.",
                status_code=503,
                retryable=True,
            )
        if any(
            record.get("run_id") != run_id or record.get("hive") != hive.identity
            for record in frame.records
        ):
            raise OperatorSourceError(
                "activity_identity_mismatch",
                "The activity source contains conflicting identity data.",
                status_code=409,
            )
        return frame
