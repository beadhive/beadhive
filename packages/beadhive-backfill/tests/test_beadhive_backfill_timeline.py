"""The lifecycle timeline pass over a fixture hive, through fake readers and a fake store.

No clock, no network, no subprocess: the four source readers are fakes that count their calls, the
extractor clock is a constant, and the store is a list of events folded on every export.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import beadlog
from beadhive_backfill import BackfillEngine, JsonFileAuditSink, Plan
from beadhive_backfill.merges import parse_first_parent
from beadhive_backfill.run_journals import RunJournalRead, read_journal_text
from beadhive_backfill.timeline import (
    COUNT_FACTS,
    FACTS,
    LIFECYCLE_FIELDS,
    TIERS,
    TimelinePass,
    confidence_field,
    fact_field,
)
from beadlog_sources import (
    CommitDate,
    CommitDates,
    HistoryEvent,
    HistoryRead,
    baseline_events,
)

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000cc", "wd")
STAMP = "2026-11-01T00:00:00Z"
SHA1, SHA2, SHA3 = ("a" * 40, "b" * 40, "c" * 40)
LAND1, LAND2, LAND3, LAND4, REFRESH = ("1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40)


def ref(iid: str) -> beadlog.BeadRef:
    return beadlog.BeadRef(GROUP.id, iid)


def row(iid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [],
        "created_at": "2026-08-10T09:00:00Z",
        "updated_at": "2026-08-20T00:00:00Z",
        **extra,
    }


def ev(
    row_id: str, bead: str, kind: str, at: str, *, actor: str | None = "dev/a", **extra: Any
) -> HistoryEvent:
    return HistoryEvent(row_id, bead, kind, at, actor=actor, **extra)


ROWS = [
    # wd-1: the whole lifecycle, landed by its own bead merge BEFORE bd closed it
    row(
        "wd-1",
        status="closed",
        closed_at="2026-08-12T10:00:00Z",
        assignee="dev/a",
        metadata={"git.commits": json.dumps([SHA2, SHA1])},
    ),
    # wd-2: closed, reopened, closed; two lands (the latest decides)
    row("wd-2", status="closed", closed_at="2026-08-16T10:00:00Z"),
    # wd-3: imported (no events-table created row), landed in a named batch
    row(
        "wd-3",
        status="closed",
        closed_at="2026-08-17T10:00:00Z",
        labels=["batch:group-a"],
        created_at="2026-06-29T03:43:46Z",
    ),
    # wd-4: a snapshot shows closed -> open -> closed, but only closes were ever recorded
    row("wd-4", status="closed", closed_at="2026-08-14T10:00:00Z"),
    # wd-5: never claimed, never landed
    row("wd-5"),
]

HISTORY = HistoryRead(
    tuple(
        sorted(
            [
                ev("e-101", "wd-1", "created", "2026-08-10T09:00:00Z"),
                ev(
                    "e-102",
                    "wd-1",
                    "claimed",
                    "2026-08-10T10:00:00Z",
                    assignee="dev/a",
                    new_status="in_progress",
                    changes={"assignee": "dev/a", "status": "in_progress"},
                ),
                ev("e-103", "wd-1", "label_added", "2026-08-11T09:00:00Z", label="review:pending"),
                ev(
                    "e-104",
                    "wd-1",
                    "label_removed",
                    "2026-08-11T12:00:00Z",
                    actor="disp/x",
                    label="review:pending",
                ),
                ev("e-105", "wd-1", "closed", "2026-08-12T10:00:00Z", new_status="closed"),
                ev("e-201", "wd-2", "created", "2026-08-10T09:00:00Z"),
                ev("e-202", "wd-2", "closed", "2026-08-12T10:00:00Z", new_status="closed"),
                ev(
                    "e-203",
                    "wd-2",
                    "reopened",
                    "2026-08-13T10:00:00Z",
                    new_status="open",
                    reason="more work",
                ),
                ev("e-204", "wd-2", "closed", "2026-08-16T10:00:00Z", new_status="closed"),
                ev("e-301", "wd-3", "closed", "2026-08-17T10:00:00Z", new_status="closed"),
                ev("e-401", "wd-4", "created", "2026-08-10T09:00:00Z"),
                ev("e-402", "wd-4", "closed", "2026-08-12T10:00:00Z", new_status="closed"),
                ev("e-403", "wd-4", "closed", "2026-08-14T10:00:00Z", new_status="closed"),
                ev("e-501", "wd-5", "created", "2026-08-10T09:00:00Z"),
            ],
            key=lambda event: event.sort_key,
        )
    ),
    rows=15,
)

# first-parent log lines: land commits for wd-1, wd-2 (twice), the batch; refresh and
# merge-main subjects, which are never lands.
LOG = [
    f"{'7' * 40}\t2026-08-18T09:00:00+00:00\tchore(merge): bead wd-404",
    f"{LAND4}\t2026-08-17T09:00:00+00:00\tchore(merge): batch group-a",
    f"{REFRESH}\t2026-08-16T08:00:00+00:00\tchore(merge): refresh wt/bead/epic/wd-9 from main",
    f"{LAND3}\t2026-08-16T09:30:00+02:00\tchore(merge): bead wd-2",
    f"{LAND2}\t2026-08-12T09:30:00+00:00\tchore(merge): bead wd-1",
    f"{LAND1}\t2026-08-12T07:00:00+00:00\tchore(merge): bead wd-2",
    "6" * 40 + "\t2026-08-11T00:00:00+00:00\tMerge branch 'main' into wt/bead/wd-7",
]
DATES = {
    SHA1: CommitDate(SHA1, "2026-08-10T11:00:00Z", "2026-08-10T11:05:00Z"),
    SHA2: CommitDate(SHA2, "2026-08-11T08:00:00Z", "2026-08-11T08:10:00Z"),
}
JOURNAL = (
    "\n".join(
        json.dumps(
            {
                "version": "beadhive.run-journal/v1",
                "run_id": "run-1",
                "hive": "github/acme/widgets",
                "bead": "wd-1",
                "driver": "baml",
                "provider": "claude-code",
                "manifest_digest": "sha256:" + "a" * 64,
                "source_revision": f"opaque:{n}",
                "timestamp_ms": millis,
                "writer": "w",
                "activity": {"kind": kind},
            }
        )
        for n, millis, kind in (
            (1, 1786356000000, "run.created"),  # 2026-08-10T10:00:00Z
            (2, 1786356060000, "process.spawned"),
            (3, 1786358400000, "process.harvested"),
        )
    )
    + "\n"
)


class CountingReaders:
    """Fixture readers that count every call, so the cost of a read is asserted, not timed."""

    def __init__(
        self,
        history: HistoryRead = HISTORY,
        log: Sequence[str] = LOG,
        dates: Mapping[str, CommitDate] = DATES,
        journal: str = JOURNAL,
    ) -> None:
        self._history, self._log, self._dates, self._journal = history, log, dates, journal
        self.calls: dict[str, int] = {"history": 0, "lands": 0, "commit_dates": 0, "journals": 0}

    def history(self) -> HistoryRead:
        self.calls["history"] += 1
        return self._history

    def lands(self):
        self.calls["lands"] += 1
        return parse_first_parent(self._log)

    def commit_dates(self, shas: Sequence[str]) -> CommitDates:
        self.calls["commit_dates"] += 1
        found = {sha: self._dates[sha] for sha in shas if sha in self._dates}
        return CommitDates(found, tuple(sha for sha in shas if sha not in self._dates))

    def run_journals(self):
        self.calls["journals"] += 1
        records, skipped = read_journal_text(self._journal, "run-1.jsonl")
        return RunJournalRead(tuple(records), skipped, 1)


def baseline(rows: Sequence[dict[str, Any]] = ROWS) -> tuple[beadlog.BeadEvent, ...]:
    return baseline_events(rows, group=GROUP.id, extracted_at=STAMP)


class FoldStore:
    """A store that is a list of events: the reader, loader and pre-state port of the engine."""

    name = "fake"
    version = "0"
    capabilities = frozenset({"valid-time"})

    def __init__(self, events: Iterable[beadlog.BeadEvent]) -> None:
        self.events = list(events)
        self.exported = False

    def export(self) -> tuple[beadlog.BeadEvent, ...]:
        return tuple(self.events)

    def export_pre_state(self, baseline: Sequence[beadlog.BeadEvent]) -> str:
        self.exported = True
        return "pre-state"

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        if not dry_run:
            assert self.exported
            self.events.extend(events)
        return {"summary": {"by_code": {}}, "losses": []}


class ListSink:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def record(self, entry: Any) -> None:
        self.entries.append(entry)


def timeline_of(iid: str, readers: CountingReaders | None = None):
    pass_ = TimelinePass(readers or CountingReaders())
    timeline = pass_.timeline(baseline(), extracted_at=STAMP)
    return timeline, timeline.of(ref(iid))


def brief(events: Iterable[beadlog.BeadEvent]) -> list[tuple[Any, ...]]:
    """A bead's timeline as comparable tuples: (occurred, recorded, kind, what, provenance)."""
    out = []
    for event in events:
        data = event.data
        what = data.get("field") or data.get("phase") or data.get("label") or ""
        if "value" in data and data.get("field", "").startswith("metadata.lifecycle."):
            what = f"{what}={data['value']}"
        out.append(
            (event.occurred_at, event.recorded_at, str(event.kind), what, str(event.provenance))
        )
    return out


