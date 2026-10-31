"""Recorded judgments: a plan records every verdict its pass received, and re-verification
replays them without ever invoking a judge (platform ADR section 4: "Verdicts are recorded in
the plan and are never asked again at apply time")."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from beadlog import canonical_bytes
from beadlog_sources import ABSTAIN, ChoiceQuery, Judgment, JudgmentPort, ScoreQuery, Verdict

RECORDED_ENGINE = "recorded"
RECORDED_VERSION = "1"


def choice_hash(query: ChoiceQuery) -> str:
    """Identity of a choice query (the same material reconcile hashes)."""
    return _material_hash(
        {
            "family": query.family,
            "kind": "choice",
            "question": query.question,
            "options": dict(query.options),
            "context": query.context,
        }
    )


def score_hash(query: ScoreQuery) -> str:
    """Identity of a score query."""
    return _material_hash(
        {
            "family": query.family,
            "kind": "score",
            "question": query.question,
            "criteria": list(query.criteria),
            "context": query.context,
        }
    )


def _material_hash(material: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(material)).hexdigest()


def _abstain(option_count: int, rationale: str) -> Judgment:
    return Judgment(
        outcome=None,
        probabilities={ABSTAIN: 1.0},
        confidence=0.0,
        option_count=option_count,
        rationale=rationale,
        engine=RECORDED_ENGINE,
        engine_version=RECORDED_VERSION,
    )


class JudgeTap:
    """A :class:`~beadlog_sources.JudgmentPort` that serves recorded verdicts first.

    A query whose hash is in ``recorded`` is answered from it. Otherwise, with a ``fallback``
    judge, the fallback answers; without one (replay only) the tap abstains and counts a
    ``miss`` — it never invokes anything. Every verdict served is kept in :attr:`verdicts`.
    """

    def __init__(
        self, recorded: Mapping[str, Verdict], fallback: JudgmentPort | None = None
    ) -> None:
        self._recorded = dict(recorded)
        self._fallback = fallback
        self.verdicts: dict[str, Verdict] = {}
        self.misses = 0

    @property
    def engine(self) -> str:
        return str(getattr(self._fallback, "engine", RECORDED_ENGINE))

    @property
    def engine_version(self) -> str:
        return str(getattr(self._fallback, "engine_version", RECORDED_VERSION))

    @property
    def model(self) -> str | None:
        return getattr(self._fallback, "model", None)

    def _serve(self, family: str, key: str, ask: Any, option_count: int) -> Judgment:
        held = self._recorded.get(key)
        if held is not None:
            judgment = held.judgment
        elif self._fallback is not None:
            judgment = ask()
        else:
            self.misses += 1
            judgment = _abstain(option_count, "no recorded verdict for this query (replay only)")
            return judgment  # a miss is not a verdict: never record it
        self.verdicts[key] = Verdict(family, key, judgment)
        return judgment

    def choose(self, query: ChoiceQuery) -> Judgment:
        fallback = self._fallback
        return self._serve(
            query.family,
            choice_hash(query),
            lambda: fallback.choose(query),  # type: ignore[union-attr]
            len(query.options),
        )

    def score(self, query: ScoreQuery) -> Judgment:
        fallback = self._fallback
        return self._serve(
            query.family,
            score_hash(query),
            lambda: fallback.score(query),  # type: ignore[union-attr]
            len(query.criteria),
        )


def verdicts_to_json(verdicts: Mapping[str, Verdict]) -> list[dict[str, Any]]:
    return [verdicts[key].to_json() for key in sorted(verdicts)]


def verdicts_from_json(items: Iterable[Mapping[str, Any]]) -> dict[str, Verdict]:
    verdicts = (Verdict.from_json(item) for item in items)
    return {verdict.query_hash: verdict for verdict in verdicts}
