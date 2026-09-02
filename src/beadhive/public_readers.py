"""Public, cancellation-safe readers for Beadhive's distinct observer sources.

Bead state, coarse agent-run summaries, and rich run journals have different authorities and
different revision domains.  This module composes their landed implementations without merging
those domains or manufacturing correlation that their writers do not carry.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from . import run_journal
from .agent_run_summary_reader import compute_freshness, read_from_sink
from .modules.state import (
    AgentRunSnapshot,
    AgentRunSummary,
    Coverage,
    Freshness,
    JournalFrameKind,
    JournalResyncReason,
    RunJournalFrame,
    StateStreamProvider,
    StreamFrame,
    StreamRequest,
    stream_frames,
)
from .modules.state import (
    HiveCorrelation as HiveCorrelation,
)
from .modules.state import (
    SummaryJournalCorrelation as SummaryJournalCorrelation,
)
from .state_stream_polling import get_polling_provider
from .state_stream_process import StreamProcessScope

_JOURNAL_IDENTITY_FIELDS = ("run_id", "hive", "bead", "driver", "provider", "manifest_digest")
_JOURNAL_REQUIRED_FIELDS = frozenset(
    {
        "version",
        "source_revision",
        "timestamp_ms",
        "run_id",
        "hive",
        "bead",
        "driver",
        "provider",
        "manifest_digest",
        "provider_continuation",
        "writer",
        "activity",
    }
)


class ProviderFactory(Protocol):
    def __call__(self, process_scope: StreamProcessScope) -> StateStreamProvider: ...


@dataclass
class BeadFrameReader:
    """Yield canonical state-stream frames while owning the backend process scope."""

    provider_factory: ProviderFactory
    process_scope_factory: Callable[[], StreamProcessScope] = StreamProcessScope

    def frames(self, request: StreamRequest) -> Iterator[StreamFrame]:
        with self.process_scope_factory() as processes:
            provider = self.provider_factory(processes)
            yield from stream_frames(provider, request)

    @classmethod
    def polling(cls, cfg: dict | None = None) -> BeadFrameReader:
        """Today's bd-backed adapter without exposing bd or process ownership to consumers."""

        return cls(lambda processes: get_polling_provider(cfg, process_scope=processes))


def _opaque_revision(kind: str, content: bytes) -> str:
    digest = hashlib.sha256(kind.encode("utf-8") + b"\0" + content).hexdigest()
    return f"opaque:{digest}"


def _validate_scope(host_id: str, source_id: str) -> None:
    if not host_id or not source_id:
        raise ValueError("host_id and source_id must be explicit non-empty strings")


def _line_coverage(content: bytes) -> tuple[Coverage, str | None]:
    if not content:
        return Coverage.PARTIAL, "source_empty"
    complete = content.endswith(b"\n")
    lines = content.splitlines() if complete else content.splitlines()[:-1]
    for line in lines:
        try:
            if not isinstance(json.loads(line), dict):
                return Coverage.PARTIAL, "invalid_complete_record"
        except (UnicodeDecodeError, ValueError):
            return Coverage.PARTIAL, "invalid_complete_record"
    if not complete:
        return Coverage.PARTIAL, "final_record_incomplete"
    return Coverage.COMPLETE, None


def read_agent_run_snapshot(
    sink: Path,
    *,
    host_id: str,
    source_id: str,
    copied: bool = False,
    summary_loader: Callable[[Path], list[AgentRunSummary]] = read_from_sink,
) -> AgentRunSnapshot:
    """Read summaries without treating a missing or copied sink as authoritative live state."""

    _validate_scope(host_id, source_id)
    try:
        content = sink.read_bytes()
    except FileNotFoundError:
        return AgentRunSnapshot(
            host_id=host_id,
            source_id=source_id,
            revision=_opaque_revision("missing", b""),
            summaries=(),
            coverage=Coverage.UNKNOWN,
            coverage_reason="source_missing",
            freshness=Freshness(detail="source missing; writer coverage unknown"),
        )
    except OSError:
        return AgentRunSnapshot(
            host_id=host_id,
            source_id=source_id,
            revision=_opaque_revision("unreadable", b""),
            summaries=(),
            coverage=Coverage.DEGRADED,
            coverage_reason="source_unreadable",
            freshness=Freshness(detail="source unreadable; writer coverage unknown"),
        )

    coverage, reason = _line_coverage(content)
    try:
        summaries = tuple(summary_loader(sink))
    except Exception:  # noqa: BLE001 - source failure becomes degradation, never false emptiness
        summaries = ()
        coverage, reason = Coverage.DEGRADED, "summary_projection_failed"
    freshness = compute_freshness(sink)
    if copied:
        freshness = replace(
            freshness,
            state="unknown",
            expires_at=None,
            detail="copied source; live writer freshness unknown",
        )
        summaries = tuple(replace(summary, freshness=freshness) for summary in summaries)
    return AgentRunSnapshot(
        host_id=host_id,
        source_id=source_id,
        revision=_opaque_revision("content", content),
        summaries=summaries,
        coverage=coverage,
        coverage_reason=reason,
        freshness=freshness,
    )


