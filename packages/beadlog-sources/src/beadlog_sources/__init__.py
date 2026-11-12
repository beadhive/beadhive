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
  beads of a baseline (its matcher primitives and the ``git.commits`` value parser are public, so
  other libraries measure linkage with the same matcher);
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
- the evidence IR (:mod:`beadlog_sources.evidence`): :class:`Artifact` and :class:`Ref` source
  records typed by the closed :class:`Category` set (each with a validated required-attrs
  shape) and the ``bdlg.evidence/1`` JSONL corpus (:class:`EvidenceCorpus`), content-addressed
  and byte-stable; synthetic corpora come from :mod:`beadlog_sources.testing.evidence`;
- the reader port and discovery (:mod:`beadlog_sources.readers`): :class:`SourceReader`,
  :class:`ReadRequest`, the ``beadlog.readers``/``beadlog.sources`` entry-point groups merged with
  a static built-in table so discovery never depends on the installed distribution;
- the ledger directory port (:mod:`beadlog_sources.ledger`): which registered ledger owns a
  bead-id token, by exact id-set membership only (never a prefix guess), local / foreign /
  ambiguous / unresolved; test fake in :mod:`beadlog_sources.testing.ledger`;
- the ``git`` reader (:mod:`beadlog_sources.git_reader`): commits, merges and tags as ``change`` /
  ``release-marker`` artifacts from two ``git`` calls per read, with a hand-written Conventional
  Commits and trailer parse and un-resolved refs;
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
from .commits import (
    GitCommitsStrategy,
    ShallowCloneError,
    candidate_pattern,
    derive_namespaces,
    extract_candidates,
    parse_linkage,
    resolve_candidates,
)
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

# --- docs reader (bh-4r0uh) ---
from .doc_formats import DocFormat, HtmlFormat, MarkdownFormat, ParsedDoc, Section
from .docs import DocsStrategy
from .docs_reader import LAYOUT_PRESETS, DocsReader
from .evidence import (
    CATEGORY_SHAPES,
    DECLARED_STATUSES,
    EVIDENCE_SCHEMA,
    READER_PROVENANCES,
    REF_KINDS,
    Artifact,
    ArtifactShapeError,
    AttrType,
    Category,
    CategoryShape,
    EvidenceCorpus,
    EvidenceFormatError,
    ReaderHeader,
    Ref,
    artifact_id,
    build_corpus,
    dumps_evidence,
    evidence_schema,
    loads_evidence,
    read_evidence,
    write_evidence,
)
from .git_dates import CommitDate, CommitDates, GitReadError, commit_dates
from .git_reader import GitRead, GitReader, parse_conventional, parse_trailers, read_git
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
from .ledger import (
    AMBIGUOUS,
    FOREIGN,
    LOCAL,
    RESOLUTION_KINDS,
    UNRESOLVED,
    CachedLedgerDirectory,
    LedgerDirectory,
    LedgerError,
    Resolution,
    resolve,
    resolve_ref,
)
from .planning import PlanningStrategy
from .readers import (
    BUILTIN_READERS,
    BUILTIN_STRATEGIES,
    READERS_GROUP,
    DuplicateRegistrationError,
    ReadRequest,
    Registration,
    SourceReader,
    discover_readers,
    discover_strategies,
    load_reader,
)
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

# --- end docs reader ---
from .tokens import bead_token_pattern, find_bead_tokens, token_mode

DISTRIBUTION = "beadlog-sources"


def distribution_name() -> str:
    """Return this package's distribution name."""
    return DISTRIBUTION


__all__ = [
    # --- docs reader (bh-4r0uh) ---
    "LAYOUT_PRESETS",
    "DocFormat",
    "DocsReader",
    "MarkdownFormat",
    "ParsedDoc",
    "Section",
    # --- end docs reader ---
    # --- html doc format (bh-rqshf) ---
    "HtmlFormat",
    # --- end html doc format ---
    "BUILTIN_READERS",
    "BUILTIN_STRATEGIES",
    "bead_token_pattern",
    "find_bead_tokens",
    "token_mode",
    "READERS_GROUP",
    "DuplicateRegistrationError",
    "GitRead",
    "GitReader",
    "ReadRequest",
    "Registration",
    "SourceReader",
    "discover_readers",
    "discover_strategies",
    "parse_conventional",
    "parse_trailers",
    "read_git",
    "load_reader",
    "AMBIGUOUS",
    "FOREIGN",
    "LOCAL",
    "RESOLUTION_KINDS",
    "UNRESOLVED",
    "CachedLedgerDirectory",
    "LedgerDirectory",
    "LedgerError",
    "Resolution",
    "resolve",
    "resolve_ref",
    "ABSTAIN",
    "CATEGORY_SHAPES",
    "DECLARED_STATUSES",
    "EVIDENCE_SCHEMA",
    "READER_PROVENANCES",
    "REF_KINDS",
    "Artifact",
    "ArtifactShapeError",
    "AttrType",
    "Category",
    "CategoryShape",
    "EvidenceCorpus",
    "EvidenceFormatError",
    "ReaderHeader",
    "Ref",
    "artifact_id",
    "build_corpus",
    "dumps_evidence",
    "evidence_schema",
    "loads_evidence",
    "read_evidence",
    "write_evidence",
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
    "candidate_pattern",
    "build_judge",
    "choice_confidence",
    "commit_dates",
    "consistent_history",
    "derive_namespaces",
    "extract_candidates",
    "distribution_name",
    "gap_events",
    "gapfill_annotation",
    "history_annotation",
    "history_argv",
    "history_events",
    "judge_schema",
    "lacking_created",
    "parse_history",
    "parse_linkage",
    "plan_entries",
    "prior_verdicts",
    "read_history",
    "read_plan",
    "read_store_start",
    "reconcile",
    "resolve_candidates",
    "settings_from",
    "source_family",
    "subprocess_runner",
    "suppress_artifacts",
    "write_plan",
]
