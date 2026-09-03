"""Daemon-owned port over authoritative per-hive state, activity, and event replay.

The source adapters, atomic feed, and SSE relay predate the unified daemon.  This module is
the daemon composition boundary: it owns those replaceable implementations as one lifecycle,
applies the typed host limits to every retained buffer, and exposes a narrow consumer contract.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol, TypeVar, runtime_checkable

from starlette.requests import Request

from .daemon_config import HostDaemonConfig
from .host_daemon import DaemonRuntime, LifespanComponent
from .operator_feed import (
    DEFAULT_MAX_CACHED_ACTIVITY_BYTES,
    DEFAULT_MAX_CACHED_ACTIVITY_RUNS,
    OperatorFeed,
)
from .operator_sources import OperatorSourceError, OperatorSources, process_limits_for_shutdown
from .operator_sse import DEFAULT_CLIENT_QUEUE_BYTES, OperatorEventRelay

_FeedResult = TypeVar("_FeedResult")


@runtime_checkable
class StateBroker(Protocol):
    """Consumer-facing state capability; adapters remain behind this port."""

    def snapshot_with_cursor(self, identity: str) -> dict[str, object]: ...

    def activity_with_cursor(
        self, run_id: str, *, after: tuple[str, int] | None = None
    ) -> dict[str, object]: ...

    async def read_snapshot(self, identity: str) -> dict[str, object]: ...

    async def read_activity(
        self, run_id: str, after: tuple[str, int] | None = None
    ) -> dict[str, object]: ...

    async def project_latest_activity(self, run_id: str) -> dict[str, object]: ...

    async def events(self, request: Request) -> Any: ...

    async def remove_hive(self, hive_id: str) -> None: ...

    def retained_state(self) -> Mapping[str, object]: ...

    def component(self) -> LifespanComponent: ...


class DaemonStateBroker:
    """One bounded ordering and source-lifecycle owner for the host daemon."""

    @classmethod
    def for_host(
        cls,
        *,
        runtime: DaemonRuntime,
        host_id: str,
        cfg: dict | None = None,
        settings: HostDaemonConfig | None = None,
    ) -> DaemonStateBroker:
        """Compose the production readers under one daemon-owned process scope.

        ``OperatorSources`` pins the polling adapter used by ``BeadFrameReader.polling`` for the
        daemon lifetime, then composes its schedule/gate projections with the public
        ``AgentRunSummary`` and bounded run-journal readers.  The longer-lived scope is required
        for refresh coalescing and lets this broker reap every backend process during shutdown.
        """

        process_timeout, process_term_grace = process_limits_for_shutdown(runtime.shutdown_budget)
        source_options: dict[str, object] = {}
        if settings is not None:
            source_options = {
                "max_records_per_read": settings.activity.max_records_per_read,
                "max_record_bytes": settings.activity.max_body_bytes,
                "max_inventory_roots": settings.activity.max_inventory_roots,
                "max_inventory_entries": settings.activity.max_inventory_entries,
                "max_inventory_bytes": settings.activity.max_inventory_bytes,
            }
        sources = OperatorSources(
            cfg=cfg,
            host_id=host_id,
            process_timeout=process_timeout,
            process_term_grace=process_term_grace,
            **source_options,
        )
        return cls(sources=sources, runtime=runtime, settings=settings)

    def __init__(
        self,
        *,
        sources: OperatorSources,
        runtime: DaemonRuntime,
        settings: HostDaemonConfig | None = None,
        now_millis: Callable[[], int] | None = None,
        registry_poll_interval: float | None = None,
    ) -> None:
        self.sources = sources
        self._registry_poll_interval = max(
            0.001,
            float(registry_poll_interval if registry_poll_interval is not None else 2.0),
        )
        self._registry_task: asyncio.Task[None] | None = None
        self._registry_stats_lock = threading.Lock()
        self._registry_scans = 0
        self._registry_failures = 0
        self._removed_hives = 0
        self._owned_source_lock = threading.Lock()
        self._owned_source_reads: set[object] = set()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        configured_drain = (
            settings.shutdown.request_drain_seconds
            if settings is not None
            else runtime.shutdown_budget
        )
        self._read_drain_seconds = max(0.001, min(runtime.shutdown_budget, configured_drain))
        if settings is None:
            self.feed = OperatorFeed(sources, now_millis=now_millis)
            self.relay = OperatorEventRelay(
                self.feed,
                runtime,
                now_millis=now_millis,
                feed_runner=self._run_feed_call,
            )
            return

        activity = settings.activity
        self.feed = OperatorFeed(
            sources,
            now_millis=now_millis,
            max_cached_activity_runs=min(
                DEFAULT_MAX_CACHED_ACTIVITY_RUNS,
                activity.max_records_per_read,
            ),
            max_cached_activity_bytes=min(
                DEFAULT_MAX_CACHED_ACTIVITY_BYTES,
                activity.max_records_per_read * activity.max_body_bytes,
            ),
        )
        sse = settings.sse
        # Every event consumes at least one byte, so the configured total byte ceiling is also
        # a conservative global event-count ceiling.  Per-hive byte retention cannot exceed the
        # same process-wide ceiling, and each client gets its own independently bounded queue.
        self.relay = OperatorEventRelay(
            self.feed,
            runtime,
            now_millis=now_millis,
            heartbeat_interval=sse.heartbeat_seconds,
            replay_events=sse.replay_events_per_hive,
            replay_bytes=sse.replay_total_bytes,
            global_replay_events=sse.replay_total_bytes,
            global_replay_bytes=sse.replay_total_bytes,
            client_queue_events=sse.client_queue_events,
            client_queue_bytes=min(DEFAULT_CLIENT_QUEUE_BYTES, sse.replay_total_bytes),
            feed_runner=self._run_feed_call,
        )

    def snapshot_with_cursor(self, identity: str) -> dict[str, object]:
        operation = self._begin_source_read()
        try:
            return self.feed.snapshot_with_cursor(identity)
        finally:
            self._finish_source_read(operation)

    def activity_with_cursor(
        self, run_id: str, *, after: tuple[str, int] | None = None
    ) -> dict[str, object]:
        operation = self._begin_source_read()
        try:
            return self.feed.activity_with_cursor(run_id, after=after)
        finally:
            self._finish_source_read(operation)

    @staticmethod
    def _closing_error() -> OperatorSourceError:
        return OperatorSourceError(
            "state_broker_closing",
            "The authoritative state broker is draining.",
            status_code=503,
            retryable=True,
        )

    def _begin_source_read(self) -> object:
        operation = object()
        with self._owned_source_lock:
            if self._closing or self._closed:
                raise self._closing_error()
            self._owned_source_reads.add(operation)
        return operation

    def _finish_source_read(self, operation: object) -> None:
        with self._owned_source_lock:
            self._owned_source_reads.discard(operation)

    async def _run_feed_call(
        self,
        function: Callable[..., _FeedResult],
        *args: object,
        on_cancel: Callable[[], None] | None = None,
    ) -> _FeedResult:
        """Own one executor read until its worker and cancellation bookkeeping complete."""

        loop = asyncio.get_running_loop()
        operation = object()
        with self._owned_source_lock:
            if self._closing or self._closed:
                raise self._closing_error()
            future = loop.run_in_executor(None, function, *args)
            self._owned_source_reads.add(operation)
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    cancelled = True
                if future.done():
                    break
            if cancelled:
                if on_cancel is not None:
                    on_cancel()
                raise asyncio.CancelledError
            return future.result()
        finally:
            self._finish_source_read(operation)

    async def read_snapshot(self, identity: str) -> dict[str, object]:
        return await self._run_feed_call(
            self.feed.snapshot_with_cursor,
            identity,
            on_cancel=lambda: self.feed.mark_hive_discontinuous(
                identity, "snapshot_read_cancelled"
            ),
        )

    async def read_activity(
        self, run_id: str, after: tuple[str, int] | None = None
    ) -> dict[str, object]:
        worker = functools.partial(self.feed.activity_with_cursor, run_id, after=after)
        return await self._run_feed_call(
            worker,
            on_cancel=lambda: self.feed.mark_activity_discontinuous(
                run_id, "activity_read_cancelled"
            ),
        )

    async def project_latest_activity(self, run_id: str) -> dict[str, object]:
        return await self._run_feed_call(
            self.feed.project_latest_durable_activity,
            run_id,
            on_cancel=lambda: self.feed.mark_activity_discontinuous(
                run_id, "activity_projection_cancelled"
            ),
        )

    async def events(self, request: Request) -> Any:
        return await self.relay.events(request)

    async def remove_hive(self, hive_id: str) -> None:
        await self.relay.remove_hive(hive_id)

    async def reconcile_registry(self) -> None:
        """Remove retained state for hive identities absent from the current registry."""

        operation = self._begin_source_read()
        try:
            try:
                registered = await self._run_feed_call(self.sources.registered_hives)
            except Exception:
                with self._registry_stats_lock:
                    self._registry_failures += 1
                return
            configured = {hive.identity for hive in registered}
            tracked = self.feed.tracked_hive_ids() | self.relay.tracked_hive_ids()
            removed = sorted(tracked - configured)
            for hive_id in removed:
                await self.remove_hive(hive_id)
            with self._registry_stats_lock:
                self._registry_scans += 1
                self._removed_hives += len(removed)
        finally:
            self._finish_source_read(operation)

    async def _reconcile_registry_loop(self) -> None:
        while True:
            await self.reconcile_registry()
            await asyncio.sleep(self._registry_poll_interval)

    def _cancel_registry_reconciler(self) -> asyncio.Task[None] | None:
        task = self._registry_task
        self._registry_task = None
        if task is not None:
            task.cancel()
        return task

    async def _drain_source_reads(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._read_drain_seconds
        while True:
            with self._owned_source_lock:
                remaining = len(self._owned_source_reads)
            if remaining == 0:
                return
            wait_seconds = deadline - loop.time()
            if wait_seconds <= 0:
                raise OperatorSourceError(
                    "state_broker_drain_timeout",
                    "Authoritative source reads exceeded the bounded shutdown drain.",
                    status_code=503,
                    retryable=True,
                )
            await asyncio.sleep(min(0.01, wait_seconds))

    def retained_state(self) -> dict[str, object]:
        state = self.relay.retained_state()
        state["cachedActivityRuns"] = self.feed.cached_activity_runs
        state["cachedActivityBytes"] = self.feed.cached_activity_bytes
        state["cachedRunOwnerships"] = self.feed.cached_run_ownerships
        state["hiveAdmissions"] = self.feed.hive_admissions
        state["hiveAdmissionCapacity"] = self.feed.max_hive_admissions
        state["hiveAdmissionRejections"] = self.feed.hive_admission_rejections
        with self._owned_source_lock:
            state["ownedFeedCalls"] = len(self._owned_source_reads)
            state["brokerClosing"] = self._closing
            state["brokerClosed"] = self._closed
        task = self._registry_task
        with self._registry_stats_lock:
            state["registryReconciler"] = {
                "running": task is not None and not task.done(),
                "scans": self._registry_scans,
                "failures": self._registry_failures,
                "removedHives": self._removed_hives,
            }
        return state

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            with self._owned_source_lock:
                self._closing = True
            self.feed.cancel_source_reads()
            reconciler = self._cancel_registry_reconciler()
            await self._drain_source_reads()
            if reconciler is not None:
                await asyncio.gather(reconciler, return_exceptions=True)
            await self.relay.close()
            for hive_id in sorted(self.feed.tracked_hive_ids()):
                self.feed.remove_hive(hive_id)
            with self._owned_source_lock:
                if self._owned_source_reads:
                    raise RuntimeError("state broker close left an owned source read")
                self._closed = True

    def component(self) -> LifespanComponent:
        relay_component = self.relay.component()

        @asynccontextmanager
        async def lifespan(app):
            async with relay_component.lifespan(app):
                self._registry_task = asyncio.create_task(
                    self._reconcile_registry_loop(),
                    name="daemon-state-broker:registry",
                )
                try:
                    yield
                finally:
                    await self.close()

        return LifespanComponent(
            name="daemon-state-broker",
            lifespan=lifespan,
            startup_phase=relay_component.startup_phase,
            shutdown_phase=relay_component.shutdown_phase,
        )


__all__ = ["DaemonStateBroker", "StateBroker"]
