"""Spreading hive primaries across executors (bh-zncqo, M8b; ADR section 3).

A primary outage stops hive writes for every forwarder of that hive until failover or recovery,
so placement spreads primaries: one frame's death stalls only the hives it holds. Pure functions
over plain data (no SQL, no clock) so the rule is unit-testable and shared by the director's
failover loop (:func:`pick_successor`) and ``bh doctor`` (:func:`spread_report`).

Placed hives are never moved just to rebalance; a move is a deliberate CAS. The spread rule only
decides *where a hive goes when it has to move*, and doctor only *reports* drift.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_MAX_SPREAD",
    "SpreadReport",
    "pick_successor",
    "primary_holdings",
    "spread_report",
    "validate_max_spread",
]

#: Doctor warns when the busiest eligible executor holds more than this many primaries over the
#: least busy one. 2 tolerates the one-off imbalance that never-rebalance drift produces;
#: configurable as ``host.daemon.failover.max_primary_spread``.
DEFAULT_MAX_SPREAD = 2


def validate_max_spread(value: object) -> int:
    """The threshold, refusing anything but a positive integer (never clamped)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"max_primary_spread must be an integer >= 1, got {value!r}")
    return value


def primary_holdings(
    placements: Mapping[str, object], frames: Iterable[str] = ()
) -> dict[str, tuple[str, ...]]:
    """``frame -> sorted hive prefixes`` from placement records (anything with ``frame_id``;
    a released or unknown row holds nothing). Every frame in `frames` appears, with ``()`` when
    it holds no primary."""
    held: dict[str, list[str]] = {frame: [] for frame in frames}
    for prefix, record in placements.items():
        frame = getattr(record, "frame_id", "") or ""
        if frame:
            held.setdefault(frame, []).append(prefix)
    return {frame: tuple(sorted(hives)) for frame, hives in held.items()}


def pick_successor(candidates: Sequence[tuple[str, float]], load: Mapping[str, int]) -> str | None:
    """The spreading rule over eligible `(frame, session_age)` candidates: the frame holding the
    fewest primaries, then the freshest session, then the lowest frame id. ``None`` when there
    is no candidate."""
    if not candidates:
        return None
    return min(candidates, key=lambda c: (load.get(c[0], 0), c[1], c[0]))[0]


@dataclass(frozen=True)
class SpreadReport:
    """Primaries per executor against the configured spread threshold."""

    holdings: Mapping[str, tuple[str, ...]]
    threshold: int

    @property
    def counts(self) -> dict[str, int]:
        return {frame: len(hives) for frame, hives in sorted(self.holdings.items())}

    @property
    def spread(self) -> int:
        counts = self.counts.values()
        return max(counts) - min(counts) if counts else 0

    @property
    def lopsided(self) -> bool:
        return self.spread > self.threshold

    def describe(self) -> str:
        """One line naming the executors and hives; empty when balanced."""
        if not self.lopsided:
            return ""
        parts = [
            f"{frame} holds {len(hives)} ({', '.join(hives) or 'none'})"
            for frame, hives in sorted(self.holdings.items())
        ]
        return (
            f"hive primaries are lopsided: spread {self.spread} exceeds "
            f"{self.threshold} (host.daemon.failover.max_primary_spread); " + "; ".join(parts)
        )

    def as_dict(self) -> dict:
        return {
            "counts": self.counts,
            "hives": {frame: list(hives) for frame, hives in sorted(self.holdings.items())},
            "spread": self.spread,
            "threshold": self.threshold,
            "lopsided": self.lopsided,
            "detail": self.describe(),
        }


def spread_report(holdings: Mapping[str, Sequence[str]], threshold: int) -> SpreadReport:
    return SpreadReport({f: tuple(h) for f, h in holdings.items()}, validate_max_spread(threshold))