@dataclass
class StopToken:
    """Thread-safe cooperative cancellation with an interruptible poll wait."""

    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def stop(self) -> None:
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(max(0.0, timeout))


@dataclass(frozen=True)
class _JournalState:
    records: tuple[dict[str, object], ...]
    revisions: tuple[str, ...]
    coverage: Coverage
    coverage_reason: str | None
    freshness: Freshness
    fingerprint: str

    @property
    def last_revision(self) -> str | None:
        return self.revisions[-1] if self.revisions else None


def _journal_freshness(path: Path, *, copied: bool) -> Freshness:
    freshness = compute_freshness(path)
    if copied:
        return replace(
            freshness,
            state="unknown",
            expires_at=None,
            detail="copied source; live writer freshness unknown",
        )
    return freshness


def _inspect_journal(path: Path, run_id: str, *, copied: bool) -> _JournalState:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return _JournalState(
            (),
            (),
            Coverage.UNKNOWN,
            "source_missing",
            Freshness(detail="source missing; writer coverage unknown"),
            _opaque_revision("missing", b""),
        )
    except OSError:
        return _JournalState(
            (),
            (),
            Coverage.DEGRADED,
            "source_unreadable",
            Freshness(detail="source unreadable; writer coverage unknown"),
            _opaque_revision("unreadable", b""),
        )

    coverage = Coverage.COMPLETE
    reason = None

    def degrade(new_reason: str) -> None:
        nonlocal coverage, reason
        coverage = Coverage.DEGRADED
        reason = reason or new_reason

    if not content:
        coverage, reason = Coverage.PARTIAL, "source_empty"
    complete = content.endswith(b"\n")
    raw_lines = content.splitlines() if complete else content.splitlines()[:-1]
    if content and not complete:
        coverage, reason = Coverage.PARTIAL, "final_record_incomplete"

    records: list[dict[str, object]] = []
    revisions: list[str] = []
    identity: tuple[object, ...] | None = None
    continuation: object = None
    continuation_observed = False
    for raw in raw_lines:
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            degrade("invalid_complete_record")
            continue
        if not isinstance(record, dict) or not _JOURNAL_REQUIRED_FIELDS.issubset(record):
            degrade("invalid_complete_record")
            continue
        if set(record) - (_JOURNAL_REQUIRED_FIELDS | {"$schema"}):
            degrade("invalid_complete_record")
            continue
        revision = record.get("source_revision")
        if (
            record.get("version") != run_journal.VERSION
            or not isinstance(revision, str)
            or not revision
            or type(record.get("timestamp_ms")) is not int
            or not isinstance(record.get("activity"), dict)
            or not isinstance(record["activity"].get("kind"), str)
        ):
            degrade("invalid_complete_record")
            continue
        current_identity = tuple(record.get(field) for field in _JOURNAL_IDENTITY_FIELDS)
        bead = record.get("bead")
        current_continuation = record.get("provider_continuation")
        if (
            any(not isinstance(value, str) or not value for value in current_identity[0:2])
            or any(not isinstance(value, str) or not value for value in current_identity[3:])
            or (bead is not None and (not isinstance(bead, str) or not bead))
            or (
                current_continuation is not None
                and (not isinstance(current_continuation, str) or not current_continuation)
            )
            or record.get("writer") not in run_journal.WRITERS
        ):
            degrade("invalid_complete_record")
            continue
        try:
            run_journal.RunIdentity(
                hive=str(record["hive"]),
                bead=bead if isinstance(bead, str) else None,
                driver=str(record["driver"]),
                provider=str(record["provider"]),
                manifest_digest=str(record["manifest_digest"]),
            )
            run_journal._validate_activity(record["activity"])
        except (TypeError, ValueError):
            degrade("invalid_complete_record")
            continue
        if current_identity[0] != run_id:
            degrade("run_id_mismatch")
            continue
        if identity is None:
            identity = current_identity
        elif current_identity != identity:
            degrade("identity_drift")
            continue
        if revision in revisions:
            degrade("duplicate_source_revision")
            continue
        if current_continuation == run_id:
            degrade("provider_continuation_aliases_run_id")
            continue
        if current_continuation is not None:
            if continuation_observed and current_continuation != continuation:
                degrade("provider_continuation_drift")
                continue
            continuation = current_continuation
            continuation_observed = True
        elif continuation_observed:
            degrade("provider_continuation_drift")
            continue
        records.append(record)
        revisions.append(revision)
    return _JournalState(
        records=tuple(records),
        revisions=tuple(revisions),
        coverage=coverage,
        coverage_reason=reason,
        freshness=_journal_freshness(path, copied=copied),
        fingerprint=_opaque_revision("content", content),
    )


