"""BeadHive-specific backfill (``docs/design/backfill-platform-adr.md`` section 2).

A **library** package: it depends only on ``beadlog`` and ``beadlog-sources`` (each through its
public ``__all__``) and never imports ``beadhive``. It owns the hive apply envelope lifted from
``complexity_backfill``:

- contracts (:mod:`beadhive_backfill.contracts`): :class:`HivePass` and its optional hooks,
  the store ports (:class:`StoreReader`, :class:`StoreLoader`, :class:`PreStateExporter`) and
  the audit (:class:`AuditSink`, :class:`AuditEntry`, ...);
- the engine (:mod:`beadhive_backfill.engine`): :class:`BackfillEngine` ``plan`` / ``apply``
  with plan hash, corpus-drift refusal, pre-state export, checkpointed audit, rollback through
  loader inverses and the recorded-judgments-only idempotency proof;
- plans (:mod:`beadhive_backfill.plan`): :class:`Plan`, a ``.bdlg`` with a manifest ``plan``
  section;
- per-kind rollback inverses (:func:`inverse_of`) and store-neutral sinks
  (:class:`JsonFileAuditSink`, :class:`ArchivePreStateExporter`);
- the hive-policy passes, each its own public submodule: :mod:`beadhive_backfill.complexity`
  (pass #1, complexity labels, with the published JSON plan/audit contract of
  ``bh backfill complexity``);
- the lifecycle timeline pass (:mod:`beadhive_backfill.timeline`, pass #2): AGF conventions over
  the history readers, proposing ``metadata.lifecycle.*`` keys with their confidence;
- link-coverage metrics and their cached state (:mod:`beadhive_backfill.coverage`): the three
  ratios over injected reads, the inputs hash and the versioned cache file;
- the AGF history readers, also public submodules: :mod:`beadhive_backfill.merges` (one
  ``git log --first-parent`` -> ``chore(merge): bead|molecule|batch`` lands, refresh merges
  ignored) and :mod:`beadhive_backfill.run_journals` (the optional ``run-journals/<hive>/`` reader,
  a missing directory being a no-op). The neutral readers (bd events table, commit dates) live
  in ``beadlog_sources``.
"""

from __future__ import annotations

from ._errors import BackfillError
from ._inverse import ROLLBACK_REASON, Stamper, has_inverse, inverse_of
from ._projection import ALL_FACETS, bead_key, projection, projection_hash
from .contracts import (
    RECOVERY_INSTRUCTION,
    AuditEntry,
    AuditProgress,
    AuditRecovery,
    AuditSink,
    AuditState,
    HivePass,
    PreStateExporter,
    ReportingPass,
    ScopedPass,
    StoreLoader,
    StoreReader,
    VerifyingPass,
)
from .engine import BackfillEngine, system_clock
from .plan import ENGINE_ANNOTATION, ENGINE_SCHEMA, Plan
from .sinks import ArchivePreStateExporter, JsonFileAuditSink
from .timeline import PathReaders, TimelinePass, TimelineReaders

DISTRIBUTION = "beadhive-backfill"


def distribution_name() -> str:
    """Return this package's distribution name."""
    return DISTRIBUTION


__all__ = [
    "ALL_FACETS",
    "DISTRIBUTION",
    "ENGINE_ANNOTATION",
    "ENGINE_SCHEMA",
    "RECOVERY_INSTRUCTION",
    "ROLLBACK_REASON",
    "ArchivePreStateExporter",
    "AuditEntry",
    "AuditProgress",
    "AuditRecovery",
    "AuditSink",
    "AuditState",
    "BackfillEngine",
    "BackfillError",
    "HivePass",
    "JsonFileAuditSink",
    "PathReaders",
    "Plan",
    "PreStateExporter",
    "ReportingPass",
    "ScopedPass",
    "Stamper",
    "StoreLoader",
    "StoreReader",
    "TimelinePass",
    "TimelineReaders",
    "VerifyingPass",
    "bead_key",
    "distribution_name",
    "has_inverse",
    "inverse_of",
    "projection",
    "projection_hash",
    "system_clock",
]
