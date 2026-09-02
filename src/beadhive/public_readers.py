"""Public, cancellation-safe readers for Beadhive's distinct observer sources.

Bead state, coarse agent-run summaries, and rich run journals have different authorities and
different revision domains.  This module composes their landed implementations without merging
those domains or manufacturing correlation that their writers do not carry.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from . import registry, run_journal
from .agent_run_summary import AgentRunSummary, Freshness
from .agent_run_summary_reader import compute_freshness, read_from_sink
from .daemon_contract import encode_run_id
from .state_stream import StateStreamProvider, StreamFrame, StreamRequest, stream_frames
from .state_stream_polling import get_polling_provider
from .state_stream_process import StreamProcessScope

SUMMARY_JOURNAL_CORRELATION_REASON = (
    "dispatch summaries do not carry the journal outer run_id; session_id is a distinct "
    "seat-process identifier"
)
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
DEFAULT_MAX_JOURNAL_RECORDS = 1_000
DEFAULT_MAX_JOURNAL_RECORD_BYTES = 262_144
DEFAULT_MAX_JOURNAL_READ_BYTES = 16 * 1_048_576
DEFAULT_MAX_INVENTORY_ROOTS = 256
DEFAULT_MAX_INVENTORY_ENTRIES = 10_000
DEFAULT_MAX_INVENTORY_BYTES = 64 * 1_048_576

_INCOMPLETE_INVENTORY_REASONS = frozenset(
    {
        "inventory_limit_exceeded",
        "journal_root_collision",
        "partial_coverage",
        "source_empty",
        "source_missing",
        "source_unreadable",
    }
)
_INVENTORY_REASON_PRECEDENCE = {
    "source_unreadable": 0,
    "inventory_limit_exceeded": 1,
    "journal_root_collision": 2,
    "partial_coverage": 3,
    "source_missing": 4,
    "source_empty": 5,
    "invalid_inventory_entry": 7,
}


class Coverage(StrEnum):
    """Truthfulness of one source observation, separate from lifecycle authority."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class RunDirectoryError(ValueError):
    """Stable exact-lookup failure from the cross-hive run directory."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RunDirectoryEntry:
    """One exact outer run discovered from a regular public journal source."""

    hive_id: str
    run_id: str
    path: Path = field(repr=False)
    modified_at: float | None
    state: str = "available"
    reason_code: str | None = None
    coverage: Coverage = Coverage.COMPLETE
    coverage_reason: str | None = None
    device: int | None = field(default=None, repr=False)
    inode: int | None = field(default=None, repr=False)
    root_device: int | None = field(default=None, repr=False)
    root_inode: int | None = field(default=None, repr=False)
    observed_bytes: int = field(default=0, repr=False)


@dataclass(frozen=True)
class RunDirectoryInventory:
    """One host-wide identity domain for journals, separate from every hive stream cursor."""

    entries: tuple[RunDirectoryEntry, ...]
    coverage: Coverage
    coverage_reason: str | None
    reason_codes: tuple[str, ...] = ()
    observed_roots: int = 0
    observed_entries: int = 0
    retained_bytes: int = 0

    @property
    def exact_lookup_available(self) -> bool:
        reasons = self.reason_codes or ((self.coverage_reason,) if self.coverage_reason else ())
        return not bool(_INCOMPLETE_INVENTORY_REASONS.intersection(reasons))

    def resolve(self, run_id: str) -> RunDirectoryEntry:
        reasons = self.reason_codes or ((self.coverage_reason,) if self.coverage_reason else ())
        if "journal_root_collision" in reasons:
            raise RunDirectoryError("journal_root_collision")
        if _INCOMPLETE_INVENTORY_REASONS.intersection(reasons):
            # An unseen root could contain either the requested run or a duplicate of an
            # otherwise-visible run.  Neither absence nor uniqueness is provable.
            raise RunDirectoryError("run_directory_unavailable")
        matches = tuple(entry for entry in self.entries if entry.run_id == run_id)
        if len(matches) > 1:
            raise RunDirectoryError("ambiguous_run_id")
        if matches:
            entry = matches[0]
            if entry.state != "available":
                raise RunDirectoryError(entry.reason_code or "invalid_run_source")
            return entry
        if self.coverage in {Coverage.DEGRADED, Coverage.UNKNOWN}:
            raise RunDirectoryError("run_directory_unavailable")
        raise RunDirectoryError("run_not_found")


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _check_readable_directory(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not metadata.st_mode & 0o444
        or not metadata.st_mode & 0o111
    ):
        raise PermissionError(errno.EACCES, "directory is not readable")
    return metadata


def _open_directory_no_follow(path: Path) -> tuple[int, os.stat_result]:
    """Open every absolute path component without accepting a symlink."""

    absolute = path.absolute()
    current = os.open(os.sep, _directory_open_flags())
    try:
        metadata = _check_readable_directory(current)
        for component in absolute.parts[1:]:
            child = os.open(component, _directory_open_flags(), dir_fd=current)
            os.close(current)
            current = child
            metadata = _check_readable_directory(current)
        return current, metadata
    except BaseException:
        os.close(current)
        raise


def _invalid_run_entry(
    path: Path,
    *,
    hive_id: str,
    run_id: str,
    reason_code: str,
    coverage: Coverage = Coverage.PARTIAL,
    coverage_reason: str | None = None,
    observed_bytes: int = 0,
) -> RunDirectoryEntry:
    return RunDirectoryEntry(
        hive_id,
        run_id,
        path,
        None,
        state="invalid",
        reason_code=reason_code,
        coverage=coverage,
        coverage_reason=coverage_reason,
        observed_bytes=observed_bytes,
    )


def _run_directory_entry(
    root_descriptor: int,
    name: str,
    path: Path,
    *,
    hive_id: str,
    run_id: str,
    root_device: int,
    root_inode: int,
    max_records_per_read: int,
    max_record_bytes: int,
    max_read_bytes: int,
    inventory_read_budget: int,
) -> RunDirectoryEntry:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=root_descriptor,
        )
        metadata = os.fstat(descriptor)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        reason = (
            "invalid_run_source"
            if error.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO}
            else "run_source_unavailable"
        )
        return _invalid_run_entry(path, hive_id=hive_id, run_id=run_id, reason_code=reason)
    try:
        if not stat.S_ISREG(metadata.st_mode):
            return _invalid_run_entry(
                path, hive_id=hive_id, run_id=run_id, reason_code="invalid_run_source"
            )
        if metadata.st_nlink != 1:
            return _invalid_run_entry(
                path,
                hive_id=hive_id,
                run_id=run_id,
                reason_code="multiply_linked_run_source",
            )
        if not metadata.st_mode & 0o444:
            return _invalid_run_entry(
                path, hive_id=hive_id, run_id=run_id, reason_code="run_source_unavailable"
            )
        if metadata.st_size > inventory_read_budget:
            return _invalid_run_entry(
                path,
                hive_id=hive_id,
                run_id=run_id,
                reason_code="inventory_limit_exceeded",
                coverage=Coverage.PARTIAL,
                coverage_reason="inventory_limit_exceeded",
            )
        state, stable_metadata = _read_journal_descriptor_state(
            descriptor,
            run_id=run_id,
            expected_hive_id=hive_id,
            copied=False,
            max_records_per_read=max_records_per_read,
            max_record_bytes=max_record_bytes,
            max_read_bytes=min(max_read_bytes, inventory_read_budget),
        )
        if stable_metadata.st_size > inventory_read_budget:
            return _invalid_run_entry(
                path,
                hive_id=hive_id,
                run_id=run_id,
                reason_code="inventory_limit_exceeded",
                coverage=Coverage.PARTIAL,
                coverage_reason="inventory_limit_exceeded",
                observed_bytes=inventory_read_budget,
            )
        valid_prefix = bool(state.records)
        if state.coverage is Coverage.DEGRADED or not valid_prefix:
            reason = state.coverage_reason or "invalid_complete_record"
            return _invalid_run_entry(
                path,
                hive_id=hive_id,
                run_id=run_id,
                reason_code=("empty_run_source" if reason == "source_empty" else reason),
                coverage=state.coverage,
                coverage_reason=reason,
                observed_bytes=stable_metadata.st_size,
            )
        return RunDirectoryEntry(
            hive_id,
            run_id,
            path,
            stable_metadata.st_mtime,
            coverage=state.coverage,
            coverage_reason=state.coverage_reason,
            device=stable_metadata.st_dev,
            inode=stable_metadata.st_ino,
            root_device=root_device,
            root_inode=root_inode,
            observed_bytes=stable_metadata.st_size,
        )
    except RunJournalSourceChanged:
        return _invalid_run_entry(
            path,
            hive_id=hive_id,
            run_id=run_id,
            reason_code="run_source_unavailable",
            coverage=Coverage.DEGRADED,
            coverage_reason="source_changed_during_read",
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _ordered_inventory_reasons(reasons: set[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            reasons,
            key=lambda reason: (_INVENTORY_REASON_PRECEDENCE.get(reason, 6), reason),
        )
    )


def read_run_directory(
    roots: Iterable[tuple[str, Path]],
    *,
    max_records_per_read: int = DEFAULT_MAX_JOURNAL_RECORDS,
    max_record_bytes: int = DEFAULT_MAX_JOURNAL_RECORD_BYTES,
    max_read_bytes: int = DEFAULT_MAX_JOURNAL_READ_BYTES,
    max_inventory_roots: int = DEFAULT_MAX_INVENTORY_ROOTS,
    max_inventory_entries: int = DEFAULT_MAX_INVENTORY_ENTRIES,
    max_inventory_bytes: int = DEFAULT_MAX_INVENTORY_BYTES,
) -> RunDirectoryInventory:
    """Inventory exact public run filenames outside per-hive sequence domains.

    Missing roots are coverage gaps, not authoritative empty directories. Symlinks and special
    files remain explicit invalid candidates so an exact lookup cannot silently turn them into
    ``not found``. Contents are validated only to derive bounded source truth; journal records and
    private locators never enter the public inventory.
    """

    if min(max_inventory_roots, max_inventory_entries, max_inventory_bytes) < 1:
        raise ValueError("inventory bounds must be positive")

    entries: list[RunDirectoryEntry] = []
    readable_roots = 0
    missing_roots = 0
    unavailable_roots = 0
    empty_roots = 0
    unreadable_entries = False
    empty_entries = False
    generic_invalid_entries = False
    partial_reasons: set[str] = set()
    degraded_reasons: set[str] = set()
    root_collision = False
    inventory_limited = False
    root_owners: dict[Path, str] = {}
    observed_roots = 0
    observed_entries = 0
    retained_bytes = 0
    for hive_id, root in roots:
        if observed_roots >= max_inventory_roots:
            inventory_limited = True
            break
        observed_roots += 1
        root_fact_bytes = len(os.fsencode(hive_id)) + len(os.fsencode(root))
        if retained_bytes + root_fact_bytes > max_inventory_bytes:
            inventory_limited = True
            break
        retained_bytes += root_fact_bytes
        root_key = root.absolute()
        previous_owner = root_owners.setdefault(root_key, hive_id)
        if previous_owner != hive_id:
            root_collision = True
        root_descriptor: int | None = None
        try:
            root_descriptor, root_metadata = _open_directory_no_follow(root)
        except FileNotFoundError:
            missing_roots += 1
            continue
        except OSError:
            unavailable_roots += 1
            continue
        try:
            names: list[str] = []
            with os.scandir(root_descriptor) as iterator:
                for directory_entry in iterator:
                    if observed_entries >= max_inventory_entries:
                        inventory_limited = True
                        break
                    name = directory_entry.name
                    name_bytes = len(os.fsencode(name))
                    if retained_bytes + name_bytes > max_inventory_bytes:
                        inventory_limited = True
                        break
                    names.append(name)
                    observed_entries += 1
                    retained_bytes += name_bytes
            names.sort()
        except OSError:
            unavailable_roots += 1
            os.close(root_descriptor)
            continue
        root_entries: list[RunDirectoryEntry] = []
        root_is_empty = not names and not inventory_limited
        root_generic_invalid_entries = False
        root_unreadable_entries = False
        root_empty_entries = False
        try:
            for name in names:
                if not name.endswith(".jsonl"):
                    root_generic_invalid_entries = True
                    continue
                run_id = name.removesuffix(".jsonl")
                try:
                    encode_run_id(run_id)
                except ValueError:
                    root_generic_invalid_entries = True
                    continue
                remaining_bytes = max_inventory_bytes - retained_bytes
                if remaining_bytes < 1:
                    inventory_limited = True
                    break
                entry = _run_directory_entry(
                    root_descriptor,
                    name,
                    root / name,
                    hive_id=hive_id,
                    run_id=run_id,
                    root_device=root_metadata.st_dev,
                    root_inode=root_metadata.st_ino,
                    max_records_per_read=max_records_per_read,
                    max_record_bytes=max_record_bytes,
                    max_read_bytes=max_read_bytes,
                    inventory_read_budget=remaining_bytes,
                )
                retained_bytes += entry.observed_bytes
                inventory_limited = (
                    inventory_limited or entry.reason_code == "inventory_limit_exceeded"
                )
                root_generic_invalid_entries = root_generic_invalid_entries or (
                    entry.state != "available" and entry.coverage_reason is None
                )
                root_unreadable_entries = (
                    root_unreadable_entries or entry.reason_code == "run_source_unavailable"
                )
                root_empty_entries = root_empty_entries or entry.reason_code == "empty_run_source"
                root_entries.append(entry)
                if entry.coverage is Coverage.DEGRADED:
                    if entry.coverage_reason:
                        degraded_reasons.add(entry.coverage_reason)
                elif entry.coverage is Coverage.PARTIAL:
                    if entry.coverage_reason:
                        partial_reasons.add(entry.coverage_reason)
                if inventory_limited:
                    break
            check_descriptor, check_metadata = _open_directory_no_follow(root)
            try:
                root_stable = (root_metadata.st_dev, root_metadata.st_ino) == (
                    check_metadata.st_dev,
                    check_metadata.st_ino,
                )
            finally:
                os.close(check_descriptor)
        except OSError:
            root_stable = False
        finally:
            os.close(root_descriptor)
        if not root_stable:
            unavailable_roots += 1
            continue
        readable_roots += 1
        empty_roots += int(root_is_empty)
        generic_invalid_entries = generic_invalid_entries or root_generic_invalid_entries
        unreadable_entries = unreadable_entries or root_unreadable_entries
        empty_entries = empty_entries or root_empty_entries
        entries.extend(root_entries)
        if inventory_limited:
            break

    reasons: set[str] = set()
    if root_collision:
        reasons.add("journal_root_collision")
    if inventory_limited:
        reasons.add("inventory_limit_exceeded")
    if unavailable_roots or unreadable_entries:
        reasons.add("source_unreadable")
    reasons.update(degraded_reasons)
    reasons.update(partial_reasons)
    if empty_roots or empty_entries or (readable_roots and not entries and not inventory_limited):
        reasons.add("source_empty")
    if generic_invalid_entries:
        reasons.add("invalid_inventory_entry")
    if missing_roots:
        reasons.add("source_missing" if not readable_roots else "partial_coverage")
    elif not readable_roots and not unavailable_roots and not inventory_limited:
        reasons.add("source_missing")

    ordered_reasons = _ordered_inventory_reasons(reasons)
    reason = ordered_reasons[0] if ordered_reasons else None
    if unavailable_roots or unreadable_entries or degraded_reasons:
        coverage = Coverage.DEGRADED
    elif not readable_roots and "source_missing" in reasons and not inventory_limited:
        coverage = Coverage.UNKNOWN
    elif ordered_reasons:
        coverage = Coverage.PARTIAL
    else:
        coverage = Coverage.COMPLETE
    return RunDirectoryInventory(
        entries=tuple(sorted(entries, key=lambda item: (item.run_id, item.hive_id))),
        coverage=coverage,
        coverage_reason=reason,
        reason_codes=ordered_reasons,
        observed_roots=observed_roots,
        observed_entries=observed_entries,
        retained_bytes=retained_bytes,
    )


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
        return cls(registered_identity=registry.hive_key(exact), repo_slug=exact["repo"])

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


def _journal_freshness_from_mtime(mtime: float, *, copied: bool) -> Freshness:
    freshness = Freshness(
        state="unknown",
        as_of=mtime,
        detail=(
            "writer colocation unverified; sink last written "
            f"{max(time.time() - mtime, 0.0):.0f}s ago"
        ),
    )
    if copied:
        return replace(
            freshness,
            state="unknown",
            expires_at=None,
            detail="copied source; live writer freshness unknown",
        )
    return freshness


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


def _inspect_journal_content(
    content: bytes,
    run_id: str,
    *,
    expected_hive_id: str | None = None,
    freshness: Freshness,
) -> _JournalState:
    """Parse one immutable journal observation supplied by a trusted read boundary."""

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
        if expected_hive_id is not None and current_identity[1] != expected_hive_id:
            degrade("hive_identity_mismatch")
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
        freshness=freshness,
        fingerprint=_opaque_revision("content", content),
    )


class RunJournalSourceChanged(RuntimeError):
    """The opened journal ceased to be the exact source that was inventoried."""


def _bounded_journal_content(
    descriptor: int,
    *,
    max_records_per_read: int,
    max_record_bytes: int,
    max_read_bytes: int,
) -> tuple[bytes, str | None]:
    """Read a bounded JSONL prefix and report why the source was truncated."""

    if min(max_records_per_read, max_record_bytes, max_read_bytes) < 1:
        raise ValueError("journal read bounds must be positive")
    effective_read_bytes = min(
        max_read_bytes,
        max_records_per_read * (max_record_bytes + 1) + 1,
    )
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = bytearray()
    while len(content) <= effective_read_bytes:
        chunk = os.read(
            descriptor,
            min(64 * 1024, effective_read_bytes + 1 - len(content)),
        )
        if not chunk:
            break
        content.extend(chunk)
    if len(content) > effective_read_bytes:
        return bytes(content[:effective_read_bytes]), "source_byte_limit_exceeded"

    accepted: list[bytes] = []
    complete_records = 0
    raw_content = bytes(content)
    offset = 0
    while offset < len(raw_content):
        newline = raw_content.find(b"\n", offset)
        complete = newline >= 0
        end = newline + 1 if complete else len(raw_content)
        if end - offset - int(complete) > max_record_bytes:
            return b"".join(accepted), "record_too_large"
        if complete:
            if complete_records >= max_records_per_read:
                return b"".join(accepted), "record_limit_exceeded"
            complete_records += 1
        accepted.append(raw_content[offset:end])
        offset = end
    return b"".join(accepted), None


def _read_journal_descriptor_state(
    descriptor: int,
    *,
    run_id: str,
    expected_hive_id: str | None = None,
    copied: bool,
    max_records_per_read: int,
    max_record_bytes: int,
    max_read_bytes: int,
) -> tuple[_JournalState, os.stat_result]:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RunJournalSourceChanged("journal source is not a regular file")
    content, limit_reason = _bounded_journal_content(
        descriptor,
        max_records_per_read=max_records_per_read,
        max_record_bytes=max_record_bytes,
        max_read_bytes=max_read_bytes,
    )
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_nlink,
    ):
        raise RunJournalSourceChanged("journal source changed during read")
    state = _inspect_journal_content(
        content,
        run_id,
        expected_hive_id=expected_hive_id,
        freshness=_journal_freshness_from_mtime(after.st_mtime, copied=copied),
    )
    if limit_reason is not None and state.coverage is not Coverage.DEGRADED:
        state = replace(
            state,
            coverage=Coverage.PARTIAL,
            coverage_reason=limit_reason,
            fingerprint=_opaque_revision(limit_reason, content),
        )
    return state, after


def open_run_journal_descriptor(source: RunDirectoryEntry) -> int:
    """Open an inventoried journal without following a changed root or ancestor.

    The returned descriptor is owned by the caller. Every directory descriptor opened while
    revalidating the path is closed here on both success and failure.
    """

    if (
        source.device is None
        or source.inode is None
        or source.root_device is None
        or source.root_inode is None
    ):
        raise RunJournalSourceChanged("journal source identity is incomplete")
    root_descriptor: int | None = None
    descriptor: int | None = None
    try:
        root_descriptor, root_metadata = _open_directory_no_follow(source.path.parent)
        if (root_metadata.st_dev, root_metadata.st_ino) != (
            source.root_device,
            source.root_inode,
        ):
            raise RunJournalSourceChanged("journal root identity changed after discovery")
        descriptor = os.open(
            source.path.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=root_descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (source.device, source.inode)
            or metadata.st_nlink != 1
            or not metadata.st_mode & 0o444
        ):
            raise RunJournalSourceChanged("journal source identity changed after discovery")
        opened = descriptor
        descriptor = None
        return opened
    except RunJournalSourceChanged:
        raise
    except OSError as error:
        raise RunJournalSourceChanged("journal source path changed after discovery") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


def read_run_journal_descriptor(
    descriptor: int,
    *,
    run_id: str,
    host_id: str,
    source_id: str,
    expected_hive_id: str | None = None,
    copied: bool = False,
    max_records_per_read: int = DEFAULT_MAX_JOURNAL_RECORDS,
    max_record_bytes: int = DEFAULT_MAX_JOURNAL_RECORD_BYTES,
    max_read_bytes: int = DEFAULT_MAX_JOURNAL_READ_BYTES,
) -> RunJournalFrame:
    """Read a finite journal snapshot from one already-validated file descriptor."""

    _validate_scope(host_id, source_id)
    state, _metadata = _read_journal_descriptor_state(
        descriptor,
        run_id=run_id,
        expected_hive_id=expected_hive_id,
        copied=copied,
        max_records_per_read=max_records_per_read,
        max_record_bytes=max_record_bytes,
        max_read_bytes=max_read_bytes,
    )
    return RunJournalFrame(
        frame=JournalFrameKind.SNAPSHOT,
        host_id=host_id,
        source_id=source_id,
        run_id=run_id,
        source_revision=state.last_revision,
        since_revision=None,
        records=state.records,
        coverage=state.coverage,
        coverage_reason=state.coverage_reason,
        freshness=state.freshness,
    )


def _inspect_journal(
    path: Path,
    run_id: str,
    *,
    copied: bool,
) -> _JournalState:
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

    return _inspect_journal_content(
        content,
        run_id,
        freshness=_journal_freshness(path, copied=copied),
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
