"""Contract kit and in-memory substitute for the intake module's bead-store port.

``beadhive.testing.intake`` ships, following the ``beadhive.testing.tracker`` precedent:

- :class:`MemoryReportStore` (``memory_store``): the in-memory ``ReportStore`` every consumer test
  substitutes for a real one. No bd, no filesystem, no clock.
- :data:`REPORT_STORE_CASES` (``cases``): the reusable contract cases every ``ReportStore`` must
  pass, with :func:`run_report_store_case` / :func:`assert_report_store_conforms`.
- :class:`InProcessIntakeLock`, :class:`MemoryCheckpointStore` and :class:`MemoryOutboxView`
  (``lock``): a never-waiting in-process ``IntakeLock`` with a settable epoch, a recording
  ``CheckpointStore`` and an outbox read view over plain sets.
- :class:`SequentialIdSource` and :class:`RecordingSanitizer` (``outbox``): a deterministic
  ``IdSource`` and a recording ``Sanitizer`` for the outbox.
- :class:`FakeReleaseVerifier` (``release``): a scripted ``ReleaseVerifier`` (tags and ancestry
  as plain data) for the milestone triggers.

It imports the intake module's contracts, so it is imported explicitly, never from
:mod:`beadhive.testing`.
"""

from .cases import (
    REPORT_STORE_CASES,
    SCOPE,
    URL,
    ReportStoreCase,
    ReportStoreFactory,
    assert_report_store_conforms,
    draft,
    memory_store,
    run_report_store_case,
    thread,
)
from .lock import InProcessIntakeLock, MemoryCheckpointStore, MemoryOutboxView
from .memory_store import MemoryReportStore
from .outbox import RecordingSanitizer, SequentialIdSource
from .release import FakeReleaseVerifier

__all__ = [
    "REPORT_STORE_CASES",
    "SCOPE",
    "URL",
    "FakeReleaseVerifier",
    "InProcessIntakeLock",
    "MemoryCheckpointStore",
    "MemoryOutboxView",
    "MemoryReportStore",
    "RecordingSanitizer",
    "ReportStoreCase",
    "ReportStoreFactory",
    "SequentialIdSource",
    "assert_report_store_conforms",
    "draft",
    "memory_store",
    "run_report_store_case",
    "thread",
]
