"""Contract kit and in-memory substitute for the intake module's bead-store port.

``beadhive.testing.intake`` ships, following the ``beadhive.testing.tracker`` precedent:

- :class:`MemoryReportStore` (``memory_store``): the in-memory ``ReportStore`` every consumer test
  substitutes for a real one. No bd, no filesystem, no clock.
- :data:`REPORT_STORE_CASES` (``cases``): the reusable contract cases every ``ReportStore`` must
  pass, with :func:`run_report_store_case` / :func:`assert_report_store_conforms`.

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
)
from .memory_store import MemoryReportStore

__all__ = [
    "REPORT_STORE_CASES",
    "SCOPE",
    "URL",
    "MemoryReportStore",
    "ReportStoreCase",
    "ReportStoreFactory",
    "assert_report_store_conforms",
    "draft",
    "memory_store",
    "run_report_store_case",
]
