"""Per-pass cursors and incremental runs of the ``timeline`` and ``commits`` passes.

The properties under test (the acceptance of continuous backfill):

- an incremental run after N new merges proposes exactly the events a full run would add for
  them, and nothing else, and reads only the beads that moved;
- a cursor that cannot be trusted (corrupt file, a commit that left the history) is a warning and
  a full run; a missing one is a full run;
- applying an incremental plan converges (second dry run empty, a full run then finds nothing).

Sources are fakes that count their calls (no clock, no network); the git history of the
``commits`` pass is a throwaway repository; the store is a list of events folded on every export.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import beadlog
from beadhive_backfill import (
    BackfillEngine,
    BackfillError,
    IncrementalReaders,
    PassCursor,
    TimelinePass,
    cursor_to_record,
    read_cursor,
    source_pass,
    source_pass_for_plan,
    write_cursor,
)
from beadhive_backfill.incremental import CURSOR_SCHEMA, read_cursors
from beadhive_backfill.merges import parse_first_parent
from beadhive_backfill.run_journals import RunJournalRead
from beadlog_sources import CommitDates, HistoryEvent, HistoryRead, baseline_events

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000ee", "wd")
STAMP = "2026-11-01T00:00:00Z"
LAND_A1, LAND_A2, LAND_B1, LAND_B2, LAND_B3 = (
    "a1" * 20,
    "a2" * 20,
    "b1" * 20,
    "b2" * 20,
    "b3" * 20,
)
HEAD_A, HEAD_B = "aa" * 20, "bb" * 20


def row(iid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [],
        "created_at": "2026-08-01T09:00:00Z",
        "updated_at": "2026-08-01T09:00:00Z",
        **extra,
    }


def ev(row_id: str, bead: str, kind: str, at: str, **extra: Any) -> HistoryEvent:
    return HistoryEvent(row_id, bead, kind, at, actor="dev/a", **extra)


def closed(row_id: str, bead: str, at: str) -> HistoryEvent:
    return ev(row_id, bead, "closed", at, new_status="closed")


def claimed(row_id: str, bead: str, at: str) -> HistoryEvent:
    return ev(row_id, bead, "claimed", at, assignee="dev/a", new_status="in_progress")


# -- the hive before and after N new merges --------------------------------------------------

ROWS_A = [
    row("wd-1", status="closed", closed_at="2026-08-12T10:00:00Z", assignee="dev/a"),
    row("wd-2", status="closed", closed_at="2026-08-13T10:00:00Z", assignee="dev/a"),
    row("wd-3", status="closed", closed_at="2026-08-14T10:00:00Z", labels=["batch:group-a"]),
]
# Three merges later: wd-4 (its own land), wd-5 and wd-6 (one batch land), plus wd-7 which was
# claimed but not merged. wd-1..wd-3 do not move.
ROWS_NEW = [
    row("wd-4", status="closed", closed_at="2026-09-02T10:00:00Z", assignee="dev/b"),
    row("wd-5", status="closed", closed_at="2026-09-03T10:00:00Z", labels=["batch:group-b"]),
    row("wd-6", status="closed", closed_at="2026-09-03T10:00:00Z", labels=["batch:group-b"]),
    row("wd-7"),
]
HISTORY_A = [
    ev("e-101", "wd-1", "created", "2026-08-01T09:00:00Z"),
    claimed("e-102", "wd-1", "2026-08-10T10:00:00Z"),
    closed("e-103", "wd-1", "2026-08-12T10:00:00Z"),
    claimed("e-201", "wd-2", "2026-08-10T10:00:00Z"),
    closed("e-202", "wd-2", "2026-08-13T10:00:00Z"),
    closed("e-301", "wd-3", "2026-08-14T10:00:00Z"),
]
HISTORY_NEW = [
    claimed("e-401", "wd-4", "2026-09-01T10:00:00Z"),
    closed("e-402", "wd-4", "2026-09-02T10:00:00Z"),
    closed("e-501", "wd-5", "2026-09-03T10:00:00Z"),
    closed("e-601", "wd-6", "2026-09-03T10:00:00Z"),
    claimed("e-701", "wd-7", "2026-09-04T10:00:00Z"),
]
LOG_A = [
    f"{LAND_A2}\t2026-08-14T09:00:00+00:00\tchore(merge): batch group-a",
    f"{LAND_A1}\t2026-08-12T09:00:00+00:00\tchore(merge): bead wd-1",
]
LOG_NEW = [
    f"{LAND_B3}\t2026-09-03T09:00:00+00:00\tchore(merge): batch group-b",
    f"{LAND_B2}\t2026-09-02T09:00:00+00:00\tchore(merge): bead wd-4",
    f"{LAND_B1}\t2026-08-20T09:00:00+00:00\tchore(merge): refresh wt/bead/epic/wd-9 from main",
]


def history_read(events: Iterable[HistoryEvent]) -> HistoryRead:
    items = tuple(sorted(events, key=lambda event: event.sort_key))
    return HistoryRead(items, rows=len(items))


class Readers:
    """Fake timeline sources over a history and a first-parent log, counting every call. Also
    the narrow reads of an incremental run (:class:`IncrementalReaders`), served from the same
    lists, so the full and the incremental run see identical sources."""

    def __init__(
        self,
        history: Sequence[HistoryEvent],
        log: Sequence[str],
        *,
        head: str,
        known: Sequence[str],
        sha_order: Sequence[str],
    ) -> None:
        self.events = list(history)
        self.log = list(log)
        self.head_sha = head
        self.known = set(known)
        self.order = list(sha_order)  # oldest first; lands_since returns what follows a sha
        self.calls: dict[str, int] = {}
        self.history_of_ids: list[list[str]] = []

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    # -- TimelineReaders
    def history(self) -> HistoryRead:
        self._count("history")
        return history_read(self.events)

    def lands(self):
        self._count("lands")
        return parse_first_parent(self.log)

    def commit_dates(self, shas: Sequence[str]) -> CommitDates:
        self._count("commit_dates")
        return CommitDates({}, tuple(shas))

    def run_journals(self) -> RunJournalRead:
        return RunJournalRead()

    # -- IncrementalReaders
    def head(self) -> str | None:
        self._count("head")
        return self.head_sha

    def reaches(self, sha: str) -> bool:
        self._count("reaches")
        return sha in self.known

    def history_since(self, at: str) -> HistoryRead:
        self._count("history_since")
        return history_read(e for e in self.events if beadlog.instant(e.at) >= beadlog.instant(at))

    def history_of(self, ids: Sequence[str]) -> HistoryRead:
        self._count("history_of")
        self.history_of_ids.append(sorted(ids))
        return history_read(e for e in self.events if e.bead in set(ids))

    def lands_since(self, sha: str):
        self._count("lands_since")
        after = set(self.order[self.order.index(sha) + 1 :]) if sha in self.order else set()
        return parse_first_parent(line for line in self.log if line.split("\t")[0] in after)


def readers_a() -> Readers:
    return Readers(
        HISTORY_A,
        LOG_A,
        head=HEAD_A,
        known=[HEAD_A, LAND_A1, LAND_A2],
        sha_order=[LAND_A1, LAND_A2],
    )


def readers_b() -> Readers:
    order = [LAND_A1, LAND_A2, LAND_B1, LAND_B2, LAND_B3, HEAD_B]
    return Readers(
        [*HISTORY_A, *HISTORY_NEW],
        [*LOG_NEW, *LOG_A],
        head=HEAD_B,
        known=[HEAD_A, HEAD_B, *order],
        sha_order=[HEAD_A, *order[2:]],
    )


class FoldStore:
    """A store that is a list of events: the reader, loader and pre-state port of the engine."""

    name = "fake"
    version = "0"
    capabilities = frozenset({"valid-time"})

    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self.events = list(baseline_events(rows, group=GROUP.id, extracted_at=STAMP))

    def add_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self.events.extend(baseline_events(rows, group=GROUP.id, extracted_at=STAMP))

    def export(self) -> tuple[beadlog.BeadEvent, ...]:
        return tuple(self.events)

    def export_pre_state(self, baseline: Sequence[beadlog.BeadEvent]) -> str:
        return "pre-state"

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        if not dry_run:
            self.events.extend(events)
        return {"summary": {"by_code": {}}, "losses": []}


class ListSink:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def record(self, entry: Any) -> None:
        self.entries.append(entry)


def _clock() -> str:
    return STAMP


def engine_for(pass_: Any) -> BackfillEngine:
    return BackfillEngine(groups=[GROUP], clock=_clock, passes=[pass_])


def applicable(plan: Any) -> set[str]:
    return {event.id for event in plan.applicable_events()}


def beads_of(plan: Any) -> set[str]:
    return {event.bead.id for event in plan.applicable_events()}


@pytest.fixture
def applied_store() -> tuple[FoldStore, PassCursor]:
    """The hive after a full run applied at time A, and the cursor that run earned."""
    store = FoldStore(ROWS_A)
    pass_ = TimelinePass(readers_a(), incremental=True)
    plan = engine_for(pass_).plan(pass_, store)
    assert plan.report["incremental"]["mode"] == "full"
    cursor = cursor_to_record(plan.report, "timeline")
    assert cursor is not None
    store.load(plan.applicable_events(), dry_run=False)
    return store, cursor


# -- the timeline pass -----------------------------------------------------------------------


def test_the_first_incremental_run_is_full_and_earns_the_cursor(applied_store) -> None:
    _store, cursor = applied_store

    assert cursor.pass_name == "timeline"
    assert cursor.land_sha == HEAD_A  # the branch head, read before the other sources
    assert cursor.events_at == "2026-08-14T10:00:00Z"  # the newest events row it read
    assert cursor.events_row_id == "e-301"


def test_an_incremental_run_after_new_merges_proposes_exactly_what_a_full_run_adds(
    applied_store,
) -> None:
    store, cursor = applied_store
    store.add_rows(ROWS_NEW)

    full_readers = readers_b()
    full = TimelinePass(full_readers)
    full_plan = engine_for(full).plan(full, store)
    incr_readers = readers_b()
    incremental = TimelinePass(incr_readers, incremental=True, cursor=cursor)
    incr_plan = engine_for(incremental).plan(incremental, store)

    assert applicable(full_plan) and applicable(incr_plan) == applicable(full_plan)
    # the three merged beads and the claimed one; wd-1 and wd-2 are not touched at all
    assert beads_of(incr_plan) == beads_of(full_plan) == {"wd-4", "wd-5", "wd-6", "wd-7"}
    info = incr_plan.report["incremental"]
    assert info["mode"] == "incremental"
    # wd-3 owns the row the cursor stopped at (re-seen on purpose: the bound is inclusive); wd-1
    # and wd-2 are not looked at
    scope = ["wd-3", "wd-4", "wd-5", "wd-6", "wd-7"]
    assert info["scope"] == scope
    assert info["warnings"] == []
    assert incr_readers.history_of_ids == [scope]
    assert "history" not in incr_readers.calls  # no full read of the events table
    assert info["cursor_next"]["land_sha"] == HEAD_B
    assert info["cursor_next"]["events_row_id"] == "e-701"


def test_the_batch_land_reaches_every_member_of_the_batch(applied_store) -> None:
    store, cursor = applied_store
    store.add_rows(ROWS_NEW)
    # wd-6's own rows are older than the cursor; only the batch land names it (via its label)
    readers = readers_b()
    readers.events = [e for e in readers.events if e.bead != "wd-6"] + [
        closed("e-601", "wd-6", "2026-08-01T10:00:00Z")
    ]

    pass_ = TimelinePass(readers, incremental=True, cursor=cursor)
    plan = engine_for(pass_).plan(pass_, store)

    assert "wd-6" in plan.report["incremental"]["scope"]
    assert "merged_at" in plan.report["facts"]


def test_applying_an_incremental_plan_converges(applied_store) -> None:
    store, cursor = applied_store
    store.add_rows(ROWS_NEW)
    pass_ = TimelinePass(readers_b(), incremental=True, cursor=cursor)
    engine = engine_for(pass_)
    plan = engine.plan(pass_, store)

    # the apply re-derives exactly the planned scope (second dry run as narrow as the plan)
    apply_pass = TimelinePass.for_plan(readers_b(), plan.report)
    applier = engine_for(apply_pass)
    entry = applier.apply(plan, store, store, ListSink(), pre_state=store)

    assert sorted(entry.completed)  # the four beads
    full = TimelinePass(readers_b())
    assert engine_for(full).plan(full, store).changes == 0  # nothing a full run would still add


def test_a_run_that_finds_nothing_new_proposes_nothing(applied_store) -> None:
    store, cursor = applied_store
    readers = readers_a()  # the same sources the cursor came from
    pass_ = TimelinePass(readers, incremental=True, cursor=cursor)

    plan = engine_for(pass_).plan(pass_, store)

    assert plan.changes == 0
    # the cursor row itself is re-seen (inclusive), which is harmless: its beads are PRESENT
    assert plan.report["incremental"]["mode"] == "incremental"
    assert "history" not in readers.calls


@pytest.mark.parametrize(
    "cursor, why",
    [
        (PassCursor("timeline", "cd" * 20, "2026-08-14T10:00:00Z"), "is not in the branch history"),
        (PassCursor("timeline", None, "2026-08-14T10:00:00Z"), "incomplete"),
        (PassCursor("timeline", HEAD_A, None), "incomplete"),
    ],
)
def test_a_cursor_that_cannot_bound_the_run_warns_and_runs_in_full(
    applied_store, cursor: PassCursor, why: str
) -> None:
    store, _good = applied_store
    store.add_rows(ROWS_NEW)
    full = TimelinePass(readers_b())
    expected = applicable(engine_for(full).plan(full, store))

    readers = readers_b()
    pass_ = TimelinePass(readers, incremental=True, cursor=cursor)
    plan = engine_for(pass_).plan(pass_, store)

    info = plan.report["incremental"]
    assert info["mode"] == "full" and "scope" not in info
    assert len(info["warnings"]) == 1 and why in info["warnings"][0]
    assert applicable(plan) == expected  # a full run, exactly
    assert readers.calls["history"] == 1


def test_no_cursor_is_a_full_run_without_a_warning() -> None:
    store = FoldStore(ROWS_A)
    pass_ = TimelinePass(readers_a(), incremental=True)

    plan = engine_for(pass_).plan(pass_, store)

    assert plan.report["incremental"]["mode"] == "full"
    assert plan.report["incremental"]["warnings"] == []
    assert plan.changes == 3


def test_readers_that_cannot_read_narrowly_run_in_full_with_a_warning(applied_store) -> None:
    class Wide:
        def __init__(self, inner: Readers) -> None:
            self.inner = inner

        def history(self):
            return self.inner.history()

        def lands(self):
            return self.inner.lands()

        def commit_dates(self, shas):
            return self.inner.commit_dates(shas)

        def run_journals(self):
            return self.inner.run_journals()

    store, cursor = applied_store
    wide = Wide(readers_b())
    assert not isinstance(wide, IncrementalReaders)
    pass_ = TimelinePass(wide, incremental=True, cursor=cursor)  # type: ignore[arg-type]

    plan = engine_for(pass_).plan(pass_, store)

    assert plan.report["incremental"]["mode"] == "full"
    assert "cannot make incremental reads" in plan.report["incremental"]["warnings"][0]


def test_named_beads_without_a_cursor_run_scoped_and_move_no_cursor() -> None:
    store = FoldStore([*ROWS_A, *ROWS_NEW])
    readers = readers_b()
    pass_ = TimelinePass(readers, incremental=True, extra_scope=["wd-4", "wd-gone"])

    plan = engine_for(pass_).plan(pass_, store)

    info = plan.report["incremental"]
    assert info["mode"] == "scoped" and info["scope"] == ["wd-4"]  # a missing bead is dropped
    assert info["cursor_next"] is None
    assert cursor_to_record(plan.report, "timeline") is None
    assert beads_of(plan) == {"wd-4"}
    assert readers.history_of_ids == [["wd-4"]]


def test_a_trigger_that_must_stay_small_refuses_to_run_in_full() -> None:
    store = FoldStore(ROWS_A)
    pass_ = TimelinePass(readers_a(), incremental=True, full_ok=False)

    with pytest.raises(BackfillError, match="no usable cursor"):
        engine_for(pass_).plan(pass_, store)


def test_for_plan_rebuilds_a_full_pass_from_a_plan_that_was_not_incremental() -> None:
    store = FoldStore(ROWS_A)
    pass_ = TimelinePass(readers_a())
    plan = engine_for(pass_).plan(pass_, store)

    rebuilt = TimelinePass.for_plan(readers_a(), plan.report)

    assert not rebuilt.incremental and rebuilt.scope is None
    assert "incremental" not in plan.report  # the default report is unchanged


# -- the cursor file -------------------------------------------------------------------------


def test_cursors_round_trip_per_pass_and_keep_the_others(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cursors.json"
    write_cursor(path, PassCursor("commits", "ab" * 20, recorded_at=STAMP, plan_hash="p1"))
    write_cursor(
        path,
        PassCursor("timeline", HEAD_A, "2026-08-14T10:00:00Z", "e-301", STAMP, "p2", "/audit.json"),
    )

    cursors, warning = read_cursors(path)

    assert warning is None and sorted(cursors) == ["commits", "timeline"]
    assert (
        cursors["timeline"].events_row_id == "e-301" and cursors["timeline"].audit == "/audit.json"
    )
    assert read_cursor(path, "commits").cursor == cursors["commits"]
    assert read_cursor(path, "docs").cursor is None  # no cursor for a pass that never ran
    assert json.loads(path.read_text())["schema"] == CURSOR_SCHEMA


def test_a_missing_cursor_file_is_no_cursor_and_no_warning(tmp_path: Path) -> None:
    read = read_cursor(tmp_path / "absent.json", "timeline")

    assert read.cursor is None and read.warning is None


def _tamper(path: Path, how: str) -> None:
    data = json.loads(path.read_text())
    if how == "not-json":
        path.write_text("{ not json")
    elif how == "schema":
        path.write_text(json.dumps({**data, "schema": "0"}))
    elif how == "digest":
        data["passes"]["timeline"]["land_sha"] = "cd" * 20  # edited without re-signing
        path.write_text(json.dumps(data))
    elif how == "truncated":
        path.write_text(path.read_text()[:30])
    elif how == "binary":
        path.write_bytes(b"\xff\xfe\x00")
    elif how == "no-passes":
        path.write_text(json.dumps({"schema": CURSOR_SCHEMA}))


@pytest.mark.parametrize(
    "how", ["not-json", "schema", "digest", "truncated", "binary", "no-passes"]
)
def test_a_corrupt_cursor_file_reads_as_no_cursor_with_a_warning(tmp_path: Path, how: str) -> None:
    path = tmp_path / "cursors.json"
    write_cursor(path, PassCursor("timeline", HEAD_A, "2026-08-14T10:00:00Z"))
    _tamper(path, how)

    read = read_cursor(path, "timeline")

    assert read.cursor is None
    assert read.warning and str(path) in read.warning


def test_a_malformed_entry_is_corrupt_even_when_the_digest_matches(tmp_path: Path) -> None:
    import hashlib

    passes = {"timeline": {"pass": "timeline", "land_sha": "not-a-sha"}}
    body = json.dumps({"schema": CURSOR_SCHEMA, "passes": passes}, sort_keys=True)
    path = tmp_path / "cursors.json"
    path.write_text(
        json.dumps(
            {
                "schema": CURSOR_SCHEMA,
                "passes": passes,
                "digest": hashlib.sha256(body.encode()).hexdigest(),
            }
        )
    )

    read = read_cursor(path, "timeline")

    assert read.cursor is None and "land_sha" in (read.warning or "")


def test_writing_over_a_corrupt_file_replaces_it(tmp_path: Path) -> None:
    path = tmp_path / "cursors.json"
    path.write_text("garbage")

    write_cursor(path, PassCursor("timeline", HEAD_A, "2026-08-14T10:00:00Z"))

    read = read_cursor(path, "timeline")
    assert read.warning is None and read.cursor is not None and read.cursor.land_sha == HEAD_A


# -- the commits pass ------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    when = "2026-09-01T10:00:00+00:00"
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    )
    return done.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        _git(root, *command)
    return root


def commits_plan(repo: Path, store: FoldStore, **kwargs: Any):
    pass_ = source_pass("commits", repo, GROUP.id, **kwargs)
    return pass_, engine_for(pass_).plan(pass_, store)


def test_an_incremental_commits_run_reads_only_the_commits_after_the_cursor(repo: Path) -> None:
    store = FoldStore([row("wd-1"), row("wd-2"), row("wd-3")])
    first = _commit(repo, "feat: first change (wd-1)")
    _, plan_a = commits_plan(repo, store, incremental=True)
    assert plan_a.report["incremental"]["mode"] == "full"  # the first run has no cursor
    cursor = cursor_to_record(plan_a.report, "commits")
    assert cursor is not None and cursor.land_sha == first
    store.load(plan_a.applicable_events(), dry_run=False)

    _commit(repo, "feat: second change (wd-2)")
    _commit(repo, "fix: third change (wd-3)")
    _, full = commits_plan(repo, store)
    pass_, incremental = commits_plan(repo, store, incremental=True, cursor=cursor)

    assert applicable(incremental) == applicable(full) and beads_of(full) == {"wd-2", "wd-3"}
    assert incremental.report["incremental"]["mode"] == "incremental"
    assert incremental.report["options"]["since"] == first  # pinned, so the apply re-reads it
    assert incremental.report["strategy"]["examined"] == 2  # only the new commits were read
    assert full.report["strategy"]["examined"] == 3


def test_an_applied_incremental_commits_plan_converges(repo: Path) -> None:
    store = FoldStore([row("wd-1"), row("wd-2")])
    first = _commit(repo, "feat: a (wd-1)")
    _commit(repo, "feat: b (wd-2)")
    cursor = PassCursor("commits", first)
    pass_, plan = commits_plan(repo, store, incremental=True, cursor=cursor)

    # the apply rebuilds the pass from the plan's recorded options (no cursor needed)
    rebuilt = source_pass_for_plan("commits", repo, GROUP.id, plan.report)
    entry = engine_for(rebuilt).apply(plan, store, store, ListSink(), pre_state=store)

    assert list(entry.completed) == [f"{GROUP.id}/wd-2"] or len(entry.completed) == 1
    assert store.events  # applied
    _, again = commits_plan(repo, store, incremental=True, cursor=PassCursor("commits", first))
    assert again.changes == 0


def test_a_commits_cursor_that_left_the_history_warns_and_reads_in_full(repo: Path) -> None:
    store = FoldStore([row("wd-1"), row("wd-2")])
    _commit(repo, "feat: a (wd-1)")
    _commit(repo, "feat: b (wd-2)")
    _, full = commits_plan(repo, store)

    _, plan = commits_plan(repo, store, incremental=True, cursor=PassCursor("commits", "cd" * 20))

    info = plan.report["incremental"]
    assert info["mode"] == "full" and "is not in the history" in info["warnings"][0]
    assert "since" not in plan.report["options"]
    assert applicable(plan) == applicable(full)


def test_a_commits_run_with_nothing_new_proposes_nothing(repo: Path) -> None:
    store = FoldStore([row("wd-1")])
    head = _commit(repo, "feat: a (wd-1)")
    _, plan = commits_plan(repo, store, incremental=True, cursor=PassCursor("commits", head))

    assert plan.changes == 0 and plan.report["strategy"]["examined"] == 0


def test_named_starting_commit_without_a_cursor_is_a_scoped_run(repo: Path) -> None:
    store = FoldStore([row("wd-1"), row("wd-2")])
    first = _commit(repo, "feat: a (wd-1)")
    _commit(repo, "feat: b (wd-2)")

    _, plan = commits_plan(repo, store, incremental=True, since=first)

    info = plan.report["incremental"]
    assert info["mode"] == "scoped" and info["cursor_next"] is None
    assert beads_of(plan) == {"wd-2"}
    assert cursor_to_record(plan.report, "commits") is None


def test_a_trigger_that_must_stay_small_never_reads_commits_in_full(repo: Path) -> None:
    store = FoldStore([row("wd-1")])
    _commit(repo, "feat: a (wd-1)")

    with pytest.raises(BackfillError, match="no usable cursor"):
        commits_plan(repo, store, incremental=True, full_ok=False)


def test_since_cursor_cannot_be_combined_with_an_explicit_since(repo: Path) -> None:
    _commit(repo, "feat: a (wd-1)")

    with pytest.raises(BackfillError, match="cannot be combined"):
        source_pass("commits", repo, GROUP.id, options={"since": "HEAD"}, incremental=True)


def test_only_the_commits_pass_has_an_incremental_form(repo: Path) -> None:
    with pytest.raises(BackfillError, match="commits and timeline"):
        source_pass("docs", repo, GROUP.id, incremental=True)


def test_the_default_commits_report_is_unchanged(repo: Path) -> None:
    store = FoldStore([row("wd-1")])
    _commit(repo, "feat: a (wd-1)")

    _, plan = commits_plan(repo, store)

    assert "incremental" not in plan.report


def test_path_readers_answer_the_narrow_git_reads_of_a_real_repository(repo: Path) -> None:
    from beadhive_backfill import PathReaders

    first = _commit(repo, "chore(merge): bead wd-1")
    second = _commit(repo, "chore(merge): bead wd-2")
    readers = PathReaders(repo, ref="main")

    assert isinstance(readers, IncrementalReaders)
    assert readers.head() == second
    assert readers.reaches(first) and readers.reaches(second)
    assert not readers.reaches("cd" * 20)
    assert [land.target for land in readers.lands_since(first).merges] == ["wd-2"]
    assert PathReaders(repo, ref="no-such-branch").head() is None
