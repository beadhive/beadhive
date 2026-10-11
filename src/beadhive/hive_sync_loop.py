"""The host daemon's ``hive.sync`` loop: periodic auto-push and auto-pull (bh-midl0).

Trust-profiles ADR section 5. Today bead state moves only inside verbs; with
``hive.sync.auto_push`` / ``auto_pull`` enabled for a hive, the daemon also moves it on a timer:

* **Push** while anything is pending: on the timer (``auto_push.interval`` since the last push
  attempt), at once after ``auto_push.max_pending`` local commits, and at once for an
  ``--await-sync`` flush request (:func:`beadhive.hive_sync_state.request_flush`). A failed push
  backs off exponentially within ``auto_push.retry_backoff``; nothing a verb does waits on it.
* **Only the writer pushes ``main``.** The route is decided per attempt (:func:`live_route`): the
  hive's writer (or a single-host hive with no writer) pushes ``main`` through
  :meth:`Engine.push_state`, whose managed preflight still checks ``bh_writer`` exactly as a
  manual push does; a replica under the ``open`` write policy publishes only its frame-private
  ``refs/dolt/frame/<id>`` (the same ``push_state`` diverts it); any other non-writer pushes
  nothing and records a ``skipped`` outcome saying so.
* **Pull** on ``auto_pull.interval``. A non-writer runs ``bd dolt pull``. The writer under the
  ``open`` write policy instead runs the frame-ref merge slice
  (:func:`beadhive.frame_merge.after_pull`): every published frame ref is merged into ``main``
  without ``--strategy``, ``metadata.clone_id`` stays the writer's, a conflicting ref is held
  and recorded (an id collision never takes a side), and one ref never blocks the others, the
  pushes or any verb (the ``bh-xwy5x`` decision's conditions 2 and 3 live in that module).

Every attempt records a typed :class:`~beadhive.hive_sync_state.SyncOutcome`; ``bh doctor``
reads them. The loop is pure orchestration over injected collaborators (clock, route, pending
count, push, pull, frame merge), so tests drive it with a fake clock and a dead remote; the
``live_*`` functions are the production collaborators and :class:`SyncLoopComponent` runs it in
one daemon thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import hive_sync_settings, hive_sync_state, log
from .hive_sync_settings import SyncSettings
from .hive_sync_state import PULL, PUSH, Status, SyncOutcome

MAIN = "main"
FRAME = "frame"
NONE = "none"

#: How often the pending count is refreshed for the ``max_pending`` trigger (at most; it is
#: also taken whenever a push is due).
COUNT_EVERY_S = 60.0
#: The loop's poll cadence: how quickly a flush request is noticed.
POLL_S = 5.0


@dataclass(frozen=True)
class SyncHive:
    """One hive the loop serves."""

    hive: str
    hive_dir: Path
    settings: SyncSettings


@dataclass(frozen=True)
class Route:
    """Where this host's pushes of a hive go."""

    kind: str
    #: ``main`` or ``refs/dolt/frame/<id>``; ``""`` for :data:`NONE`.
    target: str = ""
    #: Whether the hive runs the ``open`` write policy on this host.
    open: bool = False
    #: Why nothing is pushed (:data:`NONE`).
    reason: str = ""
    #: This host's frame id, when known (the frame remote's name).
    frame: str = ""


@dataclass
class _Clocks:
    next_push_at: float
    next_pull_at: float
    counted_at: float | None = None
    pending: int | None = None


