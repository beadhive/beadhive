"""Immutable query models shared by operator-facing transport adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .activity import AgentRunSummary, Freshness

SUMMARY_JOURNAL_CORRELATION_REASON = (
    "dispatch summaries do not carry the journal outer run_id; session_id is a distinct "
    "seat-process identifier"
)


class Coverage(StrEnum):
    """Truthfulness of one source observation, separate from lifecycle authority."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class SummaryJournalCorrelation(StrEnum):
    """The only truthful summary-to-journal correlation state until writer truth expands."""

    UNAVAILABLE = "unavailable"


class JournalFrameKind(StrEnum):
    SNAPSHOT = "snapshot"
    DELTA = "delta"
    RESYNC = "resync"


class JournalResyncReason(StrEnum):
    UNKNOWN_REVISION = "unknown_revision"
    SOURCE_RESET = "source_reset"


@dataclass(frozen=True)
class HiveCorrelation:
    """Explicit bridge between stream repo slugs and journal registered identities."""

    registered_identity: str
    repo_slug: str

    def __post_init__(self) -> None:
        if not self.registered_identity or not self.repo_slug:
            raise ValueError("hive correlation requires full identity and repo slug")

    @classmethod
    def from_registry_entry(cls, entry: Mapping[str, object]) -> HiveCorrelation:
        """Build only from a complete registry row; prefixes are deliberately ignored."""

        required = ("provider", "org", "repo")
        if any(not isinstance(entry.get(key), str) or not entry.get(key) for key in required):
            raise ValueError("hive correlation requires provider, org, and repo registry fields")
        exact = {key: str(entry[key]) for key in required}
        return cls(
            registered_identity="/".join(exact[key] for key in required),
            repo_slug=exact["repo"],
        )

    def matches(self, *, stream_hive: str, journal_hive: str) -> bool:
        return stream_hive == self.repo_slug and journal_hive == self.registered_identity

    def matches_bead(
        self,
        *,
        stream_hive: str,
        stream_bead: str,
        journal_hive: str,
        journal_bead: object,
    ) -> bool:
        """Exact bead join; null-bead journals remain run-scoped and never match."""

        return (
            isinstance(journal_bead, str)
            and bool(journal_bead)
            and stream_bead == journal_bead
            and self.matches(stream_hive=stream_hive, journal_hive=journal_hive)
        )


@dataclass(frozen=True)
class AgentRunSnapshot:
    """One host/source-scoped observation of the coarse dispatch summary sink."""

    host_id: str
    source_id: str
    revision: str
    summaries: tuple[AgentRunSummary, ...]
    coverage: Coverage
    coverage_reason: str | None
    freshness: Freshness
    journal_correlation: SummaryJournalCorrelation = SummaryJournalCorrelation.UNAVAILABLE
    journal_correlation_reason: str = SUMMARY_JOURNAL_CORRELATION_REASON


@dataclass(frozen=True)
class RunJournalFrame:
    """A run-scoped journal observation; ``source_revision`` is never a host cursor."""

    frame: JournalFrameKind
    host_id: str
    source_id: str
    run_id: str
    source_revision: str | None
    since_revision: str | None
    records: tuple[dict[str, object], ...]
    coverage: Coverage
    coverage_reason: str | None
    freshness: Freshness
    resync_reason: JournalResyncReason | None = None
