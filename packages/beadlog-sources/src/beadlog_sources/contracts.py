"""Contracts of the BeadHive-free producer side: source strategies and judgment.

Shapes only, no behavior (``docs/design/backfill-platform-adr.md`` sections 1 and 4). Source
strategies turn a repository's own history into proposed BeadLog events; the judgment port is
the typed seam for fuzzy calls, shaped like Jev's Choice/Score primitives so a deterministic
engine, a ``bh-judge/1`` command judge or Jev can sit behind it without code changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from beadlog import BeadEvent

# Entry-point group under which installed distributions register source strategies.
ENTRY_POINT_GROUP = "beadlog.sources"

# The option key a ChoiceQuery offers for "none of these"; a Judgment abstains with outcome None.
ABSTAIN = "abstain"

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ExtractRequest:
    """One extraction run of a strategy.

    ``root`` is the repository to read; ``group`` the durable bead group id every emitted
    ``bead.group`` uses; ``extracted_at`` the extractor clock stamped on every event (never part
    of an id); ``baseline`` the target's existing events, which strategies use to resolve bead ids
    (never to decide what is new: that is reconcile's job); ``options`` strategy settings;
    ``judge`` the configured :class:`JudgmentPort` for strategies that resolve a fuzzy residual
    (``None``: no judging, the residual is reported for a human; strategies that never judge
    ignore it).
    """

    root: Path
    group: str
    extracted_at: str
    baseline: tuple[BeadEvent, ...] = ()
    options: Mapping[str, Any] = field(default=_EMPTY, hash=False)
    judge: JudgmentPort | None = field(default=None, hash=False, compare=False)


@runtime_checkable
class SourceStrategy(Protocol):
    """A producer of proposed BeadLog events from one kind of local source.

    Implementations are registered in the :data:`ENTRY_POINT_GROUP` entry-point group. Extracting
    the same history twice must yield the same event ids.
    """

    @property
    def name(self) -> str:
        """Stable strategy name, e.g. ``git-commits``; also the ``extract <name>`` subcommand."""
        ...

    @property
    def version(self) -> str:
        """Strategy version, recorded in plans so a changed strategy is visible."""
        ...

    def extract(self, request: ExtractRequest) -> Iterable[BeadEvent]:
        """Emit proposed events for ``request``; never mutate any store."""
        ...


# The judgment family a query belongs to when the caller names none. Families key the
# per-family auto-accept thresholds: confidence depends on the option count, so thresholds are
# never shared between families.
DEFAULT_FAMILY = "default"


@dataclass(frozen=True)
class ChoiceQuery:
    """Pick one option. ``options`` maps option key to text and may include :data:`ABSTAIN`.
    ``family`` names the question set (the auto-accept threshold key)."""

    question: str
    options: Mapping[str, str]
    context: str = ""
    family: str = DEFAULT_FAMILY


@dataclass(frozen=True)
class ScoreQuery:
    """Score against ordered criteria (two to ten, best first). ``family`` as for a choice."""

    question: str
    criteria: tuple[str, ...]
    context: str = ""
    family: str = DEFAULT_FAMILY


@dataclass(frozen=True)
class Fallback:
    """Why a judge's answer came from somewhere other than the configured engine.

    ``engine`` is what answered instead (``deterministic-lexical``, or ``abstain`` when a call
    budget is spent); ``reason`` is a stable code (``timeout``, ``exit``, ``malformed``,
    ``version``, ``spawn``, ``max-calls``); ``detail`` is a short human note that never carries
    command output or credentials.
    """

    engine: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class Judgment:
    """A judge's verdict: ``outcome`` is the chosen option or criterion key, or ``None`` to
    abstain. Verdicts are recorded in a plan and never re-queried at apply time. ``fallback``
    is set when the configured engine did not answer (fallback provenance)."""

    outcome: str | None
    probabilities: Mapping[str, float]
    confidence: float
    option_count: int
    rationale: str
    engine: str
    engine_version: str
    model: str | None = None
    fallback: Fallback | None = None


@runtime_checkable
class JudgmentPort(Protocol):
    """The typed seam for fuzzy calls (NEW / PRESENT / NOISE / DRIFT, doc-to-bead match)."""

    def choose(self, query: ChoiceQuery) -> Judgment:
        """Pick one of ``query.options`` or abstain."""
        ...

    def score(self, query: ScoreQuery) -> Judgment:
        """Place the subject on ``query.criteria`` or abstain."""
        ...


__all__ = [
    "ABSTAIN",
    "DEFAULT_FAMILY",
    "ENTRY_POINT_GROUP",
    "ChoiceQuery",
    "ExtractRequest",
    "Fallback",
    "Judgment",
    "JudgmentPort",
    "ScoreQuery",
    "SourceStrategy",
]
