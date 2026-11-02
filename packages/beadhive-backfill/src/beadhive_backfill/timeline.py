"""The lifecycle timeline pass: AGF conventions over the history readers (``bh backfill timeline``).

Pass #2 of the hive apply envelope. The neutral half (the bd ``events`` table as BeadLog history
events) lives in :mod:`beadlog_sources.bd_timeline`; this module is the BeadHive-specific half:
what the AGF conventions *mean*. From one bulk read of each source it derives the lifecycle facts
bd lacks and proposes them as ``metadata.lifecycle.*`` keys, each with its confidence:

=====================  ======================================================  =========  ======
key                    source                                                  provenance  tier
=====================  ======================================================  =========  ======
``claimed_at``         the first bd ``claimed`` event                          observed   high
``committed_at``       author date of the first ``metadata.git.commits`` sha   derived    medium
``last_committed_at``  author date of the last of them                         derived    medium
``submitted_at``       the first ``review:pending`` label added                observed   high
``reviewed_at``        the first review outcome after it (``review:approved``  observed   high
                       / ``review:changes-requested`` added, or
                       ``review:pending`` removed)                                        medium
``merged_at``          committer date of the first-parent ``chore(merge)``     derived    high
                       land commit (latest land if several)                               medium
``reopened_at``        the last bd ``reopened`` event                          observed   high
``reopen_count``       how many ``reopened`` events there are                  derived    high
``run_started_at``     a run journal's ``process.spawned`` (else
                       ``run.created``)                                        observed   medium
``run_ended_at``       a run journal's last ``process.harvested``              observed   medium
=====================  ======================================================  =========  ======

Every key ``K`` has a companion ``K_confidence`` (``high`` / ``medium`` / ``low``) so the
confidence travels with the value in bd, which keeps only current state.

**Two times, never one.** ``occurred_at`` is when the fact happened; ``recorded_at`` is when the
source recorded it. A bd event is recorded as it happens (both its row time); a git fact happened
at the author date and was recorded at the committer date; a land happened at its commit's
committer date and was *recorded* by bd closing the bead afterwards — ``merged_at`` carries the
land's committer date as ``occurred_at`` and the bd ``closed`` event's time as ``recorded_at``,
so the ordering ``merged occurred_at < closed recorded_at`` is a property of the data, visible in
the event, and not an assumption. When no ``closed`` event follows the land, ``recorded_at`` is
null.

**Only facts, only events.** Closes and reopens come from bd events only; a status flip visible
in snapshots alone (store revert artifacts) is never a reopen. Revert artifacts in the table are
suppressed by the neutral conversion (:func:`beadlog_sources.suppress_artifacts`). Refresh merges
(``chore(merge): refresh ...``, ``Merge branch 'main' into ...``) are never lands
(:mod:`beadhive_backfill.merges`).

**Idempotent.** Event ids hash the identity members only, and every input to a fact is a source
that does not change when the pass applies (the apply adds ``updated`` rows to the events table,
which no fact reads), so a second dry run re-derives the same ids; once applied they are in the
store's ``metadata.bdlg`` and the baseline re-emits them, and reconcile finds nothing to do.

Nothing here imports ``beadhive``: the readers arrive through :class:`TimelineReaders`
(:class:`PathReaders` for a checkout on disk) and the engine ports.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    EventKind,
    Evidence,
    check_timestamp,
    fold,
    instant,
    mint_event,
    sort_events,
)
from beadlog_sources import (
    HISTORY_CLAIMED,
    HISTORY_CLOSED,
    HISTORY_LABEL_ADDED,
    HISTORY_LABEL_REMOVED,
    HISTORY_REOPENED,
    CommitDates,
    GitReadError,
    HistoryEvent,
    HistoryEvents,
    HistoryRead,
    JudgmentPort,
    commit_dates,
    history_events,
    read_history,
    subprocess_runner,
    suppress_artifacts,
)

from ._errors import BackfillError
from .incremental import (
    INCREMENTAL_KEY,
    MODE_FULL,
    MODE_INCREMENTAL,
    MODE_SCOPED,
    IncrementalReaders,
    PassCursor,
    incremental_info,
)
from .merges import DEFAULT_REF, LandKind, LandMerge, MergeScan, read_land_merges
from .run_journals import RunJournalRead, RunRecord, read_run_journals

PASS_NAME = "timeline"
PASS_SCHEMA_VERSION = "1"
LIFECYCLE_PREFIX = "metadata.lifecycle."
CONFIDENCE_SUFFIX = "_confidence"
SOURCE_BD = "bd"
SOURCE_GIT = "git"
SOURCE_JOURNAL = "run-journal"
REVIEW_PENDING = "review:pending"
REVIEW_OUTCOMES = frozenset({"review:approved", "review:changes-requested"})
APPROVED = "review:approved"
BATCH_LABEL = "batch:"
GIT_COMMITS_FIELD = "metadata.git.commits"

TIMESTAMP_FACTS = (
    "claimed_at",
    "committed_at",
    "last_committed_at",
    "submitted_at",
    "reviewed_at",
    "merged_at",
    "reopened_at",
    "run_started_at",
    "run_ended_at",
)
COUNT_FACTS = ("reopen_count",)
FACTS = (*TIMESTAMP_FACTS, *COUNT_FACTS)
# The numeric confidence an event carries for each tier (not identity; the tier string is what
# bd keeps in `<fact>_confidence`).
TIERS: Mapping[str, float] = {"high": 0.9, "medium": 0.7, "low": 0.4}


def fact_field(fact: str) -> str:
    """The BeadLog field of a lifecycle fact: ``metadata.lifecycle.<fact>``."""
    return f"{LIFECYCLE_PREFIX}{fact}"


def confidence_field(fact: str) -> str:
    """The field holding a fact's confidence tier."""
    return f"{LIFECYCLE_PREFIX}{fact}{CONFIDENCE_SUFFIX}"


