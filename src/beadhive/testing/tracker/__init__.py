"""Conformance kit and in-memory substitute for the bead tracker adapter port.

``beadhive.testing.tracker`` ships three things, following the ``beadhive.testing.impact``
precedent:

- :class:`MemoryTracker` (``memory``): the in-memory adapter every consumer test substitutes for
  a real one. No HTTP, no clock, no tracker product notion. :func:`without_resolver` hides the
  optional ``IssueResolver``.
- :class:`MemoryWorld` and the :class:`TrackerWorld` Protocol (``world``): the test-only control
  surface for a tracker's server-side state, never reachable through the port.
- :data:`TRACKER_CASES` (``cases``): the reusable conformance cases every adapter must pass, with
  :func:`run_tracker_case` / :func:`assert_tracker_conforms` and :func:`memory_subject`.

Unlike the rest of :mod:`beadhive.testing`, this subpackage imports the tracker module's
contracts and application helpers: it tests that contract, so it needs it. It is imported
explicitly, never from :mod:`beadhive.testing`.
"""

from .cases import (
    MARKER,
    OWNED,
    TRACKER_CASES,
    TrackerCase,
    TrackerFactory,
    TrackerSubject,
    all_comments,
    all_issues,
    assert_contract_only,
    assert_tracker_conforms,
    find_by_listing,
    grant,
    memory_subject,
    run_tracker_case,
)
from .memory import MemoryTracker, without_resolver
from .world import MemoryWorld, TrackerWorld, at

__all__ = [
    "MARKER",
    "OWNED",
    "TRACKER_CASES",
    "MemoryTracker",
    "MemoryWorld",
    "TrackerCase",
    "TrackerFactory",
    "TrackerSubject",
    "TrackerWorld",
    "all_comments",
    "all_issues",
    "assert_contract_only",
    "assert_tracker_conforms",
    "at",
    "find_by_listing",
    "grant",
    "memory_subject",
    "run_tracker_case",
    "without_resolver",
]
