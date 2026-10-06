"""The director's failover observer (bh-a94qw, P-M8; ADR §4, condition 8).

Time decides *when* to reassign a hive, never *who may write* (the data does that). The
director fails a frame over only when::

    min(server staleness, observed window) > failover_after

* **server staleness** — in ``dolt-server`` HQ, the age of the frame's session row by the HQ
  server's own ``UTC_TIMESTAMP(6)`` (:func:`sql_session_staleness`; the rows are M9's). In
  ``git`` HQ, the observer's first-seen staleness of verified signed beats
  (:func:`git_beat_staleness`, from ``host_heartbeat_core.observe``).
* **observed window** — how long THIS director has continuously watched, on its own monotonic
  clock. It resets whenever HQ is unreachable, **and on any gap between two successful
  observations longer than ``failover_after / 2``** (``bh-cvk70`` E15, ``bh-jbb6r`` E9).
  Without the gap reset a director that missed an outage fails a healthy primary over the
  moment HQ returns: server staleness spans the outage, and so would a window that merely
  remembered its start.

Unattended failover is a ``dolt-server`` HQ capability only (:class:`FailoverDirector` refuses
``git``); in git HQ the observer still reports, and failover is an operator or director
placement CAS (``refs/bh/lease/<prefix>`` via ``gitref.cas``, unchanged).

``failover_after`` defaults per role live here (:data:`ROLE_FAILOVER_AFTER_S`); the per-role,
per-hive override on the placement row is M8c (``bh-4biq8``), which plugs in through
:func:`failover_after_for`'s ``override``.

Pure apart from the SQL helper; every clock and port is injected. Typer-free.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

__all__ = [
    "HQ_MODES",
    "ROLE_FAILOVER_AFTER_S",
    "SESSION_STALENESS_SQL",
    "Decision",
    "FailoverDirector",
    "FailoverMonitor",
    "FailoverObserver",
    "FailoverResult",
    "UnattendedFailoverUnsupported",
    "failover_after_for",
    "git_beat_staleness",
    "sql_session_staleness",
    "unattended_failover_supported",
]

#: Code defaults (ADR §4): an executor rides the 35.4 min false-stale stretch observed live; a
#: transient frame comes and goes; a viewer is never placed, so it never fails over.
ROLE_FAILOVER_AFTER_S: dict[str, float | None] = {
    "executor": 3600.0,
    "transient": 1800.0,
    "viewer": None,
}
HQ_MODES = ("git", "dolt-server")

#: Session-row age by the HQ server's clock (M9 owns the table; one row, ``id = 1``).
SESSION_STALENESS_SQL = (
    "SELECT TIMESTAMPDIFF(MICROSECOND, renewed_at, UTC_TIMESTAMP(6)) FROM {table} WHERE id = 1"
)
_TABLE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class UnattendedFailoverUnsupported(ValueError):
    """Unattended failover was asked of an HQ mode that has no server-stamped liveness."""


def failover_after_for(role: str, *, override: float | None = None) -> float | None:
    """``failover_after`` seconds for `role`; ``None`` means never fail over (never placed).

    `override` is the hook for the per-role, per-hive field on the HQ placement row (M8c,
    ``bh-4biq8``), which owns loading and floor validation (refused, never clamped). An unknown
    role takes the executor default: the longest, so a newer role never fails over early."""
    from .host_manifest_contracts import canonical_role

    if override is not None:
        if type(override) not in (int, float) or not math.isfinite(override) or override <= 0:
            raise ValueError("failover_after override must be a positive, finite number")
        return float(override)
    role = canonical_role(role)
    if role in ROLE_FAILOVER_AFTER_S:
        return ROLE_FAILOVER_AFTER_S[role]
    return ROLE_FAILOVER_AFTER_S["executor"]


def unattended_failover_supported(hq_mode: str) -> bool:
    """Only ``dolt-server`` HQ has server-stamped session rows to fail over on unattended."""
    if hq_mode not in HQ_MODES:
        raise ValueError(f"unknown HQ mode {hq_mode!r}")
    return hq_mode == "dolt-server"


# =============================================================================================
# The observer
# =============================================================================================


@dataclass(frozen=True)
class Decision:
    """One observation's outcome. ``reason`` names why it is (not) due."""

    due: bool
    reason: str
    staleness: float | None = None
    window: float = 0.0


@dataclass
class FailoverObserver:
    """``min(server staleness, observed window) > failover_after`` on a monotonic clock.

    Call :meth:`observe` once per poll with the frame's server staleness, or ``None`` when HQ
    could not be read. The window resets on ``None`` and on a gap between two successful
    observations longer than ``failover_after / 2``."""

    failover_after: float
    clock: Callable[[], float] = field(default=time.monotonic)
    _since: float | None = field(default=None, init=False)
    _last: float | None = field(default=None, init=False)

    def __post_init__(self):
        value = self.failover_after
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("failover_after must be a positive, finite number of seconds")

    @property
    def window(self) -> float:
        """The current observed window (0 when nothing is being observed)."""
        if self._since is None or self._last is None:
            return 0.0
        return self._last - self._since

    def reset(self) -> None:
        self._since = self._last = None

    def observe(self, staleness: float | None) -> Decision:
        now = self.clock()
        if staleness is None or not math.isfinite(staleness):
            self.reset()  # HQ unreachable: observe nothing, decide nothing
            return Decision(False, "hq-unreachable")
        reason = "observing"
        if self._last is not None and now - self._last > self.failover_after / 2:
            self._since = None  # a gap: whatever happened in it was not observed
            reason = "gap-reset"
        if self._since is None:
            self._since = now
            reason = "gap-reset" if reason == "gap-reset" else "window-start"
        self._last = now
        window = now - self._since
        due = min(staleness, window) > self.failover_after
        return Decision(due, "due" if due else reason, staleness, window)


