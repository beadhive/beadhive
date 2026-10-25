"""Tracker-neutral paging helpers over the port. Standard library plus this module's contracts.

``iterate`` follows ``next_cursor`` to the last page. ``snapshot_ids`` collects a full id set
and reports ``complete=False`` on any port error, so a consumer never infers a deletion from a
listing that failed part-way.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar

from ..contracts import IdQuery, IdSnapshot, Page, PageCursor, TrackerError, TrackerReader

T = TypeVar("T")


def iterate(fetch: Callable[[PageCursor | None], Page[T]]) -> Iterator[Page[T]]:
    """Yield every page, starting with ``fetch(None)``, until a page has no ``next_cursor``."""
    cursor: PageCursor | None = None
    while True:
        page = fetch(cursor)
        yield page
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def snapshot_ids(reader: TrackerReader, query: IdQuery) -> IdSnapshot:
    """Collect a full id set. Any port error leaves ``complete=False``: never infer a deletion."""
    ids: set[str] = set()
    cursor = query.cursor
    try:
        while True:
            page = reader.list_ids(
                IdQuery(query.subject, query.issue, cursor=cursor, page_size=query.page_size)
            )
            ids.update(page.ids)
            if page.next_cursor is None:
                return IdSnapshot(frozenset(ids), complete=True)
            cursor = page.next_cursor
    except TrackerError:
        return IdSnapshot(frozenset(ids), complete=False)


__all__ = ["iterate", "snapshot_ids"]
