"""Opt-in host-daemon timer that runs external-issue discovery on a schedule (bh-df64b, I6).

``intake.external.discovery.enabled`` (default ``false``; external-intake-module-adr.md section 15)
on top of ``intake.external.enabled`` (default ``false``). With both on, every ``interval_seconds``
the daemon runs :func:`beadhive.intake_services.discover` for each managed hive: the same
operation as ``bh work discover``, through the module and its import upsert. The timer holds no
tracker adapter of its own, writes no tracker and no public store, and every Nth run
(``full_reconcile_every``) is a full pass; the rest resume from the checkpoint.

It follows :mod:`beadhive.release_catalog_auto_import`: a plain object whose ``poll`` is one
scheduling decision (driven directly in tests with an injected clock, no sleep), an ``asyncio``
``run`` loop around it and a lifespan ``component``. One failed run never stops the loop, and a
hive that is not bound to a tracker, or whose host is not the primary writer, is skipped.

Enabling the timer without a ``TrackerAdapters`` (``intake_services.process_adapters`` is
``None`` unless ``intake.external.enabled`` and ``plugins.tracker-github.enabled`` are both set)
refuses daemon startup (:class:`DiscoveryRefused`) instead of
running a loop that could only refuse every tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import intake_services, registry

__all__ = ["ACTOR", "DiscoveryRefused", "DiscoveryTimer", "HiveRun", "build_loop"]

_log = logging.getLogger(__name__)

#: The ``--actor`` of the reports a timer run creates (the reporter is the issue author).
ACTOR = "host-daemon/intake-discovery"

#: Refusals that are normal for a hive the timer cannot serve (not bound, intake off for it, or
#: this host is not its primary writer): skipped without a warning.
_QUIET = frozenset({"intake_disabled", "not_primary", "intake_busy"})


class DiscoveryRefused(ValueError):
    """Discovery is enabled but cannot run; the daemon refuses to start."""


@dataclass(frozen=True)
class HiveRun:
    """One hive's result of one timer run."""

    hive: str
    result: intake_services.DiscoverResult


@dataclass
class DiscoveryTimer:
    """The schedule. ``discover(full)`` runs one pass over every hive and returns its results.

    ``clock`` is a monotonic seconds clock and ``sleep`` the loop's async wait, both injectable:
    tests move a fake clock and call :meth:`poll`, and never sleep."""

    discover: Callable[[bool], Sequence[HiveRun]]
    interval: float
    full_reconcile_every: int = 24
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ticks: int = 0
    errors: int = 0
    runs: list[Sequence[HiveRun]] = field(default_factory=list)
    _next_due: float | None = None
    _closed: bool = False

    def due(self) -> bool:
        """Whether a run is owed now (the first poll is always due)."""
        return self._next_due is None or self.clock() >= self._next_due

    def seconds_until_due(self) -> float:
        if self._next_due is None:
            return 0.0
        return max(0.0, self._next_due - self.clock())

    def poll(self) -> Sequence[HiveRun] | None:
        """Run discovery if it is due, else do nothing. Returns the run's results, or ``None``
        when not due. The next run is due ``interval`` after this one STARTED, however it ended."""
        now = self.clock()
        if self._next_due is not None and now < self._next_due:
            return None
        self._next_due = now + self.interval
        self.ticks += 1
        full = self.ticks % self.full_reconcile_every == 0
        try:
            results = tuple(self.discover(full))
        except Exception:  # noqa: BLE001 - one failed run never stops the loop
            self.errors += 1
            _log.exception("intake discovery run failed")
            return ()
        self.runs.append(results)
        for run in results:
            if run.result.code and run.result.reason not in _QUIET:
                self.errors += 1
                _log.warning("intake discovery of %s refused: %s", run.hive, run.result.error)
        return results

    async def run(self) -> None:
        while not self._closed:
            await asyncio.to_thread(self.poll)
            await self.sleep(self.seconds_until_due())

    def component(self) -> Any:
        from .kernel.lifecycle import HostDaemonLifespanComponent, HostDaemonShutdownPhase

        @asynccontextmanager
        async def lifespan(_app: Any):
            task = asyncio.create_task(self.run(), name="intake-discovery")
            try:
                yield
            finally:
                self._closed = True
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        return HostDaemonLifespanComponent(
            name="intake-discovery",
            lifespan=lifespan,
            shutdown_phase=HostDaemonShutdownPhase.CANCEL_PROCESSES,
        )


def _discover_hives(
    cfg: Mapping[str, Any],
    adapters: Any,
    discover: Callable[..., intake_services.DiscoverResult],
) -> Callable[[bool], list[HiveRun]]:
    """One pass over every managed hive. A hive that raises is recorded as a failed run and the
    pass moves on."""

    def run(full: bool) -> list[HiveRun]:
        runs: list[HiveRun] = []
        for entry in registry.hives(cfg):
            key = registry.hive_key(entry)
            try:
                result = discover(
                    cfg,
                    entry,
                    Path(registry.hive_dir(entry)),
                    ACTOR,
                    full=full,
                    adapters=adapters,
                    hive=key,
                )
            except Exception as exc:  # noqa: BLE001 - one hive never stops the others
                _log.exception("intake discovery of %s failed", key)
                result = intake_services.DiscoverResult(1, str(exc), "error", full=full)
            runs.append(HiveRun(key, result))
        return runs

    return run


def build_loop(
    cfg: Mapping[str, Any] | None,
    *,
    adapters: Any = None,
    discover: Callable[..., intake_services.DiscoverResult] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DiscoveryTimer | None:
    """The timer for ``intake.external.discovery``, or ``None`` unless it is enabled (the default).

    Refuses (:class:`DiscoveryRefused`) an enabled timer with no tracker wiring, so the daemon
    never runs without the loop it was configured to run. The keyword seams are for tests."""
    cfg = cfg or {}
    settings = intake_services.discovery_settings(cfg)
    if not settings.enabled:
        return None
    if adapters is None:
        adapters = intake_services.process_adapters(cfg)
    if adapters is None:
        raise DiscoveryRefused(
            "intake.external.discovery.enabled needs a wired tracker adapter: "
            f"{intake_services.TRACKER_CONFIG_HINT}, or leave discovery off"
        )
    return DiscoveryTimer(
        _discover_hives(cfg, adapters, discover or intake_services.discover),
        settings.interval_seconds,
        settings.full_reconcile_every,
        clock,
        sleep,
    )