LIFECYCLE_FIELDS = frozenset(
    {*(fact_field(fact) for fact in FACTS), *(confidence_field(fact) for fact in FACTS)}
)
# Facets whose change since planning invalidates a plan: the bead exists, is in the same phase
# (merged/closed ordering reads it), and every lifecycle key — so the post-apply projection
# proves the apply wrote exactly the planned values.
INVALIDATING_FIELDS = frozenset(
    {"exists", "phase", *(f"fields.{name}" for name in LIFECYCLE_FIELDS)}
)


# -- the source readers ----------------------------------------------------------------------


@runtime_checkable
class TimelineReaders(Protocol):
    """The four bulk reads the pass needs; each costs a fixed number of store calls, never one
    per bead. Tests inject fakes; :class:`PathReaders` reads a checkout."""

    def history(self) -> HistoryRead:
        """The bd ``events`` table (one ``SELECT``)."""
        ...

    def lands(self) -> MergeScan:
        """The first-parent land commits of the integration branch (one ``git log``)."""
        ...

    def commit_dates(self, shas: Sequence[str]) -> CommitDates:
        """Author and committer dates of ``shas`` (one ``git log`` per chunk)."""
        ...

    def run_journals(self) -> RunJournalRead:
        """The optional run journals (no store call; a missing directory is empty)."""
        ...


