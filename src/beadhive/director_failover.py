"""The director's unattended failover loop, run by the host daemon (bh-16347.5).

bh-a94qw shipped :class:`beadhive.failover_observer.FailoverDirector` (one ``tick``) and the SQL
placement director as library code. This module binds the director's ports to ``dolt-server``
HQ and runs ticks from a long-lived process:

* **Where it runs.** The host daemon (``bh host daemon serve``), as one lifespan component, when
  ``host.daemon.failover.enabled`` is true. **Off by default**: nothing else turns it on.
* **What it reads.** One verified read transaction per tick
  (:meth:`~beadhive.hq_sql_placement.SqlPlacementDirector.survey`) on the director credential
  (``hq.sql.placement_writer`` in the operator settings file): the operator-signed authority,
  every placement row and each active frame's session staleness by the HQ server's clock. A
  frame without a session table (M9, ``bh-owqdg``, not provisioned for it) is unobserved, so it
  never fails over — fail safe.
* **What it writes.** Only the placement CAS, through
  :meth:`~beadhive.hq_sql_placement.SqlPlacementDirector.place` at the row's epoch + 1. A lost CAS
  is recorded and re-decided next tick, never retried with the same expectation.
* **Successor.** Until placement spreading lands (M8b, ``bh-zncqo``), the successor is the
  freshest other frame that is active, uncordoned, bound to the hive's signed policy and
  currently observed live; ties break by frame id. No such frame means no failover.
* **git HQ is refused** at startup (:class:`~beadhive.failover_observer.FailoverDirector`):
  unattended failover needs server-stamped session rows.

``failover_after`` is the per-role code default for an executor (the longest; frames carry no
role in the signed authority). The per-role, per-hive override on the placement row is M8c
(``bh-4biq8``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from .failover_observer import (
    FailoverDirector,
    FailoverMonitor,
    FailoverResult,
    failover_after_for,
    session_table,
    sql_session_staleness,
)
from .hq_sql_placement import PlacementError, SqlPlacementDirector, Survey

__all__ = [
    "DEFAULT_ROLE",
    "FailoverLoop",
    "FailoverRefused",
    "SqlFailoverPorts",
    "build_loop",
    "host_hq_mode",
]

_log = logging.getLogger(__name__)
#: Frames carry no role in the signed authority; the executor default is the longest window.
DEFAULT_ROLE = "executor"
_SESSION_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema=DATABASE() AND table_name LIKE 'frame%session'"
)


class FailoverRefused(ValueError):
    """The failover loop cannot run with this configuration; the daemon refuses to start."""


def host_hq_mode(cfg: Mapping) -> str:
    """``dolt-server`` when the (effective) config's HQ binding selects SQL, else ``git``."""
    hq = (cfg or {}).get("hq") or {}
    sql = hq.get("sql") or {}
    if hq.get("mode") == "dolt-server" or (isinstance(sql, Mapping) and sql.get("enabled")):
        return "dolt-server"
    return "git"


def _active_incarnations(cursor, head: str, state: Mapping) -> dict[str, tuple[str, int]]:
    """``{frame_id: (principal, epoch)}`` for each frame's ACTIVE incarnation in the registry."""
    active = {
        frame: entry["active"]["authority"]
        for frame, entry in (state.get("frames") or {}).items()
        if entry.get("active") is not None and entry["active"].get("state") == "active"
    }
    if not active:
        return {}
    cursor.execute(
        "SELECT principal,frame_id,holder_identity,epoch FROM hq_principal_registry AS OF %s",
        (head,),
    )
    found = {}
    for principal, frame, holder, epoch in cursor.fetchall():
        authority = active.get(frame)
        if (
            authority is not None
            and authority.get("holder_identity") == holder
            and authority.get("epoch") == epoch
        ):
            found[frame] = (str(principal), int(epoch))
    return found


def observe_sessions(
    cursor, head: str, state: Mapping, *, table_for: Callable[[str, int], str] = session_table
) -> dict[str, float | None]:
    """Each active frame's session staleness (seconds by the HQ server's clock). ``None`` for
    a frame whose session table does not exist or holds no row: not observed."""
    cursor.execute(_SESSION_TABLES_SQL)
    existing = {str(row[0]) for row in cursor.fetchall()}
    staleness: dict[str, float | None] = {}
    for frame, (principal, epoch) in _active_incarnations(cursor, head, state).items():
        try:
            table = table_for(principal, epoch)
        except ValueError:
            staleness[frame] = None
            continue
        staleness[frame] = sql_session_staleness(cursor, table) if table in existing else None
    return staleness