@dataclass
class FailoverMonitor:
    """One :class:`FailoverObserver` per placed frame, with per-frame ``failover_after``."""

    failover_after: Callable[[str], float | None]
    clock: Callable[[], float] = field(default=time.monotonic)
    observers: dict[str, FailoverObserver] = field(default_factory=dict)

    def observe(self, staleness: Mapping[str, float | None] | None) -> dict[str, Decision]:
        """Feed one poll. ``None`` (HQ unreachable) resets every window and decides nothing.
        A frame missing from `staleness` is unobserved this round: its window resets."""
        if staleness is None:
            for observer in self.observers.values():
                observer.reset()
            return {}
        decisions = {}
        for frame in set(self.observers) - set(staleness):
            self.observers[frame].reset()
        for frame, age in staleness.items():
            after = self.failover_after(frame)
            if after is None:
                decisions[frame] = Decision(False, "never-fails-over", age)
                continue
            observer = self.observers.get(frame)
            if observer is None or observer.failover_after != after:
                observer = self.observers[frame] = FailoverObserver(after, clock=self.clock)
            decisions[frame] = observer.observe(age)
        return decisions


# =============================================================================================
# Staleness sources
# =============================================================================================


def sql_session_staleness(cursor, table: str) -> float | None:
    """Session-row age in seconds by the HQ server's ``UTC_TIMESTAMP(6)``; ``None`` when the
    row is absent (never renewed: nothing observable, not "infinitely stale")."""
    if not isinstance(table, str) or not _TABLE.fullmatch(table):
        raise ValueError("invalid session table identifier")
    cursor.execute(SESSION_STALENESS_SQL.format(table=table))
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0]) / 1e6


def git_beat_staleness(observation) -> float | None:
    """First-seen staleness of a verified signed beat (``host_heartbeat_core.observe``); an
    absent, invalid or unverified beat is not an observation."""
    if not getattr(observation, "verified", False):
        return None
    age = getattr(observation, "age_seconds", None)
    if type(age) not in (int, float) or not math.isfinite(age):
        return None
    return float(age)


# =============================================================================================
# The unattended director loop body
# =============================================================================================


@dataclass(frozen=True)
class FailoverResult:
    prefix: str
    from_frame: str
    to_frame: str | None
    outcome: str  # "placed" | "lost" | "no-successor" | "refused"
    detail: str = ""


class FailoverDirector:
    """One tick: observe every placed frame, and for each frame that is due, CAS each of its
    hives to a successor. Every port is injected:

    * ``staleness()`` → ``{frame_id: seconds | None}``; raise or return ``None`` when HQ is
      unreachable (every window resets);
    * ``placements()`` → ``{prefix: PlacementRecord}`` (``frame_id``, ``revision``);
    * ``successor(prefix, dead_frame)`` → a frame id or ``None`` (M8b spreads placement);
    * ``place(prefix, frame_id, expected_revision)`` → the new row, or raises
      :class:`beadhive.hq_sql_placement.PlacementLost` (recorded; never retried this tick).

    Refuses construction for ``git`` HQ: unattended failover is ``dolt-server`` only."""

    def __init__(
        self,
        *,
        hq_mode: str,
        monitor: FailoverMonitor,
        staleness: Callable[[], Mapping[str, float | None] | None],
        placements: Callable[[], Mapping[str, object]],
        successor: Callable[[str, str], str | None],
        place: Callable[[str, str, str], object],
    ):
        if not unattended_failover_supported(hq_mode):
            raise UnattendedFailoverUnsupported(
                "unattended failover needs dolt-server HQ (server-stamped session rows); in git "
                "HQ failover is an operator or director placement CAS on refs/bh/lease/<prefix>"
            )
        self.monitor = monitor
        self._staleness, self._placements = staleness, placements
        self._successor, self._place = successor, place

    def tick(self) -> list[FailoverResult]:
        from .hq_sql_placement import PlacementError, PlacementLost

        try:
            observed = self._staleness()
        except Exception:  # noqa: BLE001 - an unreadable HQ is "unreachable": reset, decide nothing
            observed = None
        decisions = self.monitor.observe(observed)
        due = {frame for frame, decision in decisions.items() if decision.due}
        if not due:
            return []
        results = []
        for prefix, record in sorted(self._placements().items()):
            dead = getattr(record, "frame_id", "")
            if dead not in due:
                continue
            target = self._successor(prefix, dead)
            if not target or target == dead:
                results.append(FailoverResult(prefix, dead, None, "no-successor"))
                continue
            try:
                self._place(prefix, target, record.revision)
            except PlacementLost as exc:
                results.append(FailoverResult(prefix, dead, target, "lost", str(exc)))
                continue
            except PlacementError as exc:
                results.append(FailoverResult(prefix, dead, target, "refused", str(exc)))
                continue
            results.append(FailoverResult(prefix, dead, target, "placed"))
        return results