@dataclass(frozen=True)
class PathReaders:
    """:class:`TimelineReaders` over a hive checkout: ``bd -C root sql``, ``git -C root log`` and
    an optional run-journal directory (the caller resolves it; this package never imports
    ``beadhive``)."""

    root: Path
    ref: str = DEFAULT_REF
    journals: Path | None = None
    bd_binary: str = "bd"
    git_binary: str = "git"
    timeout: float | None = 300.0

    def history(self) -> HistoryRead:
        return read_history(self.root, binary=self.bd_binary, timeout=self.timeout)

    def lands(self) -> MergeScan:
        return read_land_merges(self.root, self.ref, binary=self.git_binary, timeout=self.timeout)

    def commit_dates(self, shas: Sequence[str]) -> CommitDates:
        return commit_dates(shas, root=self.root, binary=self.git_binary, timeout=self.timeout)

    def run_journals(self) -> RunJournalRead:
        return read_run_journals(self.journals)

    # -- the narrow reads of an incremental run (IncrementalReaders) --------------------------

    def _git(self, *args: str) -> str:
        run = subprocess_runner(self.timeout, GitReadError, "git")
        return run([self.git_binary, "-C", str(self.root), *args])

    def head(self) -> str | None:
        try:
            out = self._git("rev-parse", "--verify", "--quiet", f"{self.ref}^{{commit}}")
        except GitReadError:
            return None
        return out.strip() or None

    def reaches(self, sha: str) -> bool:
        try:  # exit 0: an ancestor of (or equal to) the branch; 1 or an unknown sha: not
            self._git("merge-base", "--is-ancestor", sha, self.ref)
        except GitReadError:
            return False
        return True

    def history_since(self, at: str) -> HistoryRead:
        return read_history(self.root, binary=self.bd_binary, timeout=self.timeout, since=at)

    def history_of(self, ids: Sequence[str]) -> HistoryRead:
        return read_history(self.root, binary=self.bd_binary, timeout=self.timeout, issue_ids=ids)

    def lands_since(self, sha: str) -> MergeScan:
        return read_land_merges(
            self.root, f"{sha}..{self.ref}", binary=self.git_binary, timeout=self.timeout
        )


# -- facts -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """One derived lifecycle fact of a bead, ready to become two ``set`` events (the value and
    its confidence tier)."""

    name: str
    value: str | int
    occurred_at: str
    recorded_at: str | None
    provenance: str
    tier: str
    system: str
    ref: str
    actor: str | None = None
    evidence: tuple[Evidence, ...] = ()

    @property
    def key(self) -> str:
        return fact_field(self.name)


def fact_events(bead: BeadRef, fact: Fact, *, extracted_at: str) -> tuple[BeadEvent, BeadEvent]:
    """The value ``set`` and the confidence ``set`` of a fact (same times and source)."""
    confidence = TIERS[fact.tier]

    def mint(field_name: str, value: str | int, ref: str) -> BeadEvent:
        return mint_event(
            str(EventKind.SET),
            bead,
            {"field": field_name, "value": value},
            occurred_at=fact.occurred_at,
            recorded_at=fact.recorded_at,
            extracted_at=extracted_at,
            source=(fact.system, ref),
            provenance=fact.provenance,
            confidence=confidence,
            actor=fact.actor,
            evidence=fact.evidence,
        )

    return (
        mint(fact.key, fact.value, fact.ref),
        mint(confidence_field(fact.name), fact.tier, f"{fact.ref}#confidence"),
    )


@dataclass
class Timeline:
    """The derived timeline of a hive: per-bead facts, the proposed lifecycle events, and the
    neutral history events (when requested). ``notes`` counts what the sources did not explain."""

    facts: dict[BeadRef, tuple[Fact, ...]] = field(default_factory=dict)
    events: dict[BeadRef, tuple[BeadEvent, ...]] = field(default_factory=dict)
    history: HistoryEvents | None = None
    notes: Counter[str] = field(default_factory=Counter)

    def lifecycle_events(self) -> Iterator[BeadEvent]:
        """Every proposed lifecycle event, beads in order, each bead's in key order."""
        for bead in sorted(self.events):
            yield from sort_events(self.events[bead])

    def of(self, bead: BeadRef) -> tuple[BeadEvent, ...]:
        """One bead's full timeline in total-order key order: its history events and its derived
        lifecycle events."""
        mine = [
            event for event in (self.history.events if self.history else ()) if event.bead == bead
        ]
        return tuple(sort_events([*mine, *self.events.get(bead, ())]))


def _commit_shas(state: BeadState) -> tuple[str, ...]:
    raw = state.fields.get(GIT_COMMITS_FIELD)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(item for item in raw if isinstance(item, str) and item))


