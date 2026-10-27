"""Intake's own pager and snapshot loop (gap G5): same semantics as the tracker's helpers."""

from __future__ import annotations

import pytest

from beadhive.modules.intake.application import collect_snapshot, iterate_pages
from beadhive.modules.tracker.contracts import (
    IdPage,
    IdQuery,
    IdSubject,
    IssueKey,
    Page,
    PageCursor,
    TransientFailure,
)
from beadhive.testing.tracker import at, memory_subject


class _ScriptedIds:
    """A reader whose ``list_ids`` answers from a script of pages (or raises)."""

    def __init__(self, script: list[IdPage | Exception]) -> None:
        self.script = script
        self.queries: list[IdQuery] = []

    def list_ids(self, query: IdQuery) -> IdPage:
        self.queries.append(query)
        step = self.script[len(self.queries) - 1]
        if isinstance(step, Exception):
            raise step
        return step


def test_iterate_pages_follows_cursors_to_the_last_page() -> None:
    seen: list[PageCursor | None] = []

    def fetch(cursor: PageCursor | None) -> Page[int]:
        seen.append(cursor)
        index = int(cursor.token) if cursor else 0
        nxt = PageCursor(str(index + 1)) if index < 2 else None
        return Page((index,), nxt, None)

    assert [page.items for page in iterate_pages(fetch)] == [(0,), (1,), (2,)]
    assert seen == [None, PageCursor("1"), PageCursor("2")]


def test_collect_snapshot_is_complete_and_forwards_the_query() -> None:
    issue = IssueKey("#1", "memory://x/issues/1")
    reader = _ScriptedIds([IdPage(("a", "b"), PageCursor("2")), IdPage(("c",), None)])

    snap = collect_snapshot(reader, IdQuery(IdSubject.COMMENTS, issue=issue, page_size=2))

    assert snap.complete and snap.ids == {"a", "b", "c"}
    assert [q.cursor for q in reader.queries] == [None, PageCursor("2")]
    assert {(q.subject, q.issue, q.page_size) for q in reader.queries} == {
        (IdSubject.COMMENTS, issue, 2)
    }


def test_collect_snapshot_is_incomplete_after_a_port_error_mid_listing() -> None:
    reader = _ScriptedIds([IdPage(("a",), PageCursor("2")), TransientFailure("cut off")])

    snap = collect_snapshot(reader, IdQuery(IdSubject.ISSUES))

    assert not snap.complete and snap.ids == {"a"}  # never infer a deletion from this


def test_collect_snapshot_does_not_swallow_programming_errors() -> None:
    reader = _ScriptedIds([ValueError("a bug, not a tracker answer")])

    with pytest.raises(ValueError, match="a bug"):
        collect_snapshot(reader, IdQuery(IdSubject.ISSUES))


def test_the_pager_pages_a_memory_tracker_completely() -> None:
    subject = memory_subject(page_size=2)
    keys = [subject.world.add_issue(f"i{n}", at=at(n)) for n in range(1, 6)]
    subject.world.add_change_request("cr", at=at(7))  # excluded by the port

    snap = collect_snapshot(subject.adapter, IdQuery(IdSubject.ISSUES, page_size=2))

    assert snap.complete and snap.ids == {key.url for key in keys}
    assert subject.world.write_requests() == 0


def test_the_pager_matches_the_trackers_own_helper_semantics() -> None:
    """Gap G5 makes intake own the loop; it must not drift from the tracker's meaning of
    'complete'. (The only place a test reaches the tracker's application layer.)"""
    from beadhive.modules.tracker.application import snapshot_ids

    script = [IdPage(("a",), PageCursor("2")), TransientFailure("cut off")]
    mine = collect_snapshot(_ScriptedIds(list(script)), IdQuery(IdSubject.ISSUES))
    theirs = snapshot_ids(_ScriptedIds(list(script)), IdQuery(IdSubject.ISSUES))

    assert mine == theirs