@dataclass
class SqlFailoverPorts:
    """:class:`FailoverDirector`'s four ports over one :class:`SqlPlacementDirector`."""

    director: SqlPlacementDirector
    failover_after: Callable[[str], float | None] = field(
        default=lambda _frame: failover_after_for(DEFAULT_ROLE)
    )
    table_for: Callable[[str, int], str] = session_table
    survey: Survey | None = None

    def staleness(self) -> Mapping[str, float | None]:
        self.survey = None  # a failed survey leaves nothing to place from
        self.survey = self.director.survey(
            lambda cursor, head, state: observe_sessions(
                cursor, head, state, table_for=self.table_for
            )
        )
        return self.survey.observed or {}

    def placements(self) -> Mapping[str, object]:
        return dict(self.survey.placements) if self.survey is not None else {}

    def successor(self, prefix: str, dead: str) -> str | None:
        survey = self.survey
        if survey is None:
            return None
        observed: Mapping[str, float | None] = survey.observed or {}
        candidates = []
        for frame, age in observed.items():
            after = self.failover_after(frame)
            if frame == dead or age is None or after is None or age >= after:
                continue
            try:
                self.director.placeable(survey.state, survey.policies, prefix, frame)
            except PlacementError:
                continue
            candidates.append((age, frame))
        return min(candidates)[1] if candidates else None

    def place(self, prefix: str, frame: str, expected: str) -> object:
        return self.director.place(prefix, frame_id=frame, expected_revision=expected)


@dataclass
class FailoverLoop:
    """Ticks a :class:`FailoverDirector` every `interval` seconds off the event loop."""

    director: FailoverDirector
    interval: float
    last: list[FailoverResult] = field(default_factory=list)
    ticks: int = 0
    errors: int = 0
    _closed: bool = False

    def tick(self) -> list[FailoverResult]:
        try:
            results = self.director.tick()
        except Exception:  # noqa: BLE001 - one failed tick never stops the loop
            self.errors += 1
            _log.exception("director failover tick failed")
            return []
        self.ticks += 1
        self.last = results
        for result in results:
            _log.warning(
                "director failover %s: %s %s -> %s %s",
                result.outcome,
                result.prefix,
                result.from_frame,
                result.to_frame or "-",
                result.detail,
            )
        return results

    async def run(self) -> None:
        while not self._closed:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(self.interval)

    def component(self) -> Any:
        from .kernel.lifecycle import HostDaemonLifespanComponent, HostDaemonShutdownPhase

        @asynccontextmanager
        async def lifespan(_app: Any):
            task = asyncio.create_task(self.run(), name="director-failover")
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
            name="director-failover",
            lifespan=lifespan,
            shutdown_phase=HostDaemonShutdownPhase.CANCEL_PROCESSES,
        )


def build_loop(
    settings: Any,
    *,
    hq_mode: str | None = None,
    director: SqlPlacementDirector | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FailoverLoop | None:
    """The loop for a ``host.daemon.failover`` section, or ``None`` when disabled.

    Refuses (:class:`FailoverRefused`) git HQ, a missing operator settings file, or a file
    without the director credential — the daemon then refuses to start rather than run
    without the loop it was configured to run."""
    if not settings.enabled:
        return None
    from .failover_observer import UnattendedFailoverUnsupported, unattended_failover_supported

    mode = hq_mode if hq_mode is not None else "git"  # unknown is refused, never assumed SQL
    try:
        supported = unattended_failover_supported(mode)
    except ValueError as exc:
        raise FailoverRefused(f"director failover refused: {exc}") from None
    if not supported:  # before touching any credential file
        raise FailoverRefused(
            f"director failover refused: this host's HQ is {mode}; unattended failover needs "
            "dolt-server HQ (server-stamped session rows)"
        )
    if director is None:
        from . import hq_operator_settings

        path = settings.operator_settings or os.environ.get(hq_operator_settings.ENV)
        if not path:
            raise FailoverRefused(
                "host.daemon.failover.enabled needs host.daemon.failover.operator_settings "
                f"(or {hq_operator_settings.ENV}) naming the director's settings file"
            )
        try:
            director = hq_operator_settings.placement_director(path)
        except ValueError as exc:
            raise FailoverRefused(f"director failover settings refused: {exc}") from None
    ports = SqlFailoverPorts(director)
    try:
        failover = FailoverDirector(
            hq_mode=mode,
            monitor=FailoverMonitor(ports.failover_after, clock=clock),
            staleness=ports.staleness,
            placements=ports.placements,
            successor=ports.successor,
            place=ports.place,
        )
    except (UnattendedFailoverUnsupported, ValueError) as exc:
        raise FailoverRefused(f"director failover refused: {exc}") from None
    return FailoverLoop(failover, interval=float(settings.interval_seconds))