def _bd_fact(
    name: str,
    event: HistoryEvent,
    tier: str,
    *,
    value: str | int | None = None,
    note: str | None = None,
    provenance: str = "observed",
) -> Fact:
    return Fact(
        name,
        event.at if value is None else value,
        event.at,
        event.at,
        provenance,
        tier,
        SOURCE_BD,
        f"bd:events/{event.row_id}",
        actor=event.actor,
        evidence=(Evidence("bd-event", f"events/{event.row_id}", note),) if note else (),
    )


def _land_tier(land: LandMerge, *, by_group: bool) -> str:
    return "medium" if land.legacy or by_group else "high"


def _land_fact(
    land: LandMerge, closes: Sequence[HistoryEvent], *, by_group: bool, count: int
) -> Fact:
    at = [close for close in closes if instant(close.at) >= instant(land.committed_at)]
    closed = min(at, key=lambda event: event.sort_key) if at else None
    note = f"{land.kind} land {land.subject!r}: committer date of {land.sha}"
    if by_group:
        note += " (matched through the bead's batch label)"
    if count > 1:
        note += f"; {count} lands found, the latest is used"
    evidence = [Evidence("git-commit", land.sha, note)]
    if closed is not None:
        evidence.append(
            Evidence("bd-event", f"events/{closed.row_id}", "closed: bd recorded the merge here")
        )
    else:
        evidence.append(
            Evidence("note", "no-closed-event", "no bd closed event at or after the land")
        )
    return Fact(
        "merged_at",
        land.committed_at,
        land.committed_at,
        closed.at if closed is not None else None,
        "derived",
        _land_tier(land, by_group=by_group),
        SOURCE_GIT,
        f"git:{land.sha}",
        evidence=tuple(evidence),
    )


def _review_fact(rows: Sequence[HistoryEvent]) -> tuple[Fact | None, Fact | None]:
    submitted = next(
        (e for e in rows if e.kind == HISTORY_LABEL_ADDED and e.label == REVIEW_PENDING), None
    )
    if submitted is None:
        return None, None
    outcome = next(
        (
            e
            for e in rows
            if instant(e.at) >= instant(submitted.at)
            and e.row_id != submitted.row_id
            and (
                (e.kind == HISTORY_LABEL_ADDED and e.label in REVIEW_OUTCOMES)
                or (e.kind == HISTORY_LABEL_REMOVED and e.label == REVIEW_PENDING)
            )
        ),
        None,
    )
    reviewed = None
    if outcome is not None:
        explicit = outcome.kind == HISTORY_LABEL_ADDED and outcome.label == APPROVED
        what = f"{outcome.kind} {outcome.label}"
        reviewed = _bd_fact(
            "reviewed_at", outcome, "high" if explicit else "medium", note=f"convention: {what}"
        )
    return _bd_fact(
        "submitted_at", submitted, "high", note=f"convention: {REVIEW_PENDING}"
    ), reviewed


def _journal_facts(records: Sequence[RunRecord]) -> list[Fact]:
    def fact(name: str, record: RunRecord) -> Fact:
        return Fact(
            name,
            record.at,
            record.at,
            record.at,
            "observed",
            "medium",
            SOURCE_JOURNAL,
            f"run-journal:{record.file}#{record.source_revision}",
            evidence=(
                Evidence("run-journal", record.file, f"{record.kind} of run {record.run_id}"),
            ),
        )

    spawned = [r for r in records if r.kind == "process.spawned"] or [
        r for r in records if r.kind == "run.created"
    ]
    harvested = [r for r in records if r.kind == "process.harvested"]
    out = []
    if spawned:
        out.append(fact("run_started_at", min(spawned, key=lambda r: (instant(r.at), r.run_id))))
    if harvested:
        out.append(fact("run_ended_at", max(harvested, key=lambda r: (instant(r.at), r.run_id))))
    return out