def test_the_pass_reproduces_the_expected_timeline_of_a_bead() -> None:
    _, events = timeline_of("wd-1")
    lifecycle = [e for e in events if str(e.data.get("field", "")).startswith("metadata.life")]
    values = {
        e.data["field"].removeprefix("metadata.lifecycle."): e
        for e in lifecycle
        if not e.data["field"].endswith("_confidence")
    }

    # the facts, each with its two times and provenance
    assert {
        name: (e.occurred_at, e.recorded_at, str(e.provenance)) for name, e in values.items()
    } == {
        "claimed_at": ("2026-08-10T10:00:00Z",) * 2 + ("observed",),
        "committed_at": ("2026-08-10T11:00:00Z", "2026-08-10T11:05:00Z", "derived"),
        "last_committed_at": ("2026-08-11T08:00:00Z", "2026-08-11T08:10:00Z", "derived"),
        "submitted_at": ("2026-08-11T09:00:00Z",) * 2 + ("observed",),
        "reviewed_at": ("2026-08-11T12:00:00Z",) * 2 + ("observed",),
        "merged_at": ("2026-08-12T09:30:00Z", "2026-08-12T10:00:00Z", "derived"),
        "run_started_at": ("2026-08-10T10:01:00Z",) * 2 + ("observed",),
        "run_ended_at": ("2026-08-10T10:40:00Z",) * 2 + ("observed",),
    }
    tiers = {e.data["value"] for e in lifecycle if e.data["field"].endswith("_confidence")}
    assert tiers and tiers <= set(TIERS)

    # the ordering the design requires: the land (git) happened BEFORE bd recorded the close
    merged = values["merged_at"]
    closed = next(e for e in events if e.data.get("phase") == "closed")
    assert closed.recorded_at == "2026-08-12T10:00:00Z"
    assert beadlog.instant(merged.occurred_at) < beadlog.instant(closed.recorded_at)
    assert merged.recorded_at == closed.recorded_at  # bd recorded the merge when it closed

    # the whole timeline, history and lifecycle together, in the format's total order
    assert brief(
        e for e in events if e.kind is not beadlog.EventKind.SET or "life" not in e.data["field"]
    ) == [
        ("2026-08-10T09:00:00Z", "2026-08-10T09:00:00Z", "create", "", "observed"),
        ("2026-08-10T10:00:00Z", "2026-08-10T10:00:00Z", "set", "assignee", "observed"),
        ("2026-08-10T10:00:00Z", "2026-08-10T10:00:00Z", "phase", "in_progress", "observed"),
        ("2026-08-11T09:00:00Z", "2026-08-11T09:00:00Z", "label_add", "review:pending", "observed"),
        (
            "2026-08-11T12:00:00Z",
            "2026-08-11T12:00:00Z",
            "label_remove",
            "review:pending",
            "observed",
        ),
        ("2026-08-12T10:00:00Z", "2026-08-12T10:00:00Z", "phase", "closed", "observed"),
    ]
    times = [beadlog.ordering_key(e) for e in events]
    assert times == sorted(times)


