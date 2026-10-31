"""BeadHive-free BeadLog producers (``docs/design/backfill-platform-adr.md`` section 2).

A **library** package: it depends only on ``beadlog`` (through its public ``__all__``) and
never imports ``beadhive``. It will own the source strategies (entry-point group
``beadlog.sources``), reconcile against a baseline BeadLog, the judgment engines and the
standalone ``beadlog`` CLI. This scaffold carries the contracts only: see
:mod:`beadlog_sources.contracts`.
"""

from __future__ import annotations

from .contracts import (
    ABSTAIN,
    ENTRY_POINT_GROUP,
    ChoiceQuery,
    ExtractRequest,
    Judgment,
    JudgmentPort,
    ScoreQuery,
    SourceStrategy,
)

DISTRIBUTION = "beadlog-sources"


def distribution_name() -> str:
    """Return this package's distribution name."""
    return DISTRIBUTION


__all__ = [
    "ABSTAIN",
    "DISTRIBUTION",
    "ENTRY_POINT_GROUP",
    "ChoiceQuery",
    "ExtractRequest",
    "Judgment",
    "JudgmentPort",
    "ScoreQuery",
    "SourceStrategy",
    "distribution_name",
]
