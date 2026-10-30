"""BeadHive-specific backfill (``docs/design/backfill-platform-adr.md`` section 2).

A **library** package: it depends only on ``beadlog`` and ``beadlog-sources`` (each through its
public ``__all__``) and never imports ``beadhive``. It will own the hive apply envelope lifted
from ``complexity_backfill`` and the hive-policy passes. This scaffold carries the contracts
only: see :mod:`beadhive_backfill.contracts`.
"""

from __future__ import annotations

from .contracts import (
    AuditEntry,
    AuditProgress,
    AuditRecovery,
    AuditSink,
    AuditState,
    HivePass,
    StoreLoader,
)

DISTRIBUTION = "beadhive-backfill"


def distribution_name() -> str:
    """Return this package's distribution name."""
    return DISTRIBUTION


__all__ = [
    "DISTRIBUTION",
    "AuditEntry",
    "AuditProgress",
    "AuditRecovery",
    "AuditSink",
    "AuditState",
    "HivePass",
    "StoreLoader",
    "distribution_name",
]
