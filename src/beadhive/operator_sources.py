"""Policy-neutral authoritative sources for the phase-one operator HTTP API."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import config, dispatch_log, public_readers, registry, run_journal
from .public_readers import AgentRunSnapshot, RunJournalFrame
from .state_stream import ProviderSnapshot, StreamRequest, StreamScope
from .state_stream_polling import PollingStateStreamProvider
from .state_stream_process import StreamProcessScope

_IDENTITY_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
DEFAULT_PROCESS_TIMEOUT = 8.0
DEFAULT_PROCESS_TERM_GRACE = 0.5


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
JournalReader = Callable[[Path, str, str, str], RunJournalFrame]


def _default_summary_reader(path: Path, host_id: str, source_id: str) -> AgentRunSnapshot:
    return public_readers.read_agent_run_snapshot(path, host_id=host_id, source_id=source_id)


def _default_journal_reader(
    path: Path, run_id: str, host_id: str, source_id: str
) -> RunJournalFrame:
    return public_readers.RunJournalTailReader(
        path=path,
        run_id=run_id,
        host_id=host_id,
        source_id=source_id,
    ).snapshot()[0]


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
        journal_reader: JournalReader = _default_journal_reader,
        journal_base: Path | None = None,
        dispatch_sink_for_entry: Callable[[dict, Mapping[str, object]], Path] | None = None,
        process_timeout: float = DEFAULT_PROCESS_TIMEOUT,
        process_term_grace: float = DEFAULT_PROCESS_TERM_GRACE,
    ) -> None:
        self.cfg = cfg if cfg is not None else config.load()
        self.host_id = host_id
        self._summary_reader = summary_reader
        self._journal_reader = journal_reader
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

    def close(self) -> None:
        """Cancel production polling process trees; injected providers remain caller-owned."""

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
        request = StreamRequest(StreamScope.HIVE, hive=hive.identity)
        try:
            bead_state = self.provider.refresh(request)
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

        sink = self._dispatch_sink_for_entry(self.cfg, hive.entry)
        try:
            runtime_state = self._summary_reader(
                sink,
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

    def locate_run(self, run_id: str) -> tuple[ExactHive, Path]:
        # Reuse the writer's exact path-safe validation rather than maintaining another regex.
        candidates: list[tuple[ExactHive, Path]] = []
        for hive in self.registered_hives():
            try:
                path = run_journal.journal_path_for_hive(
                    hive.identity, run_id, base=self._journal_base
                )
            except ValueError as exc:
                raise OperatorSourceError(
                    "invalid_run_id",
                    "Run identity must be one path-safe outer run token.",
                    status_code=400,
                ) from exc
            if path.exists():
                candidates.append((hive, path))
        if not candidates:
            raise OperatorSourceError(
                "run_not_found", "The exact outer run was not found.", status_code=404
            )
        if len(candidates) > 1:
            raise OperatorSourceError(
                "ambiguous_run_id",
                "The outer run identity exists in more than one hive.",
                status_code=409,
            )
        hive, path = candidates[0]
        if path.is_symlink() or not path.is_file():
            raise OperatorSourceError(
                "invalid_run_source",
                "The outer run source is not a regular host-local journal.",
                status_code=409,
            )
        return hive, path

    def read_run(self, hive: ExactHive, path: Path, run_id: str) -> RunJournalFrame:
        try:
            frame = self._journal_reader(
                path,
                run_id,
                self.host_id,
                f"beadhive.run-journal:{self.host_id}:{hive.identity}:{run_id}",
            )
        except Exception as exc:
            raise OperatorSourceError(
                "activity_source_unavailable",
                "The authoritative run activity source is unavailable.",
                status_code=503,
                retryable=True,
            ) from exc
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