def test_the_merge_time_is_the_land_commit_never_a_refresh_or_a_main_merge() -> None:
    timeline, _ = timeline_of("wd-1")
    merged = {
        bead.id: next(f for f in facts if f.name == "merged_at")
        for bead, facts in timeline.facts.items()
        if any(f.name == "merged_at" for f in facts)
    }
    assert set(merged) == {"wd-1", "wd-2", "wd-3"}
    assert merged["wd-1"].value == "2026-08-12T09:30:00Z"
    assert merged["wd-1"].ref == f"git:{LAND2}"
    # two lands for wd-2: the later one (a +02:00 committer date, normalized to UTC) decides
    assert merged["wd-2"].value == "2026-08-16T07:30:00Z"
    assert merged["wd-2"].recorded_at == "2026-08-16T10:00:00Z"
    # a batch land names its group; membership comes from the bead's batch label, medium tier
    assert merged["wd-3"].value == "2026-08-17T09:00:00Z"
    assert merged["wd-3"].tier == "medium" and merged["wd-1"].tier == "high"
    assert timeline.notes["lands_unmatched"] == 1  # the land of a bead the hive does not hold
    assert timeline.notes["lands_skipped:refresh"] == 1
    assert timeline.notes["lands_skipped:merge_main"] == 1


def test_a_close_open_close_in_snapshots_without_a_reopened_event_is_no_reopen() -> None:
    timeline, events = timeline_of("wd-4")
    names = {f.name for f in timeline.facts.get(ref("wd-4"), ())}
    assert "reopened_at" not in names and "reopen_count" not in names
    assert not [e for e in events if e.data.get("phase") in ("open", "in_progress")]
    assert [e.data["phase"] for e in events if e.kind is beadlog.EventKind.PHASE] == [
        "closed",
        "closed",
    ]

    # a recorded `reopened` event, in contrast, is a reopen (wd-2)
    wd2 = {f.name: f for f in timeline.facts[ref("wd-2")]}
    assert wd2["reopened_at"].value == "2026-08-13T10:00:00Z"
    assert wd2["reopen_count"].value == 1 and wd2["reopen_count"].provenance == "derived"