def build_timeline(
    baseline: Sequence[BeadEvent],
    readers: TimelineReaders,
    *,
    extracted_at: str,
    include_history: bool = False,
) -> Timeline:
    """Derive every lifecycle fact of the hive described by ``baseline`` from one bulk read of
    each source (module doc). ``include_history`` also converts the events table to neutral
    BeadLog history events (:attr:`Timeline.history`), for the full per-bead timeline."""
    baseline = tuple(baseline)
    states = {
        bead: state
        for bead, state in fold(baseline).items()
        if state.exists and not state.deleted and not state.erased
    }
    timeline = Timeline()
    read = readers.history()
    for reason, count in read.skipped.items():
        timeline.notes[f"history_skipped:{reason}"] += count
    ids = {bead.id for bead in states}
    usable, dropped = suppress_artifacts(event for event in read.events if event.bead in ids)
    timeline.notes.update({f"suppressed:{reason}": n for reason, n in dropped.items()})
    timeline.notes["history_rows_of_unknown_beads"] = sum(
        1 for event in read.events if event.bead not in ids
    )
    by_bead: dict[str, list[HistoryEvent]] = {}
    for event in usable:
        by_bead.setdefault(event.bead, []).append(event)

    scan = readers.lands()
    timeline.notes.update({f"lands_skipped:{reason}": n for reason, n in scan.skipped.items()})
    by_target: dict[str, list[LandMerge]] = {}
    for land in scan.merges:
        by_target.setdefault(land.target, []).append(land)
    matched: set[str] = set()

    shas = {bead: _commit_shas(state) for bead, state in states.items()}
    wanted = sorted({sha for items in shas.values() for sha in items})
    dates = readers.commit_dates(wanted) if wanted else CommitDates()
    timeline.notes["commits_missing"] = len(dates.missing)
    timeline.notes["commits_invalid"] = len(dates.invalid)
    journals = readers.run_journals()
    timeline.notes.update(
        {f"journal_skipped:{reason}": n for reason, n in journals.skipped.items()}
    )
    records: dict[str, list[RunRecord]] = {}
    for record in journals.records:
        if record.bead is not None:
            records.setdefault(record.bead, []).append(record)

    for bead in sorted(states):
        state = states[bead]
        rows = by_bead.get(bead.id, [])
        facts: list[Fact] = []
        claim = next((e for e in rows if e.kind == HISTORY_CLAIMED), None)
        if claim is not None:
            facts.append(_bd_fact("claimed_at", claim, "high"))
        resolved = [dates.dates[sha] for sha in shas[bead] if sha in dates.dates]
        if resolved:
            first = min(resolved, key=lambda d: (instant(d.author_date), d.sha))
            last = max(resolved, key=lambda d: (instant(d.author_date), d.sha))
            for name, date in (("committed_at", first), ("last_committed_at", last)):
                facts.append(
                    Fact(
                        name,
                        date.author_date,
                        date.author_date,
                        date.committer_date,
                        "derived",
                        "medium",
                        SOURCE_GIT,
                        f"git:{date.sha}",
                        evidence=(
                            Evidence(
                                "git-commit",
                                date.sha,
                                f"author date of a commit in {GIT_COMMITS_FIELD}",
                            ),
                        ),
                    )
                )
        submitted, reviewed = _review_fact(rows)
        facts += [fact for fact in (submitted, reviewed) if fact is not None]
        lands = [(land, False) for land in by_target.get(bead.id, ())]
        for label in state.labels:
            if label.startswith(BATCH_LABEL):
                group = label[len(BATCH_LABEL) :]
                lands += [
                    (land, True)
                    for land in by_target.get(group, ())
                    if land.kind is LandKind.BATCH and land.target != bead.id
                ]
        if lands:
            matched.update(land.sha for land, _ in lands)
            land, by_group = max(
                lands, key=lambda item: (instant(item[0].committed_at), item[0].sha)
            )
            closes = [e for e in rows if e.kind == HISTORY_CLOSED]
            facts.append(_land_fact(land, closes, by_group=by_group, count=len(lands)))
        reopened = [e for e in rows if e.kind == HISTORY_REOPENED]
        if reopened:
            facts.append(_bd_fact("reopened_at", reopened[-1], "high"))
            facts.append(
                _bd_fact(
                    "reopen_count",
                    reopened[-1],
                    "high",
                    value=len(reopened),
                    provenance="derived",
                    note=f"{len(reopened)} reopened event(s)",
                )
            )
        facts += _journal_facts(records.get(bead.id, ()))
        if not facts:
            continue
        timeline.facts[bead] = tuple(facts)
        timeline.events[bead] = tuple(
            event for fact in facts for event in fact_events(bead, fact, extracted_at=extracted_at)
        )
    timeline.notes["lands_unmatched"] = sum(1 for land in scan.merges if land.sha not in matched)
    if include_history:
        group = next(iter(states)).group if states else ""
        timeline.history = history_events(read, baseline, group=group, extracted_at=extracted_at)
    return timeline


