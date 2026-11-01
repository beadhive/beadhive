"""Hive apply passes over the neutral source strategies: ``commits``, ``docs`` and ``planning``.

``bh backfill commits|docs|planning`` (``docs/design/backfill-platform-adr.md`` section 2). The
neutral strategies (:mod:`beadlog_sources`: ``git-commits``, ``docs``, ``planning``) read a
repository and propose BeadLog events against a baseline; reconcile classifies most of what they
propose as DRIFT, because the bead already exists: the ``git.commits`` appends, the ``external_ref``
stamps and a close that arrives after the bead was created. The neutral ``bh backfill apply``
applies only NEW. Each pass here wraps one strategy and declares ``drift_applicable``, so the hive
envelope (:class:`~beadhive_backfill.BackfillEngine`: plan hash, drift gate, pre-state, applying
audit, verification, rollback) applies DRIFT and NEW alike.

=============  ==================  ================================  ===========================
pass           strategy            applies                           invalidated by a change to
=============  ==================  ================================  ===========================
``commits``    ``git-commits``     ``set`` of ``git.commits``        which beads exist, their
                                   (appended shas, DRIFT)            ``git.commits``
``docs``       ``docs``            ``set`` of ``external_ref``       which beads exist, their
                                   (DRIFT); a judged NEW doc is a    ``external_ref`` (judging on:
                                   ``create`` (NEW)                  also type, title, description)
``planning``   ``planning``        ``create``, edges, close (NEW);   which beads exist, their
                                   a later close is DRIFT            ``external_ref`` and phase
=============  ==================  ================================  ===========================

**Options.** A pass takes the strategy's own options as strings (``rev``, ``since``, ``docs``,
``labels``, ``planning`` ...). They are recorded in the plan report, and ``commits`` pins ``rev``
to the commit it names, so the apply (and its second dry run) re-reads exactly the planned
history even when the branch moved in between.

**Judge.** Off unless the caller passes ``judging=True`` (root wires ``backfill.judge``): without
it ``docs`` hands the strategy no judge, so every unmatched doc is an abstain residual
(``RESIDUAL <doc>``) for a human to confirm. Whether the pass judges is part of its recorded
settings, so a plan made with a judge is refused by a pass built without one.

**Idempotent.** Event ids are content-addressed and every strategy proposes nothing once its
output is in the baseline, so the engine's second dry run (recorded judgments only) finds zero
changes.

Nothing here imports ``beadhive``: the root hive wires the StoreLoader and the CLI.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from beadlog import BeadEvent, BeadRef, BeadState
from beadlog_sources import (
    DocsStrategy,
    ExtractRequest,
    GitCommitsStrategy,
    JudgmentPort,
    PlanningStrategy,
)

from ._errors import BackfillError

PASS_SCHEMA_VERSION = "1"
PASS_COMMITS = "commits"
PASS_DOCS = "docs"
PASS_PLANNING = "planning"
SOURCE_PASS_NAMES = (PASS_COMMITS, PASS_DOCS, PASS_PLANNING)

_BEAD_FACETS = frozenset({"exists", "deleted", "erased"})
# What the strategy reads from the baseline besides which beads exist. Docs reads a bead's type,
# title and description only to shortlist candidates for a judge, so those join the projection
# only when the pass judges.
_JUDGE_FACETS = frozenset({"type", "fields.title", "fields.description"})
_COMMON_OPTIONS = frozenset({"allow_shallow", "git"})


def parse_options(items: Iterable[str]) -> dict[str, str]:
    """``key=value`` items (``--option``) as a mapping; a malformed item raises."""
    options: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise BackfillError(f"--option {item!r} is not key=value")
        options[key.strip()] = value
    return options


class SourcePass:
    """A hive pass over one neutral source strategy (module doc). Subclasses name the strategy
    and the baseline facets it reads; DRIFT is applicable.

    ``root`` is the checkout the strategy reads, ``group`` the durable bead group id events are
    minted in, ``options`` the strategy options (strings) and ``judging`` whether an enabled
    judge is handed to the strategy.
    """

    name: ClassVar[str]
    schema_version = PASS_SCHEMA_VERSION
    drift_applicable = True
    allowed_options: ClassVar[frozenset[str]]
    facets: ClassVar[frozenset[str]]
    judge_facets: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        root: Path | str,
        group: str,
        *,
        options: Mapping[str, str] | None = None,
        judging: bool = False,
    ) -> None:
        self.root = Path(root)
        self.group = group
        self.judging = bool(judging)
        chosen = {str(key): str(value) for key, value in (options or {}).items()}
        unknown = sorted(set(chosen) - self.allowed_options)
        if unknown:
            raise BackfillError(
                f"{self.name}: unknown option(s) {', '.join(unknown)}; "
                f"known: {', '.join(sorted(self.allowed_options))}"
            )
        self.options = self.pin(chosen)
        self._report: Mapping[str, Any] | None = None

    # -- pass contract -----------------------------------------------------------------------

    @property
    def invalidating_fields(self) -> frozenset[str]:
        return self.facets | (self.judge_facets if self.judging else frozenset())

    def pin(self, options: dict[str, str]) -> dict[str, str]:
        """Options resolved to what must not move between plan and apply (default: as given)."""
        return options

    def strategy(self) -> Any:
        raise NotImplementedError

    def request(
        self, baseline: Sequence[BeadEvent], extracted_at: str, judge: JudgmentPort | None
    ) -> ExtractRequest:
        return ExtractRequest(
            root=self.root,
            group=self.group,
            extracted_at=extracted_at,
            baseline=tuple(baseline),
            options=dict(self.options),
            judge=judge if self.judging else None,
        )

    def propose(
        self,
        baseline: Sequence[BeadEvent],
        *,
        extracted_at: str,
        judge: JudgmentPort,
    ) -> tuple[BeadEvent, ...]:
        strategy = self.strategy()
        events = tuple(strategy.extract(self.request(baseline, extracted_at, judge)))
        self._report = strategy.report()
        return events

    def report(
        self, states: Mapping[BeadRef, BeadState], proposed: Sequence[BeadEvent]
    ) -> dict[str, Any]:
        """The plan report: the pass settings, and what the strategy saw and refused to propose
        (``drift`` it will not overwrite, ``residual`` docs a human must confirm)."""
        strategy = dict(self._report or {})
        return {
            "options": dict(self.options),
            "judging": self.judging,
            "beads": len({event.bead for event in proposed}),
            "drift": list(strategy.get("drift") or ()),
            "residual": list(strategy.get("residual") or ()),
            "strategy": strategy,
        }


class CommitsPass(SourcePass):
    """``git.commits`` appends from ``git log`` (strategy ``git-commits``)."""

    name = PASS_COMMITS
    allowed_options = frozenset({"rev", "since"}) | _COMMON_OPTIONS
    facets = _BEAD_FACETS | {"fields.metadata.git.commits"}

    def strategy(self) -> GitCommitsStrategy:
        return GitCommitsStrategy()

    def pin(self, options: dict[str, str]) -> dict[str, str]:
        """``rev`` becomes the commit sha it names: a branch that moves between the plan and
        the apply must not change what the second dry run reads."""
        rev = options.get("rev") or "HEAD"
        binary = options.get("git") or "git"
        try:
            done = subprocess.run(
                [binary, "-C", str(self.root), "rev-parse", "--verify", "--quiet"]
                + [f"{rev}^{{commit}}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise BackfillError(f"commits: cannot run {binary}: {exc}") from exc
        sha = done.stdout.strip()
        if done.returncode != 0 or not sha:
            raise BackfillError(f"commits: {rev!r} does not name a commit in {self.root}")
        return {**options, "rev": sha}


class DocsPass(SourcePass):
    """``external_ref`` stamps linking decision and design docs to beads (strategy ``docs``)."""

    name = PASS_DOCS
    allowed_options = (
        frozenset({"docs", "top_k", "id_prefix", "new_type", "labels"}) | _COMMON_OPTIONS
    )
    facets = _BEAD_FACETS | {"fields.external_ref"}
    judge_facets = _JUDGE_FACETS

    def strategy(self) -> DocsStrategy:
        return DocsStrategy()


class PlanningPass(SourcePass):
    """Epics, issues, edges and closes from a GSD ``.planning`` tree (strategy ``planning``)."""

    name = PASS_PLANNING
    allowed_options = frozenset({"planning", "id_prefix", "labels", "issue_type"}) | _COMMON_OPTIONS
    facets = _BEAD_FACETS | {"phase", "fields.external_ref"}

    def strategy(self) -> PlanningStrategy:
        return PlanningStrategy()


_PASSES: Mapping[str, type[SourcePass]] = {
    PASS_COMMITS: CommitsPass,
    PASS_DOCS: DocsPass,
    PASS_PLANNING: PlanningPass,
}


def source_pass(
    name: str,
    root: Path | str,
    group: str,
    *,
    options: Mapping[str, str] | None = None,
    judging: bool = False,
) -> SourcePass:
    """The hive pass named ``name`` (one of :data:`SOURCE_PASS_NAMES`)."""
    try:
        cls = _PASSES[name]
    except KeyError:
        raise BackfillError(
            f"unknown source pass {name!r}; known: {', '.join(SOURCE_PASS_NAMES)}"
        ) from None
    return cls(root, group, options=options, judging=judging)


def source_pass_for_plan(
    name: str, root: Path | str, group: str, report: Mapping[str, Any]
) -> SourcePass:
    """The pass that made a saved plan, rebuilt from the settings its report recorded (options,
    pinned ``rev``, judging), so the apply re-reads exactly what the plan read."""
    options = report.get("options")
    return source_pass(
        name,
        root,
        group,
        options=options if isinstance(options, Mapping) else None,
        judging=bool(report.get("judging")),
    )


__all__ = [
    "PASS_COMMITS",
    "PASS_DOCS",
    "PASS_PLANNING",
    "PASS_SCHEMA_VERSION",
    "SOURCE_PASS_NAMES",
    "CommitsPass",
    "DocsPass",
    "PlanningPass",
    "SourcePass",
    "parse_options",
    "source_pass",
    "source_pass_for_plan",
]
