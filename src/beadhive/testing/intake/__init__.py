"""Contract kit and in-memory substitute for the intake module's bead-store port.

``beadhive.testing.intake`` ships, following the ``beadhive.testing.tracker`` precedent:

- :class:`MemoryReportStore` (``memory_store``): the in-memory ``ReportStore`` every consumer test
  substitutes for a real one. No bd, no filesystem, no clock.
- :data:`REPORT_STORE_CASES` (``cases``): the reusable contract cases every ``ReportStore`` must
  pass, with :func:`run_report_store_case` / :func:`assert_report_store_conforms`.
- :class:`InProcessIntakeLock`, :class:`MemoryCheckpointStore` and :class:`MemoryOutboxView`
  (``lock``): a never-waiting in-process ``IntakeLock`` with a settable epoch, a recording
  ``CheckpointStore`` and an outbox read view over plain sets.
- :class:`SequentialIdSource`, :class:`RecordingSanitizer` and :class:`MemoryOutboxIndex`
  (``outbox``): a deterministic ``IdSource``, a recording ``Sanitizer`` and a counting
  ``OutboxIndex`` for the outbox.
- :class:`FakeReleaseVerifier` (``release``): a scripted ``ReleaseVerifier`` (tags and ancestry
  as plain data) for the milestone triggers.
- :class:`MemoryWriteLedger` and :class:`FakePacerClock` (``pacing``): the outbox write pacer's
  host ledger as a shared list, and its clocks and wait moving only when it waits (``bh-eqtxv``).
- :class:`FakeRepoFacts` (``repo_facts``): a scripted ``RepoFacts`` (branch ancestry, releases in
  version order and changed paths as plain data) for the proposal checks, and
  :class:`FakeLandedCommits` (``repo_facts``): a scripted ``LandedCommits`` for apply.

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
    proposal,
    run_report_store_case,
    thread,
)
from .lock import InProcessIntakeLock, MemoryCheckpointStore, MemoryOutboxView
from .memory_store import MemoryReportStore
from .outbox import MemoryOutboxIndex, RecordingSanitizer, SequentialIdSource
from .pacing import FakePacerClock, MemoryWriteLedger
from .release import FakeReleaseVerifier
from .repo_facts import FakeLandedCommits, FakeRepoFacts

__all__ = [
    "REPORT_STORE_CASES",
    "SCOPE",
    "URL",
    "FakeLandedCommits",
    "FakePacerClock",
    "FakeReleaseVerifier",
    "FakeRepoFacts",
    "InProcessIntakeLock",
    "MemoryCheckpointStore",
    "MemoryOutboxIndex",
    "MemoryOutboxView",
    "MemoryReportStore",
    "MemoryWriteLedger",
    "RecordingSanitizer",
    "ReportStoreCase",
    "ReportStoreFactory",
    "SequentialIdSource",
    "assert_report_store_conforms",
    "draft",
    "memory_store",
    "proposal",
    "run_report_store_case",
    "thread",
]
