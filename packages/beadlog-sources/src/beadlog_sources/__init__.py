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
- the neutral ``bd`` history reader (:mod:`beadlog_sources.bd_history`): the ``events`` table in
  one bounded ``SELECT``, parsed into typed :class:`HistoryEvent` records with counted skips;
- the neutral history conversion (:mod:`beadlog_sources.bd_timeline`): those records as BeadLog
  history events (create, phase, set, labels, edges, comments) with bi-temporal stamps,
  revert-artifact suppression and a guard that history never changes the exported current state;
- the opt-in, bounded ``dolt_history_issues`` gap fill (:mod:`beadlog_sources.bd_gapfill`):
  snapshot-level history for beads with no ``created`` event, read in chunked ``SELECT`` batches
  with a resumable cursor and emitted as derived, commit-dated, reduced-confidence events;
- commit dates (:mod:`beadlog_sources.git_dates`): author and committer dates of many shas in one
  ``git`` call; the injected command seam is :mod:`beadlog_sources.runner`;
- reconcile against a baseline (:mod:`beadlog_sources.reconcile`): NEW / PRESENT / NOISE /
  DRIFT plans written as ``.bdlg`` archives;
- the standalone ``beadlog`` CLI (:mod:`beadlog_sources.cli`) and the opt-in reference judge
  command ``bh-judge-haiku`` (:mod:`beadlog_sources.haiku`, ``[haiku]`` extra).
"""

from __future__ import annotations

from .bd import BdBaselineStrategy, BdReadError, baseline_events, bd_timestamp
from .bd_gapfill import (
    BdGapfillError,
    GapfillEvents,
    GapfillReader,
    JsonCursorFile,
    gap_events,
    gapfill_annotation,
    lacking_created,
)
from .bd_history import (
    CLAIMED as HISTORY_CLAIMED,
)
from .bd_history import (
    CLOSED as HISTORY_CLOSED,
)
from .bd_history import (
    LABEL_ADDED as HISTORY_LABEL_ADDED,
)
from .bd_history import (
    LABEL_REMOVED as HISTORY_LABEL_REMOVED,
)
from .bd_history import (
    REOPENED as HISTORY_REOPENED,
)
from .bd_history import (
    BdHistoryError,
    HistoryEvent,
    HistoryRead,
    history_argv,
    parse_history,
    read_history,
)
from .bd_timeline import (
    HISTORY_ANNOTATION,
    BdHistoryStrategy,
    BeadRow,
    HistoryEvents,
    bead_rows,
    consistent_history,
    history_annotation,
    history_events,
    read_store_start,
    source_family,
    suppress_artifacts,
)
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
from .git_dates import CommitDate, CommitDates, GitReadError, commit_dates
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
from .runner import DEFAULT_TIMEOUT_SECONDS, Runner, subprocess_runner

DISTRIBUTION = "beadlog-sources"


def distribution_name() -> str:
    """Return this package's distribution name."""
    return DISTRIBUTION


__all__ = [
    "ABSTAIN",
    "BdBaselineStrategy",
    "BdGapfillError",
    "BdHistoryError",
    "BdReadError",
    "DEFAULT_FAMILY",
    "DEFAULT_TIMEOUT_SECONDS",
    "DISTRIBUTION",
    "ENTRY_POINT_GROUP",
    "HISTORY_ANNOTATION",
    "HISTORY_CLAIMED",
    "HISTORY_CLOSED",
    "HISTORY_LABEL_ADDED",
    "HISTORY_LABEL_REMOVED",
    "HISTORY_REOPENED",
    "BdHistoryStrategy",
    "BeadRow",
    "ChoiceQuery",
    "CommandJudge",
    "CommitDate",
    "CommitDates",
    "CommandResult",
    "DeterministicJudge",
    "DocsStrategy",
    "ExtractRequest",
    "Fallback",
    "GapfillEvents",
    "GapfillReader",
    "GitReadError",
    "HistoryEvent",
    "HistoryEvents",
    "HistoryRead",
    "GitCommitsStrategy",
    "JudgeProtocolError",
    "JsonCursorFile",
    "JudgeSettings",
    "Judgment",
    "JudgmentPort",
    "PlanEntry",
    "PlanningStrategy",
    "ReconcileClass",
    "ReconcilePlan",
    "Review",
    "Runner",
    "ScoreQuery",
    "ShallowCloneError",
    "SourceStrategy",
    "Verdict",
    "baseline_events",
    "bd_timestamp",
    "bead_rows",
    "build_judge",
    "choice_confidence",
    "commit_dates",
    "consistent_history",
    "distribution_name",
    "gap_events",
    "gapfill_annotation",
    "history_annotation",
    "history_argv",
    "history_events",
    "judge_schema",
    "lacking_created",
    "parse_history",
    "plan_entries",
    "prior_verdicts",
    "read_history",
    "read_plan",
    "read_store_start",
    "reconcile",
    "settings_from",
    "source_family",
    "subprocess_runner",
    "suppress_artifacts",
    "write_plan",
]
