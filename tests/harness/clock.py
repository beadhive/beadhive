"""Deterministic test clock on the ``StateClock`` seam (bh-j4v0l).

One object, three views of the same virtual time: ``now()`` (the ``StateClock`` wall view),
``wall()`` (epoch seconds) and ``monotonic()`` (never decreases). ``advance()`` moves time
forward; ``sleep()`` and ``wait()`` advance it instead of blocking, so loops that wait on a
clock finish instantly and reproducibly.

See docs/TEST_PROCESS_POLICY.md for when to use this vs ``time-machine`` or data-driven expiry.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

_DEFAULT_START = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """Satisfies ``beadhive.modules.state.contracts.ports.StateClock``."""

    def __init__(self, start: datetime = _DEFAULT_START, *, monotonic_start: float = 1000.0):
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be timezone-aware")
        self._now = start
        self._monotonic = float(monotonic_start)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def wall(self) -> float:
        return self._now.timestamp()

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float | timedelta) -> None:
        """Move both views forward; time never runs backwards."""
        delta = seconds.total_seconds() if isinstance(seconds, timedelta) else float(seconds)
        if not math.isfinite(delta) or delta < 0:
            raise ValueError("FakeClock can only advance by a finite, non-negative amount")
        self._now += timedelta(seconds=delta)
        self._monotonic += delta

    def sleep(self, seconds: float) -> None:
        """Advance instead of blocking; records the request in ``sleeps``."""
        self.sleeps.append(float(seconds))
        self.advance(max(0.0, float(seconds)))

    def wait(self, seconds: float) -> bool:
        """``SessionRenewer.run(stop=...)`` shape: advance, never stop (returns False)."""
        self.sleep(seconds)
        return False

    def stop_after(self, ticks: int):
        """A ``stop`` callable that advances by the wait and returns True after ``ticks`` calls."""
        remaining = ticks

        def stop(wait_s: float) -> bool:
            nonlocal remaining
            self.sleep(wait_s)
            remaining -= 1
            return remaining <= 0

        return stop
