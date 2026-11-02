"""The advisory reading of a cached coverage state (``docs/BACKFILL.md`` "Coverage").

``bh doctor`` and ``bh hive ready`` surface link coverage without paying for it: they read the
cache file once through :func:`beadhive_backfill.coverage.check_cache` with no inputs (so the
state is :attr:`~beadhive_backfill.coverage.CacheState.UNVERIFIED`, never a freshness claim) and
hand the result to :func:`evaluate`. No bd export, no ``git log``, no doc walk, no clock.

What the cheap read can and cannot say:

``ok`` / ``warn``
    The cache is readable and was computed with the settings now configured. ``warn`` when some
    ratio is below ``warn_below`` (per ratio; a ratio over an empty denominator has no value and
    never warns). A warning is advice: the callers never turn it into a failure.
``never``
    There is no cache file ("never computed"): the hint is to run ``--refresh``.
``stale``
    The cached figures cannot be trusted as the hive's current coverage, and so are not shown:
    the file is unreadable, corrupt or of a version this build does not read, or it was computed
    with different ``code_types`` / ``doc_roots`` than the config now says. Whether the *inputs*
    (beads, commits, docs) moved since is exactly what the cheap read cannot know; only
    ``bh backfill coverage`` compares the inputs hash, and it reports that finer ``stale``.

Library rules: pure, no ``beadhive`` import, no I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .coverage import (
    RATIOS,
    CacheCheck,
    CacheState,
    CoverageSettings,
)

DEFAULT_WARN_BELOW = 0.60
REFRESH_HINT = "run `bh backfill coverage --refresh`"
_LABELS = {
    "closed_beads_with_commits": "beads with commits",
    "commits_resolving_to_bead": "commits with beads",
    "docs_linked_to_bead": "docs with beads",
}


class AdvisoryState(StrEnum):
    """The advisory verdict on a cached coverage state."""

    OK = "ok"
    WARN = "warn"  # at least one ratio is below the threshold
    NEVER = "never"  # no cache yet: never computed
    STALE = "stale"  # the cache cannot be presented as current


@dataclass(frozen=True)
class RatioReading:
    """One cached ratio against the threshold; ``below`` is never true for an empty ratio."""

    key: str
    linked: int
    total: int
    value: float | None
    below: bool

    @property
    def label(self) -> str:
        return _LABELS[self.key]

    @property
    def percent(self) -> str:
        return "n/a" if self.value is None else f"{self.value * 100:.1f}%"

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "linked": self.linked,
            "total": self.total,
            "ratio": None if self.value is None else round(self.value, 6),
            "below": self.below,
        }


@dataclass(frozen=True)
class CoverageAdvisory:
    """What an advisory surface renders: the verdict, the readings and a remedy."""

    state: AdvisoryState
    warn_below: float
    readings: tuple[RatioReading, ...] = ()
    computed_at: str | None = None
    detail: str = ""
    hint: str = ""

    @property
    def below(self) -> tuple[RatioReading, ...]:
        return tuple(r for r in self.readings if r.below)

    def summary(self) -> str:
        """One line: the figures, the below-threshold ones marked ``!``, or why there are none."""
        if not self.readings:
            return f"{self.detail}; {self.hint}" if self.hint else self.detail
        figures = ", ".join(
            f"{r.label} {r.percent} ({r.linked}/{r.total}){' !' if r.below else ''}"
            for r in self.readings
        )
        tail = f"; below {self.warn_below * 100:.0f}%: {self.hint}" if self.below else ""
        return f"{figures} [cached {self.computed_at}]{tail}"

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "warn_below": self.warn_below,
            "computed_at": self.computed_at,
            "detail": self.detail,
            "hint": self.hint,
            "ratios": [r.to_json() for r in self.readings],
        }


def _settings_key(settings: CoverageSettings) -> Mapping[str, Any]:
    return settings.to_json()


def evaluate(
    check: CacheCheck,
    *,
    warn_below: float = DEFAULT_WARN_BELOW,
    settings: CoverageSettings | None = None,
) -> CoverageAdvisory:
    """The advisory verdict on a cache read.

    ``settings`` are the currently configured ones; a cache measured with other ``code_types`` or
    ``doc_roots`` is stale. ``check`` normally comes from ``check_cache(path)`` (no inputs, so
    ``unverified``); a ``stale`` or ``fresh`` check, when a caller does have the inputs, is
    honoured as is."""
    if check.state is CacheState.MISSING:
        return CoverageAdvisory(
            AdvisoryState.NEVER, warn_below, detail="never computed", hint=REFRESH_HINT
        )
    report = check.report
    if check.state is CacheState.INVALID or report is None:
        return CoverageAdvisory(
            AdvisoryState.STALE,
            warn_below,
            detail=f"stale: unusable cache ({check.detail})",
            hint=REFRESH_HINT,
        )
    if check.state is CacheState.STALE:
        return CoverageAdvisory(
            AdvisoryState.STALE,
            warn_below,
            computed_at=report.computed_at,
            detail=f"stale: {check.detail}",
            hint=REFRESH_HINT,
        )
    if settings is not None and _settings_key(settings) != _settings_key(report.settings):
        return CoverageAdvisory(
            AdvisoryState.STALE,
            warn_below,
            computed_at=report.computed_at,
            detail="stale: computed with other code_types/doc_roots than configured",
            hint=REFRESH_HINT,
        )
    readings = []
    for key in RATIOS:
        ratio = report.ratios[key]
        value = ratio.value
        readings.append(
            RatioReading(
                key, ratio.linked, ratio.total, value, value is not None and value < warn_below
            )
        )
    state = AdvisoryState.WARN if any(r.below for r in readings) else AdvisoryState.OK
    return CoverageAdvisory(
        state,
        warn_below,
        tuple(readings),
        report.computed_at,
        hint=REFRESH_HINT if state is AdvisoryState.WARN else "",
    )


__all__ = [
    "DEFAULT_WARN_BELOW",
    "REFRESH_HINT",
    "AdvisoryState",
    "CoverageAdvisory",
    "RatioReading",
    "evaluate",
]