# -- the pass --------------------------------------------------------------------------------


class _ScopedReaders:
    """:class:`TimelineReaders` narrowed to the beads of an incremental run: their ``events``
    rows only (one chunked ``SELECT`` when the readers can, else a filter of the full read). The
    land log, commit dates and journals are shared with the full run, so a bead's facts are the
    ones the full run derives. With ``ids=None`` it only remembers the history it passed on."""

    def __init__(self, inner: TimelineReaders, ids: frozenset[str] | None) -> None:
        self.inner = inner
        self.ids = ids
        self.read: HistoryRead | None = None

    def history(self) -> HistoryRead:
        if self.ids is None:
            self.read = self.inner.history()
        elif isinstance(self.inner, IncrementalReaders):
            self.read = self.inner.history_of(sorted(self.ids))
        else:
            whole = self.inner.history()
            self.read = HistoryRead(
                tuple(event for event in whole.events if event.bead in self.ids),
                whole.skipped,
                whole.unknown_types,
                whole.rows,
            )
        return self.read

    def lands(self) -> MergeScan:
        return self.inner.lands()

    def commit_dates(self, shas: Sequence[str]) -> CommitDates:
        return self.inner.commit_dates(shas)

    def run_journals(self) -> RunJournalRead:
        return self.inner.run_journals()


def _newest(events: Sequence[HistoryEvent]) -> HistoryEvent | None:
    return max(events, key=lambda event: (instant(event.at), event.row_id), default=None)


