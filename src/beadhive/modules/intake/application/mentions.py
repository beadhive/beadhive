"""The bound project's issues for the pre-apply mention check (``bh-iamru``).

:class:`TrackerProjectIssues` implements the contracts' ``ProjectIssues`` over the tracker port:
ONE paged listing per call, open and closed issues alike, so a triage run that mentions any number
of ``#N`` costs one listing. With the optional ``IssueResolver`` the listing is the full id
snapshot (``list_ids``, which a tracker never narrows to open issues) resolved without a request;
without one it is the issue listing with closed issues included. A listing that fails part-way is
refused (``IntakeRefused(TRACKER_ERROR)``), never answered partially, so no mention is refused as
missing on an incomplete read. The tracker may come as a factory, so a run that mentions nothing
never binds or reads it.
"""

from __future__ import annotations

from collections.abc import Callable

from beadhive.modules.tracker.contracts import (
    IdQuery,
    IdSubject,
    IssueResolver,
    TrackerError,
    TrackerReader,
)

from ..domain import IntakeRefused, RefusalReason
from .labels import issue_index
from .paging import collect_snapshot

#: The page size of the one listing (the most a tracker usually serves per page).
PAGE_SIZE = 100


class TrackerProjectIssues:
    """``ProjectIssues`` over one tracker (or a factory that binds it on first use)."""

    def __init__(self, tracker: TrackerReader | Callable[[], TrackerReader]) -> None:
        self._source = tracker

    def _tracker(self) -> TrackerReader:
        source = self._source
        return source if isinstance(source, TrackerReader) else source()

    def identifiers(self) -> dict[str, str]:
        tracker = self._tracker()
        if isinstance(tracker, IssueResolver):
            snapshot = collect_snapshot(tracker, IdQuery(IdSubject.ISSUES, page_size=PAGE_SIZE))
            if not snapshot.complete:
                raise IntakeRefused(
                    RefusalReason.TRACKER_ERROR, "the issue listing failed part-way"
                )
            found: dict[str, str] = {}
            for url in snapshot.ids:
                try:
                    key = tracker.resolve(url)
                except TrackerError:
                    continue  # not an issue this project's grammar names
                found[key.identifier] = key.url
            return found
        try:
            index = issue_index(tracker, include_closed=True)
        except TrackerError as exc:
            raise IntakeRefused(
                RefusalReason.TRACKER_ERROR, f"the issue listing failed: {type(exc).__name__}"
            ) from exc
        return {issue.key.identifier: issue.key.url for issue in index.values()}


__all__ = ["PAGE_SIZE", "TrackerProjectIssues"]