@dataclass
class SyncLoop:
    """The loop over every hive with ``hive.sync`` on. :meth:`tick` runs one pass;
    :meth:`run` ticks until stopped."""

    hives: Callable[[], Sequence[SyncHive]]
    route: Callable[[SyncHive], Route]
    pending: Callable[[SyncHive, Route], int | None]
    push: Callable[[SyncHive, Route], Any]
    pull: Callable[[SyncHive], Any]
    merge_frames: Callable[[SyncHive], Sequence[Any]]
    clock: Callable[[], float] = time.time
    state_path: Path | None = None
    count_every_s: float = COUNT_EVERY_S
    _clocks: dict[str, _Clocks] = field(default_factory=dict)

    # ---- one pass -----------------------------------------------------------------------

    def tick(self) -> list[SyncOutcome]:
        """One pass over every hive; returns the outcomes recorded. Never raises."""
        outcomes: list[SyncOutcome] = []
        try:
            hives = list(self.hives())
        except Exception as exc:  # noqa: BLE001 - an unreadable registry skips this pass
            log.get_logger(__name__).warning("hive_sync_hives_unreadable", error=str(exc))
            return outcomes
        for hive in hives:
            try:
                outcomes += self._hive(hive)
            except Exception as exc:  # noqa: BLE001 - one hive never stops the others
                log.get_logger(__name__).warning("hive_sync_failed", hive=hive.hive, error=str(exc))
        return outcomes

    def _hive(self, hive: SyncHive) -> list[SyncOutcome]:
        now = self.clock()
        clocks = self._clocks.setdefault(hive.hive, _Clocks(now, now))
        outcomes = []
        route: Route | None = None
        if hive.settings.push_enabled:
            route = self._route(hive)
            outcome = self._maybe_push(hive, route, clocks, now)
            if outcome is not None:
                outcomes.append(outcome)
        if hive.settings.pull_enabled and now >= clocks.next_pull_at:
            clocks.next_pull_at = now + hive.settings.pull_interval_s
            outcomes.append(self._pull(hive, route or self._route(hive), now))
        return outcomes

    def _route(self, hive: SyncHive) -> Route:
        try:
            return self.route(hive)
        except Exception as exc:  # noqa: BLE001 - an unreadable fence never pushes main
            return Route(NONE, reason=f"cannot decide who writes this hive: {exc}")

    # ---- push ---------------------------------------------------------------------------

    def _maybe_push(
        self, hive: SyncHive, route: Route, clocks: _Clocks, now: float
    ) -> SyncOutcome | None:
        settings = hive.settings
        record = hive_sync_state.recorded(hive.hive, path=self.state_path)
        if route.kind == NONE:
            prior = record.push
            if prior is not None and prior.status == Status.SKIPPED and prior.error == route.reason:
                return None  # already recorded; do not rewrite the record every tick
            return self._record(
                SyncOutcome(hive.hive, PUSH, Status.SKIPPED, now, now, error=route.reason)
            )
        if record.next_retry_at is not None and now < record.next_retry_at:
            return None  # backing off after a failed push
        flush = record.flush_pending
        # A failed push is retried once its backoff has passed, whatever the timer says.
        timer = now >= clocks.next_push_at or record.failures > 0
        count_due = clocks.counted_at is None or now - clocks.counted_at >= self.count_every_s
        if not (flush or timer or count_due):
            return None
        if flush or count_due:
            pending = self._pending(hive, route)
            clocks.counted_at, clocks.pending = now, pending
            if pending is not None:
                hive_sync_state.record_pending(hive.hive, pending, now, path=self.state_path)
        else:
            pending = clocks.pending  # counted within count_every_s: no bd call per tick
        if flush:
            reason = "flush"
        elif pending is not None and pending >= settings.max_pending:
            reason = "max_pending"
        elif timer and (pending is None or pending > 0):
            reason = "retry" if record.failures else "timer"
        else:
            return None
        if pending == 0:
            # Nothing to push: the remote already holds every local commit. A flush request is
            # answered here (the waiter's commits are on the target).
            clocks.next_push_at = now + settings.push_interval_s
            return self._record(
                SyncOutcome(
                    hive.hive,
                    PUSH,
                    Status.CLEAN,
                    now,
                    now,
                    target=route.target,
                    reason=reason,
                    pending=0,
                )
            )
        return self._push(hive, route, clocks, now, reason, pending, record.failures)

    def _pending(self, hive: SyncHive, route: Route) -> int | None:
        try:
            return self.pending(hive, route)
        except Exception:  # noqa: BLE001 - unknown is not zero: the timer still pushes
            return None

    def _push(
        self,
        hive: SyncHive,
        route: Route,
        clocks: _Clocks,
        started: float,
        reason: str,
        pending: int | None,
        failures: int,
    ) -> SyncOutcome:
        settings = hive.settings
        clocks.next_push_at = started + settings.push_interval_s
        error = ""
        try:
            result = self.push(hive, route)
            if getattr(result, "returncode", 1) != 0:
                error = _err_line(result) or f"push exited {getattr(result, 'returncode', '?')}"
        except Exception as exc:  # noqa: BLE001 - a failed push is an outcome, never a raise
            error = f"{type(exc).__name__}: {exc}"
        finished = self.clock()
        if not error:
            clocks.pending = 0
            return self._record(
                SyncOutcome(
                    hive.hive,
                    PUSH,
                    Status.OK,
                    started,
                    finished,
                    target=route.target,
                    reason=reason,
                    pending=pending,
                )
            )
        failures += 1
        retry = finished + settings.backoff_s(failures)
        return self._record(
            SyncOutcome(
                hive.hive,
                PUSH,
                Status.FAILED,
                started,
                finished,
                target=route.target,
                reason=reason,
                pending=pending,
                error=error,
                failures=failures,
                next_retry_at=retry,
            )
        )

    # ---- pull ---------------------------------------------------------------------------

    def _pull(self, hive: SyncHive, route: Route, started: float) -> SyncOutcome:
        if route.kind == MAIN:
            if not route.open:
                return self._record(
                    SyncOutcome(
                        hive.hive,
                        PULL,
                        Status.SKIPPED,
                        started,
                        started,
                        error="this host is the hive's writer: it pulls nothing (only the "
                        "writer pushes main); frame refs are merged only under the open policy",
                    )
                )
            return self._merge(hive, started)
        error = ""
        try:
            result = self.pull(hive)
            if getattr(result, "returncode", 1) != 0:
                error = _err_line(result) or f"pull exited {getattr(result, 'returncode', '?')}"
        except Exception as exc:  # noqa: BLE001 - a failed pull is an outcome, never a raise
            error = f"{type(exc).__name__}: {exc}"
        status = Status.FAILED if error else Status.OK
        return self._record(
            SyncOutcome(
                hive.hive,
                PULL,
                status,
                started,
                self.clock(),
                target=MAIN,
                reason="timer",
                error=error,
            )
        )

    def _merge(self, hive: SyncHive, started: float) -> SyncOutcome:
        """The writer under ``open``: merge every published frame ref. A held ref is reported
        in ``frame_merges`` (and by the frame-merge section of doctor); it never fails the pull
        or blocks the other refs."""
        try:
            merges = list(self.merge_frames(hive))
        except Exception as exc:  # noqa: BLE001 - after_pull never raises; belt and braces
            return self._record(
                SyncOutcome(
                    hive.hive,
                    PULL,
                    Status.FAILED,
                    started,
                    self.clock(),
                    reason="timer",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        summary = tuple(_merge_summary(item) for item in merges)
        return self._record(
            SyncOutcome(
                hive.hive,
                PULL,
                Status.OK,
                started,
                self.clock(),
                target=MAIN,
                reason="timer",
                frame_merges=summary,
            )
        )

    def _record(self, outcome: SyncOutcome) -> SyncOutcome:
        hive_sync_state.record(outcome, path=self.state_path)
        return outcome

    # ---- the thread ---------------------------------------------------------------------

    def run(self, wait: Callable[[float], bool], *, poll_s: float = POLL_S) -> None:
        """Tick until ``wait(poll_s)`` returns True (an Event's ``wait`` in the daemon thread,
        a fake timer that advances the fake clock in tests)."""
        while True:
            self.tick()
            if wait(poll_s):
                return


def _err_line(result: Any) -> str:
    text = (getattr(result, "stderr", "") or "").strip() or (
        getattr(result, "stdout", "") or ""
    ).strip()
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def _merge_summary(item: Any) -> Mapping:
    if hasattr(item, "as_dict"):
        data = item.as_dict()
        return {
            "frame": data.get("frame", ""),
            "status": data.get("status", ""),
            "error": data.get("error", ""),
            "summary": data.get("summary", ""),
        }
    return dict(item) if isinstance(item, Mapping) else {"status": str(item)}


# =============================================================================================
# Production collaborators
# =============================================================================================


def _settings_port():
    from .config_consumer_ports import daemon_settings as config

    return config


def live_hives() -> list[SyncHive]:
    """Every registered hive (HQ excluded) with ``hive.sync`` auto-push or auto-pull on and a
    checkout on this host."""
    from . import registry

    cfg = _settings_port().load()
    found = []
    for entry in registry.hives(cfg):
        if not isinstance(entry, Mapping) or not entry.get("prefix"):
            continue
        settings = hive_sync_settings.for_entry(entry, cfg)
        if not settings.active:
            continue
        try:
            hive_dir = Path(registry.hive_dir(entry))
        except Exception:  # noqa: BLE001 - no checkout here: nothing to sync
            continue
        if hive_dir.exists():
            found.append(SyncHive(str(entry["prefix"]), hive_dir, settings))
    return found


def live_route(hive: SyncHive) -> Route:
    """Who pushes what for `hive` on this host, decided as the guard decides it."""
    from . import write_policy

    where = write_policy.off_primary(hive_dir=hive.hive_dir)
    policy = write_policy.for_dir(hive.hive_dir)
    is_open = policy is not None and policy.open
    if where is None:
        return Route(MAIN, MAIN, open=is_open)
    if is_open:
        frame = where.this_frame
        return Route(FRAME, write_policy.FRAME_REF.format(frame=frame), open=True, frame=frame)
    described = policy.describe() if policy is not None else "fenced"
    return Route(
        NONE,
        reason=f"not the writer ({where.writer or '?'} is; this frame is "
        f"{where.this_frame or '?'}); only the writer pushes main, and the write policy here is "
        f"{described}",
    )


def _engine():
    from . import engine

    return engine.get_engine(_settings_port().load())


def live_pending(hive: SyncHive, route: Route) -> int | None:
    """Local commits not yet on the route's target, plus one for an uncommitted working set
    (``auto_commit: batch``). ``None`` when it cannot be counted (no tracking ref yet)."""
    import json

    from . import write_policy

    remotes = ["origin"]
    if route.kind == FRAME and route.frame:
        remotes.insert(0, write_policy.FRAME_REMOTE.format(frame=route.frame))
    engine = _engine()
    for remote in remotes:
        query = (
            f"SELECT (SELECT COUNT(*) FROM dolt_log('remotes/{remote}/main..main')) AS ahead, "
            "(SELECT COUNT(*) FROM dolt_status) AS dirty"
        )
        result = engine.invoke(["sql", "--json", query], cwd=hive.hive_dir, capture=True)
        if getattr(result, "returncode", 1) != 0:
            continue
        try:
            rows = json.loads(getattr(result, "stdout", "") or "[]")
            row = rows[0] if isinstance(rows, list) and rows else {}
            return int(row.get("ahead") or 0) + (1 if int(row.get("dirty") or 0) else 0)
        except (ValueError, TypeError, AttributeError):
            continue
    return None


def live_push(hive: SyncHive, route: Route) -> Any:
    """:meth:`Engine.push_state`: the writer's managed push of ``main``, or (under ``open``,
    off the writer) the publish of ``refs/dolt/frame/<id>`` it diverts to."""
    return _engine().push_state(hive.hive_dir, message="bh: hive.sync auto-push")


def live_pull(hive: SyncHive) -> Any:
    return _engine().pull_state(hive.hive_dir)


def live_merge_frames(hive: SyncHive) -> list:
    from . import frame_merge

    return frame_merge.after_pull(hive.hive_dir, cfg=_settings_port().load())


def build_loop(*, clock: Callable[[], float] = time.time) -> SyncLoop | None:
    """The daemon's loop when any registered hive has ``hive.sync`` auto-push or auto-pull on;
    ``None`` otherwise (no thread at all)."""
    if not live_hives():
        return None
    return SyncLoop(
        hives=live_hives,
        route=live_route,
        pending=live_pending,
        push=live_push,
        pull=live_pull,
        merge_frames=live_merge_frames,
        clock=clock,
    )


@dataclass
class SyncLoopComponent:
    """The host-daemon lifespan component that runs a :class:`SyncLoop` off the event loop, in
    one thread, until shutdown."""

    loop: SyncLoop
    _stop: Any = None

    def component(self) -> Any:
        import asyncio
        import threading
        from contextlib import asynccontextmanager

        from .kernel.lifecycle import HostDaemonLifespanComponent, HostDaemonShutdownPhase

        @asynccontextmanager
        async def lifespan(_app: Any):
            self._stop = threading.Event()
            task = asyncio.create_task(
                asyncio.to_thread(self.loop.run, self._stop.wait), name="hive-sync"
            )
            try:
                yield
            finally:
                self._stop.set()
                try:
                    await task
                except Exception:  # noqa: BLE001 - shutdown never fails on the sync loop
                    pass

        return HostDaemonLifespanComponent(
            name="hive-sync",
            lifespan=lifespan,
            shutdown_phase=HostDaemonShutdownPhase.CANCEL_PROCESSES,
        )