def test_a_bead_without_a_created_event_gets_an_inferred_low_confidence_create() -> None:
    _, events = timeline_of("wd-3")
    (create,) = [e for e in events if e.kind is beadlog.EventKind.CREATE]
    assert create.provenance == "inferred" and create.confidence is not None
    assert create.confidence <= 0.5
    assert create.occurred_at == "2026-06-29T03:43:46Z" and create.recorded_at is None
    assert create.source.ref == "bd:inferred/wd-3#create"
    # and one with a created event does not
    _, observed = timeline_of("wd-1")
    assert next(e for e in observed if e.kind is beadlog.EventKind.CREATE).provenance == "observed"


def test_propose_emits_only_lifecycle_sets_with_a_confidence_per_fact() -> None:
    pass_ = TimelinePass(CountingReaders())
    proposed = list(pass_.propose(baseline(), extracted_at=STAMP, judge=None))  # type: ignore[arg-type]
    assert proposed and all(e.kind is beadlog.EventKind.SET for e in proposed)
    fields = {e.data["field"] for e in proposed}
    assert fields <= LIFECYCLE_FIELDS
    for name in FACTS:
        assert (fact_field(name) in fields) == (confidence_field(name) in fields)
    assert {e.bead.id for e in proposed} == {"wd-1", "wd-2", "wd-3"}  # wd-4/5: no facts
    for event in proposed:
        if event.data["field"].endswith("_confidence"):
            assert event.data["value"] in TIERS
        elif event.data["field"].removeprefix("metadata.lifecycle.") in COUNT_FACTS:
            assert isinstance(event.data["value"], int)
    # extraction is deterministic: the same readers give the same ids, whatever the clock
    again = list(
        TimelinePass(CountingReaders()).propose(
            baseline(),
            extracted_at="2027-01-01T00:00:00Z",
            judge=None,  # type: ignore[arg-type]
        )
    )
    assert [e.id for e in again] == [e.id for e in proposed]


