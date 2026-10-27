"""The application helpers ``iterate`` and ``snapshot_ids`` over a scripted reader."""

from __future__ import annotations

import pytest

from beadhive.modules.tracker.application import iterate, snapshot_ids
from beadhive.modules.tracker.contracts import (
    IdPage,
    IdQuery,
    IdSubject,
    IssueKey,
    Page,
    PageCursor,
    TransientFailure,
)


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


def test_iterate_follows_cursors_to_the_last_page() -> None:
    seen: list[PageCursor | None] = []

    def fetch(cursor: PageCursor | None) -> Page[int]:
        seen.append(cursor)
        index = int(cursor.token) if cursor else 0
        nxt = PageCursor(str(index + 1)) if index < 2 else None
        return Page((index,), nxt, None)

    pages = list(iterate(fetch))

    assert [page.items for page in pages] == [(0,), (1,), (2,)]
    assert seen == [None, PageCursor("1"), PageCursor("2")]


def test_snapshot_ids_is_complete_and_forwards_the_query() -> None:
    issue = IssueKey("#1", "memory://x/issues/1")
    reader = _ScriptedIds([IdPage(("a", "b"), PageCursor("2")), IdPage(("c",), None)])

    snap = snapshot_ids(reader, IdQuery(IdSubject.COMMENTS, issue=issue, page_size=2))

    assert snap.complete and snap.ids == {"a", "b", "c"}
    assert [q.cursor for q in reader.queries] == [None, PageCursor("2")]
    assert {(q.subject, q.issue, q.page_size) for q in reader.queries} == {
        (IdSubject.COMMENTS, issue, 2)
    }


def test_snapshot_ids_is_incomplete_after_a_port_error_mid_listing() -> None:
    reader = _ScriptedIds([IdPage(("a",), PageCursor("2")), TransientFailure("cut off")])

    snap = snapshot_ids(reader, IdQuery(IdSubject.ISSUES))

    assert not snap.complete and snap.ids == {"a"}  # never infer a deletion from this


def test_snapshot_ids_does_not_swallow_programming_errors() -> None:
    reader = _ScriptedIds([ValueError("a bug, not a tracker answer")])

    with pytest.raises(ValueError, match="a bug"):
        snapshot_ids(reader, IdQuery(IdSubject.ISSUES))
