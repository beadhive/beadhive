"""The host daemon's side of the elected-tenure cache (trust-profiles ADR section 3, bh-ofszz).

:mod:`beadhive.hq_tenure_cache` is the cache itself — the record, the policy each trust profile
sets, the command-side reads and the injectable :class:`~beadhive.hq_tenure_cache.TenureRefresher`.
This module composes that refresher with the live collaborators only the daemon uses: the
verified HQ snapshot read (:func:`beadhive.hq_control_plane.attach_fleet_config`), one frame
eligibility read per registered hive (:func:`live_decisions`), and the daemon lifespan
component that runs the refresh loop in a thread (:class:`TenureLoop`). Commands never import
it: they read what it writes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .hq_tenure_cache import (
    HiveDecisions,
    TenureRefresher,
    authority_unavailable,
    enabled,
)
from .modules.config.domain.ports import FleetConfigSnapshot


def _section(settings: object, name: str) -> Mapping:
    value = settings.get(name) if isinstance(settings, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def live_decisions(
    host_cfg: Mapping, snapshot: FleetConfigSnapshot, now: float
) -> dict[str, HiveDecisions]:
    """Per-capability decisions for every registered hive (the refresher's ``decide``): one
    full eligibility read per hive, narrowed to each bead-write capability. A hive whose read
    found HQ unreachable, or an unenrolled legacy host, keeps its previous decision."""
    from . import capabilities, frame_eligibility, host, registry
    from .config_consumer_ports import daemon_settings as config

    identity = host.host_id()
    settings = config.load()
    found: dict[str, HiveDecisions] = {}
    for entry in registry.hives(settings):
        prefix = entry.get("prefix") if isinstance(entry, Mapping) else None
        if not prefix:
            continue
        decision = frame_eligibility.decision_for(identity, entry, cfg=settings)
        if decision is None or authority_unavailable(decision):
            continue
        found[str(prefix)] = HiveDecisions(
            identity,
            now,
            snapshot.commit_revision,
            {
                capability: tuple(decision.for_capability(capability).predicates)
                for capability in capabilities.WRITE_CAPABILITIES
            },
        )
    return found


def build_refresher(*, clock: Callable[[], float] = time.time) -> TenureRefresher | None:
    """The daemon's refresher when this host reads SQL HQ with the cache on, else ``None``."""
    from .config_consumer_ports import daemon_settings as config

    try:
        host_cfg = config.load_host()
    except FileNotFoundError:
        return None
    if _section(_section(host_cfg, "hq"), "sql").get("enabled") is not True:
        return None
    if not enabled(host_cfg):
        return None

    def load_live(host: Mapping) -> FleetConfigSnapshot:
        from .hq_control_plane import attach_fleet_config

        return attach_fleet_config(bootstrap=dict(_section(host, "hq")))[1]

    return TenureRefresher(
        host_cfg=config.load_host, load_live=load_live, decide=live_decisions, clock=clock
    )


@dataclass
class TenureLoop:
    """The host-daemon lifespan component that runs a :class:`TenureRefresher` off the event
    loop, in one thread, until shutdown."""

    refresher: TenureRefresher
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
                asyncio.to_thread(self.refresher.run, self._stop.wait), name="hq-tenure-cache"
            )
            try:
                yield
            finally:
                self._stop.set()
                try:
                    await task
                except Exception:  # noqa: BLE001 - shutdown never fails on the refresher
                    pass

        return HostDaemonLifespanComponent(
            name="hq-tenure-cache",
            lifespan=lifespan,
            shutdown_phase=HostDaemonShutdownPhase.CANCEL_PROCESSES,
        )