def test_a_full_read_costs_a_fixed_number_of_calls_not_one_per_bead() -> None:
    readers = CountingReaders()
    list(TimelinePass(readers).propose(baseline(), extracted_at=STAMP, judge=None))  # type: ignore[arg-type]
    assert readers.calls == {"history": 1, "lands": 1, "commit_dates": 1, "journals": 1}
    many = [row(f"wd-{n}", metadata={"git.commits": json.dumps([SHA1])}) for n in range(40)]
    big = CountingReaders()
    list(TimelinePass(big).propose(baseline(many), extracted_at=STAMP, judge=None))  # type: ignore[arg-type]
    assert big.calls == readers.calls


def test_apply_writes_only_lifecycle_keys_and_a_second_run_proposes_nothing(tmp_path: Path) -> None:
    store = FoldStore(baseline())
    before = beadlog.fold(store.export())
    pass_ = TimelinePass(CountingReaders())
    engine = BackfillEngine(groups=[GROUP], clock=lambda: STAMP, passes=[pass_])

    plan = engine.plan(pass_, store)
    assert plan.changes == 3 and plan.pass_name == "timeline"
    assert plan.totals["by_class"]["DRIFT"] == 3
    assert plan.report["facts"]["merged_at"] == 3 and plan.report["facts"]["claimed_at"] == 1
    path = tmp_path / "plan.bdlg"
    plan.write(path)
    plan = Plan.read(path)  # the saved plan is what is applied

    audit = engine.apply(
        plan, store, store, JsonFileAuditSink(tmp_path / "audit.json"), pre_state=store
    )
    assert audit.state.value == "applied"
    assert audit.verification == {
        "pass_errors": [],
        "second_dry_run_changes": 0,
        "unrecorded_judgments": 0,
    }

    after = beadlog.fold(store.export())
    changed: set[str] = set()
    for bead, state in after.items():
        old = before[bead]
        assert (state.type, state.phase, state.labels, state.edges, state.comments) == (
            old.type,
            old.phase,
            old.labels,
            old.edges,
            old.comments,
        )
        for name in set(state.fields) | set(old.fields):
            if state.fields.get(name) != old.fields.get(name):
                changed.add(name)
    assert changed and changed <= LIFECYCLE_FIELDS  # nothing but metadata.lifecycle.*

    assert engine.plan(pass_, store).changes == 0  # the idempotency proof, again by hand
    assert pass_.verify(after) == []


def test_verify_flags_a_malformed_lifecycle_key() -> None:
    pass_ = TimelinePass(CountingReaders())
    state = beadlog.fold(
        [
            *baseline([row("wd-1")]),
            *[
                beadlog.mint_event(
                    "set",
                    ref("wd-1"),
                    {"field": name, "value": value},
                    occurred_at="2026-08-12T00:00:00Z",
                    recorded_at=None,
                    extracted_at=STAMP,
                    source=("test", f"x/{name}"),
                    provenance="derived",
                )
                for name, value in (
                    (fact_field("merged_at"), "yesterday"),
                    (confidence_field("merged_at"), "certain"),
                    (fact_field("reopen_count"), -1),
                    (fact_field("claimed_at"), "2026-08-12T00:00:00Z"),
                )
            ],
        ]
    )
    errors = pass_.verify(state)
    assert any("merged_at is not a canonical timestamp" in e for e in errors)
    assert any("merged_at_confidence is 'certain'" in e for e in errors)
    assert any("reopen_count" in e for e in errors)
    assert any("claimed_at needs both" in e for e in errors)