class TimelinePass:
    """The lifecycle timeline as an engine pass (module doc): proposes ``set`` events for the
    ``metadata.lifecycle.*`` keys and nothing else.

    DRIFT is applicable: every bead exists already, so its facts are folded-state changes.

    **Incremental** (``incremental=True``, ``bh backfill timeline --since-cursor``): instead of
    every bead, the pass derives the facts of the beads that *moved* since ``cursor``: those with
    an ``events`` row at or after ``cursor.events_at`` and the targets of the lands after
    ``cursor.land_sha`` (a batch land: the beads carrying its ``batch:`` label), plus
    ``extra_scope`` (the beads a merge just closed). A moved bead's facts come from the same
    sources and rules as a full run, so the proposal is exactly what a full run would add for
    those beads; every other bead is simply not looked at. With no usable cursor the pass runs in
    full (a corrupt or stale one adds a warning; ``full_ok=False`` refuses instead, for triggers
    that must stay small), unless ``extra_scope`` alone names the beads
    (``mode=scoped``: nothing else is read and no cursor moves). ``scope`` freezes the beads
    explicitly, which is how an applied plan re-derives exactly what it planned.

    The run records itself in the report's ``incremental`` section: the mode, the scope, the
    cursor read and the one :func:`cursor_to_record` writes once the plan is applied.
    """

    name = PASS_NAME
    schema_version = PASS_SCHEMA_VERSION
    invalidating_fields = INVALIDATING_FIELDS
    drift_applicable = True

    def __init__(
        self,
        readers: TimelineReaders,
        *,
        incremental: bool = False,
        cursor: PassCursor | None = None,
        extra_scope: Sequence[str] = (),
        scope: Sequence[str] | None = None,
        full_ok: bool = True,
    ) -> None:
        self.readers = readers
        self.full_ok = full_ok
        self.incremental = bool(incremental or scope is not None)
        self.cursor = cursor
        self.extra_scope = tuple(extra_scope)
        self.scope = None if scope is None else frozenset(scope)
        self.info: dict[str, Any] | None = None
        self._notes: Counter[str] = Counter()

    @classmethod
    def for_plan(cls, readers: TimelineReaders, report: Mapping[str, Any]) -> TimelinePass:
        """The pass that applies a saved plan: an incremental plan re-derives its recorded scope
        (so the post-apply second dry run is as narrow as the plan), any other plan runs in full."""
        info = report.get(INCREMENTAL_KEY)
        scope = info.get("scope") if isinstance(info, Mapping) else None
        if isinstance(scope, list):
            return cls(readers, incremental=True, scope=[str(item) for item in scope])
        return cls(readers)

    def exclusion(self, state: BeadState) -> str | None:
        if not state.exists or state.deleted or state.erased:
            return "absent"
        return None

    def timeline(self, baseline: Sequence[BeadEvent], *, extracted_at: str) -> Timeline:
        """The full timeline, history events included (for a report or a test)."""
        return build_timeline(
            baseline, self.readers, extracted_at=extracted_at, include_history=True
        )

    # -- the incremental run ---------------------------------------------------------------

    def _usable(self, narrow: bool) -> tuple[bool, str | None]:
        """Whether the cursor can bound this run, and the warning when it is the reason not."""
        cursor = self.cursor
        if cursor is None:
            return False, None
        if not narrow:
            return False, "the readers cannot make incremental reads"
        if cursor.events_at is None or cursor.land_sha is None:
            return False, "the timeline cursor is incomplete"
        assert isinstance(self.readers, IncrementalReaders)
        if not self.readers.reaches(cursor.land_sha):
            return False, f"cursor commit {cursor.land_sha[:12]} is not in the branch history"
        return True, None

    def _moved(
        self, baseline: Sequence[BeadEvent], cursor: PassCursor
    ) -> tuple[frozenset[str], HistoryEvent | None]:
        """The beads that moved since ``cursor`` and the newest ``events`` row it read."""
        assert isinstance(self.readers, IncrementalReaders)
        assert cursor.events_at is not None and cursor.land_sha is not None
        floor = instant(cursor.events_at)
        rows = [
            event
            for event in self.readers.history_since(cursor.events_at).events
            if instant(event.at) >= floor
        ]
        moved = {event.bead for event in rows}
        batches: dict[str, list[str]] = {}
        for bead, state in fold(baseline).items():
            for label in state.labels:
                if label.startswith(BATCH_LABEL):
                    batches.setdefault(label[len(BATCH_LABEL) :], []).append(bead.id)
        for land in self.readers.lands_since(cursor.land_sha).merges:
            moved.add(land.target)
            if land.kind is LandKind.BATCH:
                moved.update(batches.get(land.target, ()))
        return frozenset(moved), _newest(rows)

    def _incremental(self, baseline: Sequence[BeadEvent], extracted_at: str) -> Timeline:
        narrow = isinstance(self.readers, IncrementalReaders)
        live = {
            bead.id
            for bead, state in fold(baseline).items()
            if state.exists and not state.deleted and not state.erased
        }
        warnings: list[str] = []
        cursor_next: PassCursor | None = None
        scope = self.scope
        mode = MODE_INCREMENTAL
        head = self.readers.head() if narrow and scope is None else None  # before any other read
        if scope is None:
            usable, why = self._usable(narrow)
            if why:
                warnings.append(why)
            if usable:
                assert self.cursor is not None
                moved, newest = self._moved(baseline, self.cursor)
                scope = (moved | set(self.extra_scope)) & live
                cursor_next = PassCursor(
                    PASS_NAME,
                    head or self.cursor.land_sha,
                    newest.at if newest else self.cursor.events_at,
                    newest.row_id if newest else self.cursor.events_row_id,
                )
            elif self.extra_scope:
                scope, mode = frozenset(set(self.extra_scope) & live), MODE_SCOPED
            elif not self.full_ok:
                raise BackfillError(
                    "timeline: no usable cursor and no merged bead to scope the run to; "
                    "run `bh backfill timeline --since-cursor` once to start one"
                )
            else:
                mode = MODE_FULL
        readers = _ScopedReaders(self.readers, None if scope is None else frozenset(scope))
        narrowed = (
            list(baseline)
            if scope is None
            else [event for event in baseline if event.bead.id in scope]
        )
        timeline = build_timeline(narrowed, readers, extracted_at=extracted_at)
        if mode == MODE_FULL and head is not None and readers.read is not None:
            newest = _newest(readers.read.events)
            cursor_next = PassCursor(
                PASS_NAME, head, newest.at if newest else None, newest.row_id if newest else None
            )
        self.info = incremental_info(
            mode,
            cursor_in=self.cursor,
            cursor_next=cursor_next,
            warnings=warnings,
            scope=None if scope is None else sorted(scope),
        )
        return timeline

    def propose(
        self,
        baseline: Sequence[BeadEvent],
        *,
        extracted_at: str,
        judge: JudgmentPort,
    ) -> Iterator[BeadEvent]:
        if self.incremental:
            timeline = self._incremental(baseline, extracted_at)
        else:
            timeline = build_timeline(baseline, self.readers, extracted_at=extracted_at)
        self._notes = timeline.notes
        yield from timeline.lifecycle_events()

    def verify(self, states: Mapping[BeadRef, BeadState]) -> list[str]:
        """Every lifecycle key a bead carries is well-formed and has its confidence tier."""
        errors: list[str] = []
        for bead, state in states.items():
            for name in FACTS:
                value = state.fields.get(fact_field(name))
                tier = state.fields.get(confidence_field(name))
                if value is None and tier is None:
                    continue
                where = f"{bead.id}: {fact_field(name)}"
                if value is None or tier is None:
                    errors.append(f"{where} needs both the value and its confidence")
                elif tier not in TIERS:
                    errors.append(f"{where}_confidence is {tier!r}, not one of {sorted(TIERS)}")
                if name in COUNT_FACTS:
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        errors.append(f"{where} is not a count: {value!r}")
                elif value is not None and not _timestamp(value):
                    errors.append(f"{where} is not a canonical timestamp: {value!r}")
        return errors

    def report(
        self, states: Mapping[BeadRef, BeadState], proposed: Sequence[BeadEvent]
    ) -> dict[str, Any]:
        by_fact: Counter[str] = Counter()
        by_tier: Counter[str] = Counter()
        for event in proposed:
            name = str(event.data["field"]).removeprefix(LIFECYCLE_PREFIX)
            if name.endswith(CONFIDENCE_SUFFIX):
                by_tier[str(event.data["value"])] += 1
            else:
                by_fact[name] += 1
        report: dict[str, Any] = {
            "facts": dict(sorted(by_fact.items())),
            "confidence": dict(sorted(by_tier.items())),
            "beads_with_facts": len({event.bead for event in proposed}),
            "notes": dict(sorted(self._notes.items())),
        }
        if self.info is not None:
            report[INCREMENTAL_KEY] = self.info
        return report


def _timestamp(value: Any) -> bool:
    try:
        check_timestamp(value)
    except ValueError:
        return False
    return True


__all__ = [
    "COUNT_FACTS",
    "FACTS",
    "INVALIDATING_FIELDS",
    "LIFECYCLE_FIELDS",
    "LIFECYCLE_PREFIX",
    "PASS_NAME",
    "PASS_SCHEMA_VERSION",
    "TIERS",
    "TIMESTAMP_FACTS",
    "Fact",
    "PathReaders",
    "Timeline",
    "TimelinePass",
    "TimelineReaders",
    "build_timeline",
    "confidence_field",
    "fact_events",
    "fact_field",
]