@dataclass
class RunJournalTailReader:
    """Live run-scoped tail with explicit reset and cooperative cancellation semantics."""

    path: Path
    run_id: str
    host_id: str
    source_id: str
    copied: bool = False
    poll_interval: float = 0.25

    def __post_init__(self) -> None:
        _validate_scope(self.host_id, self.source_id)
        if not self.run_id:
            raise ValueError("run_id must be an explicit non-empty outer attempt id")
        self.poll_interval = max(0.0, self.poll_interval)

    def _frame(
        self,
        state: _JournalState,
        kind: JournalFrameKind,
        records: tuple[dict[str, object], ...] = (),
        *,
        since_revision: str | None = None,
        resync_reason: JournalResyncReason | None = None,
    ) -> RunJournalFrame:
        return RunJournalFrame(
            frame=kind,
            host_id=self.host_id,
            source_id=self.source_id,
            run_id=self.run_id,
            source_revision=state.last_revision,
            since_revision=since_revision,
            records=records,
            coverage=state.coverage,
            coverage_reason=state.coverage_reason,
            freshness=state.freshness,
            resync_reason=resync_reason,
        )

    def frames(
        self,
        stop: StopToken,
        *,
        since_revision: str | None = None,
    ) -> Iterator[RunJournalFrame]:
        if stop.stopped:
            return
        state = _inspect_journal(self.path, self.run_id, copied=self.copied)
        if since_revision is None:
            yield self._frame(state, JournalFrameKind.SNAPSHOT, state.records)
        elif since_revision in state.revisions:
            index = state.revisions.index(since_revision) + 1
            yield self._frame(
                state,
                JournalFrameKind.DELTA,
                state.records[index:],
                since_revision=since_revision,
            )
        else:
            yield self._frame(
                state,
                JournalFrameKind.RESYNC,
                since_revision=since_revision,
                resync_reason=JournalResyncReason.UNKNOWN_REVISION,
            )
            yield self._frame(state, JournalFrameKind.SNAPSHOT, state.records)

        while not stop.wait(self.poll_interval):
            current = _inspect_journal(self.path, self.run_id, copied=self.copied)
            if current.fingerprint == state.fingerprint:
                continue
            previous_revision = state.last_revision
            if previous_revision is not None and previous_revision in current.revisions:
                index = current.revisions.index(previous_revision) + 1
                yield self._frame(
                    current,
                    JournalFrameKind.DELTA,
                    current.records[index:],
                    since_revision=previous_revision,
                )
            else:
                yield self._frame(
                    current,
                    JournalFrameKind.RESYNC,
                    since_revision=previous_revision,
                    resync_reason=JournalResyncReason.SOURCE_RESET,
                )
                yield self._frame(current, JournalFrameKind.SNAPSHOT, current.records)
            state = current

    def snapshot(self, *, since_revision: str | None = None) -> tuple[RunJournalFrame, ...]:
        """Return the finite initial handshake without entering the live poll loop.

        Descriptor callers need the same validation/cursor semantics as the public tail, but a
        source-discovery request must never leave a generator or polling thread behind.
        """

        stop = StopToken()
        frames = self.frames(stop, since_revision=since_revision)
        first = next(frames)
        out = [first]
        if first.frame is JournalFrameKind.RESYNC:
            out.append(next(frames))
        stop.stop()
        frames.close()
        return tuple(out)
