"""BeadHive-free BeadLog producers (``docs/design/backfill-platform-adr.md`` section 2).

A **library** package: it depends only on ``beadlog`` (through its public ``__all__``) and
never imports ``beadhive``. It owns:

- the contracts (:mod:`beadlog_sources.contracts`): source strategies (entry-point group
  ``beadlog.sources``) and the judgment port shaped like Jev's Choice/Score primitives;
- the judgment engines (:mod:`beadlog_sources.judges`): the deterministic lexical judge, the
  ``bh-judge/1`` command judge that fails closed, and :class:`JudgeSettings` (the
  ``backfill.judge`` keys);
- the neutral ``bd`` baseline strategy (:mod:`beadlog_sources.bd`): one ``bd export`` read as
  current-state events, re-emitting the BeadLog ids a bd loader stored in ``metadata.bdlg``;
- the neutral ``git-commits`` strategy (:mod:`beadlog_sources.commits`): the two-step bead-id
  matcher over a repository's git history, proposing append-only ``git.commits`` events for the
  beads of a baseline;
- the neutral ``docs`` strategy (:mod:`beadlog_sources.docs`): decision and design docs bridged to
  beads (frontmatter ``Beads:``, the first-add commit's bead id, existing ``external_ref``), then a
  lexical shortlist and the judged residual, proposing ``external_ref`` stamps;
- the neutral ``planning`` strategy (:mod:`beadlog_sources.planning`): a GSD ``.planning`` tree as
  epics, issues, ``parent-child`` and ``blocks`` edges, idempotent on ``external_ref``;
- reconcile against a baseline (:mod:`beadlog_sources.reconcile`): NEW / PRESENT / NOISE /
  DRIFT plans written as ``.bdlg`` archives;
- the standalone ``beadlog`` CLI (:mod:`beadlog_sources.cli`) and the opt-in reference judge
  command ``bh-judge-haiku`` (:mod:`beadlog_sources.haiku`, ``[haiku]`` extra).
"""

from __future__ import annotations

from .bd import BdBaselineStrategy, BdReadError, baseline_events
from .commits import GitCommitsStrategy, ShallowCloneError
from .contracts import (
    ABSTAIN,
    DEFAULT_FAMILY,
    ENTRY_POINT_GROUP,
    ChoiceQuery,
    ExtractRequest,
    Fallback,
    Judgment,
    JudgmentPort,
    ScoreQuery,
    SourceStrategy,
)
from .docs import DocsStrategy
from .judges import (
    CommandJudge,
    CommandResult,
    DeterministicJudge,
    JudgeProtocolError,
    JudgeSettings,
    build_judge,
    choice_confidence,
    judge_schema,
    settings_from,
)
from .planning import PlanningStrategy
from .reconcile import (
    PlanEntry,
    ReconcileClass,
    ReconcilePlan,
    Review,
    Verdict,
    plan_entries,
    prior_verdicts,
    read_plan,
    reconcile,
    write_plan,
)

DISTRIBUTION = "beadlog-sources"


def distribution_name() -> str:
    """Return this package's distribution name."""
    return DISTRIBUTION


__all__ = [
    "ABSTAIN",
    "BdBaselineStrategy",
    "BdReadError",
    "DEFAULT_FAMILY",
    "DISTRIBUTION",
    "ENTRY_POINT_GROUP",
    "ChoiceQuery",
    "CommandJudge",
    "CommandResult",
    "DeterministicJudge",
    "DocsStrategy",
    "ExtractRequest",
    "Fallback",
    "GitCommitsStrategy",
    "JudgeProtocolError",
    "JudgeSettings",
    "Judgment",
    "JudgmentPort",
    "PlanEntry",
    "PlanningStrategy",
    "ReconcileClass",
    "ReconcilePlan",
    "Review",
    "ScoreQuery",
    "ShallowCloneError",
    "SourceStrategy",
    "Verdict",
    "baseline_events",
    "build_judge",
    "choice_confidence",
    "distribution_name",
    "judge_schema",
    "plan_entries",
    "prior_verdicts",
    "read_plan",
    "reconcile",
    "settings_from",
    "write_plan",
]
