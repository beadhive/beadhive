"""Intake's own pager and snapshot loop over the tracker port's contracts.

The port ADR's direction block let intake import ``tracker.application``, which the checker
forbids (``_direction_allowed``) and which gap G5 keeps forbidden: intake owns these ~20 lines
instead (external-intake-module-adr.md section 2). Semantics match the tracker's helpers: a
snapshot is ``complete=False`` after ANY port error, so a deletion is never inferred from a
listing that failed part-way.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar

from beadhive.modules.tracker.contracts import (
    IdQuery,
    IdSnapshot,
    Page,
    PageCursor,
    TrackerError,
    TrackerReader,
)

T = TypeVar("T")


def iterate_pages(fetch: Callable[[PageCursor | None], Page[T]]) -> Iterator[Page[T]]:
    """Yield every page, starting with ``fetch(None)``, until a page has no ``next_cursor``."""
    cursor: PageCursor | None = None
    while True:
        page = fetch(cursor)
        yield page
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def collect_snapshot(reader: TrackerReader, query: IdQuery) -> IdSnapshot:
    """Collect a full id set; any port error leaves ``complete=False``."""
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


__all__ = ["collect_snapshot", "iterate_pages"]
