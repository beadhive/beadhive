"""Authoritative per-hive OperatorEvent sequencing and SSE transport."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TypeVar

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from .host_daemon import (
    DaemonRuntime,
    LifespanComponent,
    ShutdownPhase,
    StartupPhase,
)
from .operator_api import canonical_hive_parameter, error_payload
from .operator_feed import FeedInstall, FeedPulse, FeedTransition, OperatorFeed
from .operator_sources import OperatorSourceError

logger = logging.getLogger(__name__)
_FeedResult = TypeVar("_FeedResult")

EVENT_NAME = "operator-event"
DEFAULT_REPLAY_EVENTS = 5_000
DEFAULT_REPLAY_BYTES = 8 * 1024 * 1024
DEFAULT_GLOBAL_REPLAY_EVENTS = 20_000
DEFAULT_GLOBAL_REPLAY_BYTES = 32 * 1024 * 1024
DEFAULT_CLIENT_QUEUE_EVENTS = 1_000
DEFAULT_CLIENT_QUEUE_BYTES = 1024 * 1024
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_HEARTBEAT_INTERVAL = 15.0

_CURSOR = re.compile(r"^([A-Za-z0-9._~-]+):(0|[1-9][0-9]*)$")
_ENTITY_COLLECTIONS = (
    ("workItems", "beads"),
    ("dependencies", "beads"),
    ("epics", "beads"),
    ("gates", "beads"),
    ("agents", "runtime"),
    ("assignments", "beads"),
    ("schedules", "beads"),
    ("evidence", "beads"),
)


class ResnapshotRequired(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EventCursor:
    producer_epoch: str
    sequence: int

    @classmethod
    def parse(cls, raw: str) -> EventCursor:
        match = _CURSOR.fullmatch(raw)
        if match is None:
            raise OperatorSourceError(
                "invalid_event_cursor",
                "Event cursor must be producerEpoch:sequence.",
                status_code=400,
            )
        return cls(match.group(1), int(match.group(2)))

    def render(self) -> str:
        return f"{self.producer_epoch}:{self.sequence}"


@dataclass(frozen=True)
class RelayEvent:
    serial: int
    hive_id: str
    producer_epoch: str
    sequence: int
    base_sequence: int
    payload: Mapping[str, object]
    frame: bytes

    @property
    def size(self) -> int:
        return len(self.frame)


@dataclass(eq=False)
class EventSubscription:
    """One independently bounded client queue."""

    relay: OperatorEventRelay
    hive_id: str
    loop: asyncio.AbstractEventLoop
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    queue: deque[bytes] = field(default_factory=deque)
    queued_bytes: int = 0
    closed: bool = False
    close_reason: str | None = None

    def close(self, reason: str = "client_closed") -> None:
        self.relay.unsubscribe(self, reason=reason)

    async def frames(self) -> AsyncIterator[bytes]:
        try:
            while True:
                frame, closed = self.relay._take(self)
                if frame is not None:
                    yield frame
                    continue
                if closed:
                    return
                await self.wakeup.wait()
        finally:
            self.close()


@dataclass
class _HiveRelayState:
    hive_id: str
    subscription_id: str
    producer_epoch: str = ""
    sequence: int = 0
    source_revision: str = ""
    initialized: bool = False
    history: deque[RelayEvent] = field(default_factory=deque)
    history_bytes: int = 0
    clients: set[EventSubscription] = field(default_factory=set)
    last_emit: float = 0.0


def _positive_limit(value: int, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _cursor_values(request: Request) -> str | None:
    groups = {
        "Last-Event-ID": request.headers.getlist("last-event-id"),
        "after": request.query_params.getlist("after"),
        "cursor": request.query_params.getlist("cursor"),
    }
    present: list[str] = []
    for name, values in groups.items():
        if len(values) > 1:
            raise OperatorSourceError(
                "conflicting_event_cursors",
                f"Event requests accept at most one {name} cursor.",
                status_code=400,
            )
        if values:
            EventCursor.parse(values[0])
            present.append(values[0])
    if len(set(present)) > 1:
        raise OperatorSourceError(
            "conflicting_event_cursors",
            "Last-Event-ID, after, and cursor must be byte-identical when combined.",
            status_code=400,
        )
    return present[0] if present else None


def _subscription_value(request: Request) -> str:
    values = request.query_params.getlist("subscription")
    if len(values) != 1 or not values[0] or len(values[0]) > 512:
        raise OperatorSourceError(
            "invalid_event_subscription",
            "Event requests require one exact subscription value.",
            status_code=400,
        )
    return values[0]


def _resnapshot(code: str) -> JSONResponse:
    return JSONResponse({"error": code, "action": "resnapshot"}, status_code=409)


def _entity_map(
    snapshot: Mapping[str, object], collection: str
) -> dict[tuple[str, str, str], dict]:
    raw = snapshot.get(collection)
    if not isinstance(raw, list):
        raise RuntimeError(f"operator snapshot {collection} must be an array")
    result: dict[tuple[str, str, str], dict] = {}
    for value in raw:
        if not isinstance(value, dict) or not isinstance(value.get("ref"), dict):
            raise RuntimeError(f"operator snapshot {collection} has an invalid entity")
        ref = value["ref"]
        key = (str(ref.get("hiveId", "")), str(ref.get("kind", "")), str(ref.get("id", "")))
        if not all(key) or key in result:
            raise RuntimeError(f"operator snapshot {collection} has an invalid entity identity")
        result[key] = value
    return result


class OperatorEventRelay:
    """One in-process ordering authority for every exact per-hive feed."""

    def __init__(
        self,
        feed: OperatorFeed,
        runtime: DaemonRuntime,
        *,
        now_millis: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        replay_events: int = DEFAULT_REPLAY_EVENTS,
        replay_bytes: int = DEFAULT_REPLAY_BYTES,
        global_replay_events: int = DEFAULT_GLOBAL_REPLAY_EVENTS,
        global_replay_bytes: int = DEFAULT_GLOBAL_REPLAY_BYTES,
        client_queue_events: int = DEFAULT_CLIENT_QUEUE_EVENTS,
        client_queue_bytes: int = DEFAULT_CLIENT_QUEUE_BYTES,
    ) -> None:
        self.feed = feed
        self.runtime = runtime
        self._now_millis = now_millis or (lambda: time.time_ns() // 1_000_000)
        self._monotonic = monotonic
        self.poll_interval = max(0.001, float(poll_interval))
        self.heartbeat_interval = max(0.001, float(heartbeat_interval))
        self.replay_event_limit = _positive_limit(replay_events, "replay_events")
        self.replay_byte_limit = _positive_limit(replay_bytes, "replay_bytes")
        self.global_event_limit = _positive_limit(global_replay_events, "global_replay_events")
        self.global_byte_limit = _positive_limit(global_replay_bytes, "global_replay_bytes")
        self.client_event_limit = _positive_limit(client_queue_events, "client_queue_events")
        self.client_byte_limit = _positive_limit(client_queue_bytes, "client_queue_bytes")
        self._lock = threading.RLock()
        self._hives: dict[str, _HiveRelayState] = {}
        self._retained: OrderedDict[int, tuple[_HiveRelayState, RelayEvent]] = OrderedDict()
        self._retained_bytes = 0
        self._serial = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._workers: set[asyncio.Future[object]] = set()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._slow_disconnects = 0
        self._remove_transition = feed.register_transition_handler(self._on_transition)
        self._remove_install = feed.register_install_observer(self._on_install)

    def _state(self, hive_id: str) -> _HiveRelayState:
        return self._hives.setdefault(
            hive_id,
            _HiveRelayState(hive_id=hive_id, subscription_id=f"hive:{hive_id}"),
        )

    def _on_install(self, install: FeedInstall) -> None:
        cursor = install.current.get("cursor")
        if not isinstance(cursor, Mapping):
            raise RuntimeError("installed operator snapshot requires a cursor")
        with self._lock:
            state = self._state(install.hive_id)
            epoch = str(cursor.get("producerEpoch", ""))
            sequence = cursor.get("sequence")
            subscription = str(cursor.get("subscriptionId", ""))
            if not epoch or type(sequence) is not int or sequence < 0:
                raise RuntimeError("installed operator snapshot has an invalid cursor")
            if subscription != state.subscription_id:
                raise RuntimeError("installed operator snapshot changed its logical subscription")
            if state.initialized and (epoch, sequence) != (state.producer_epoch, state.sequence):
                raise RuntimeError("installed snapshot cursor disagrees with relay ordering state")
            state.producer_epoch = epoch
            state.sequence = sequence
            state.source_revision = install.source_revision
            state.initialized = True
            state.last_emit = state.last_emit or self._monotonic()

    def _on_transition(self, transition: FeedTransition) -> int:
        with self._lock:
            state = self._state(transition.hive_id)
            if self._closed:
                return 1
            if transition.reset_reason is not None:
                self._clear_history_locked(state)
                for client in state.clients:
                    client.queue.clear()
                    client.queued_bytes = 0
                    client.wakeup.clear()
                state.producer_epoch = transition.producer_epoch
                state.sequence = 0
                self._append_locked(
                    state,
                    source="beads",
                    revision=transition.source_revision,
                    observed_at=self._now_millis(),
                    generated_at=int(transition.current.get("generatedAt", self._now_millis())),
                    entity=None,
                    payload={"kind": "reset", "reason": transition.reset_reason},
                )
                return 1

            if not state.initialized:
                state.producer_epoch = transition.producer_epoch
                state.sequence = transition.base_sequence
                state.initialized = True
            if (state.producer_epoch, state.sequence) != (
                transition.producer_epoch,
                transition.base_sequence,
            ):
                raise RuntimeError("feed transition does not continue the relay cursor")

            events = self._diff_events(transition)
            for source, revision, entity, payload in events:
                self._append_locked(
                    state,
                    source=source,
                    revision=revision,
                    observed_at=self._now_millis(),
                    generated_at=int(transition.current.get("generatedAt", self._now_millis())),
                    entity=entity,
                    payload=payload,
                )
            return len(events)

    def _diff_events(
        self, transition: FeedTransition
    ) -> list[tuple[str, str, dict | None, dict[str, object]]]:
        result: list[tuple[str, str, dict | None, dict[str, object]]] = []
        for collection, default_source in _ENTITY_COLLECTIONS:
            previous = _entity_map(transition.previous, collection)
            current = _entity_map(transition.current, collection)
            for key in sorted(current):
                entity = current[key]
                if previous.get(key) == entity:
                    continue
                ref = dict(entity["ref"])
                source = (
                    "runtime"
                    if collection == "assignments" and str(ref["id"]).startswith("runtime:")
                    else default_source
                )
                result.append(
                    (
                        source,
                        str(entity.get("revision", transition.source_revision)),
                        ref,
                        {"kind": "entity-upsert", "entity": entity},
                    )
                )
            for key in sorted(previous.keys() - current.keys()):
                ref = dict(previous[key]["ref"])
                source = (
                    "runtime"
                    if collection == "assignments" and str(ref["id"]).startswith("runtime:")
                    else default_source
                )
                result.append(
                    (
                        source,
                        transition.source_revision,
                        ref,
                        {
                            "kind": "entity-remove",
                            "entity": ref,
                            "revision": transition.source_revision,
                        },
                    )
                )

        scopes: list[str] = []
        if transition.previous.get("coverage") != transition.current.get("coverage"):
            scopes.append("coverage")
        if transition.previous.get("advertisedActions") != transition.current.get(
            "advertisedActions"
        ):
            scopes.append("capabilities")
        if transition.previous.get("hive") != transition.current.get("hive"):
            scopes.append("snapshot")
        if scopes:
            result.append(
                (
                    "beads",
                    transition.source_revision,
                    None,
                    {
                        "kind": "invalidate",
                        "scopes": scopes,
                        "reason": "authoritative snapshot metadata changed",
                    },
                )
            )
        if not result:
            result.append(
                (
                    "beads",
                    transition.source_revision,
                    None,
                    {
                        "kind": "invalidate",
                        "scopes": ["snapshot"],
                        "reason": "authoritative source revision changed",
                    },
                )
            )
        return result

    def _heartbeat(self, pulse: FeedPulse) -> int:
        with self._lock:
            state = self._state(pulse.hive_id)
            if self._closed:
                return 1
            if (state.producer_epoch, state.sequence) != (
                pulse.producer_epoch,
                pulse.base_sequence,
            ):
                raise RuntimeError("heartbeat does not continue the installed snapshot cursor")
            self._append_locked(
                state,
                source="supervisor",
                revision=pulse.source_revision,
                observed_at=self._now_millis(),
                generated_at=self._now_millis(),
                entity=None,
                payload={"kind": "heartbeat"},
            )
            return 1

    def _append_locked(
        self,
        state: _HiveRelayState,
        *,
        source: str,
        revision: str,
        observed_at: int,
        generated_at: int,
        entity: dict | None,
        payload: dict[str, object],
    ) -> RelayEvent:
        sequence = state.sequence + 1
        envelope: dict[str, object] = {
            "schemaVersion": 1,
            "hiveId": state.hive_id,
            "subscriptionId": state.subscription_id,
            "producerEpoch": state.producer_epoch,
            "sequence": sequence,
            "baseSequence": state.sequence,
            "observedAt": observed_at,
            "generatedAt": generated_at,
            "source": source,
            "revision": revision,
            "entity": entity,
            "payload": payload,
        }
        self._validate_envelope(envelope)
        event_id = f"{state.producer_epoch}:{sequence}"
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        frame = f"event: {EVENT_NAME}\nid: {event_id}\ndata: {encoded}\n\n".encode()
        self._serial += 1
        event = RelayEvent(
            serial=self._serial,
            hive_id=state.hive_id,
            producer_epoch=state.producer_epoch,
            sequence=sequence,
            base_sequence=state.sequence,
            payload=envelope,
            frame=frame,
        )
        state.sequence = sequence
        state.source_revision = revision
        state.last_emit = self._monotonic()
        self._retain_locked(state, event)
        for client in tuple(state.clients):
            self._enqueue_locked(client, frame)
        return event

    @staticmethod
    def _validate_envelope(event: Mapping[str, object]) -> None:
        sequence = event["sequence"]
        base = event["baseSequence"]
        if (
            type(sequence) is not int
            or sequence < 1
            or type(base) is not int
            or base != sequence - 1
        ):
            raise RuntimeError("operator event sequence must be positive and continue baseSequence")
        entity = event["entity"]
        payload = event["payload"]
        if not isinstance(payload, Mapping):
            raise RuntimeError("operator event payload must be an object")
        kind = payload.get("kind")
        if kind == "entity-upsert":
            nested = payload.get("entity")
            if not isinstance(nested, Mapping) or nested.get("ref") != entity:
                raise RuntimeError("entity-upsert envelope and payload identities must agree")
        elif kind == "entity-remove":
            if payload.get("entity") != entity:
                raise RuntimeError("entity-remove envelope and payload identities must agree")
        elif entity is not None:
            raise RuntimeError("control events cannot carry an entity")

    def _retain_locked(self, state: _HiveRelayState, event: RelayEvent) -> None:
        state.history.append(event)
        state.history_bytes += event.size
        self._retained[event.serial] = (state, event)
        self._retained_bytes += event.size
        while (
            len(state.history) > self.replay_event_limit
            or state.history_bytes > self.replay_byte_limit
        ):
            self._drop_oldest_locked(state)
        while (
            len(self._retained) > self.global_event_limit
            or self._retained_bytes > self.global_byte_limit
        ):
            _, (old_state, old_event) = self._retained.popitem(last=False)
            if not old_state.history or old_state.history[0] is not old_event:
                raise RuntimeError("global replay order disagrees with per-hive retention")
            old_state.history.popleft()
            old_state.history_bytes -= old_event.size
            self._retained_bytes -= old_event.size

    def _drop_oldest_locked(self, state: _HiveRelayState) -> None:
        event = state.history.popleft()
        state.history_bytes -= event.size
        retained = self._retained.pop(event.serial, None)
        if retained is not None:
            self._retained_bytes -= event.size

    def _clear_history_locked(self, state: _HiveRelayState) -> None:
        while state.history:
            self._drop_oldest_locked(state)

    def _enqueue_locked(self, client: EventSubscription, frame: bytes) -> bool:
        if client.closed:
            return False
        if (
            len(client.queue) + 1 > self.client_event_limit
            or client.queued_bytes + len(frame) > self.client_byte_limit
        ):
            self._disconnect_locked(client, "slow_consumer")
            return False
        client.queue.append(frame)
        client.queued_bytes += len(frame)
        self._wake(client)
        return True

    @staticmethod
    def _wake(client: EventSubscription) -> None:
        try:
            client.loop.call_soon_threadsafe(client.wakeup.set)
        except RuntimeError:
            client.closed = True
            client.close_reason = client.close_reason or "event_loop_closed"

    def _disconnect_locked(self, client: EventSubscription, reason: str) -> None:
        if client.closed:
            return
        client.closed = True
        client.close_reason = reason
        client.queue.clear()
        client.queued_bytes = 0
        state = self._hives.get(client.hive_id)
        if state is not None:
            state.clients.discard(client)
        if reason == "slow_consumer":
            self._slow_disconnects += 1
            logger.warning(
                "operator SSE client disconnected after exceeding its bounded queue",
                extra={"hive_id": client.hive_id, "disconnect_reason": reason},
            )
        self._wake(client)

    def subscribe(
        self,
        hive_id: str,
        *,
        subscription_id: str,
        cursor: EventCursor,
        loop: asyncio.AbstractEventLoop,
    ) -> EventSubscription:
        with self._lock:
            state = self._state(hive_id)
            if not state.initialized:
                raise ResnapshotRequired("snapshot_required")
            if subscription_id != state.subscription_id:
                raise ResnapshotRequired("wrong_subscription")
            if cursor.producer_epoch != state.producer_epoch:
                raise ResnapshotRequired("cursor_epoch_expired")
            if cursor.sequence > state.sequence:
                raise ResnapshotRequired("cursor_in_future")

            replay = [event for event in state.history if event.sequence > cursor.sequence]
            if cursor.sequence < state.sequence:
                if not replay or replay[0].base_sequence != cursor.sequence:
                    raise ResnapshotRequired("cursor_expired")
                expected = cursor.sequence
                for event in replay:
                    if event.base_sequence != expected or event.sequence != expected + 1:
                        raise ResnapshotRequired("cursor_gap")
                    expected = event.sequence
                if expected != state.sequence:
                    raise ResnapshotRequired("cursor_gap")

            replay_bytes = sum(event.size for event in replay)
            if len(replay) > self.client_event_limit or replay_bytes > self.client_byte_limit:
                raise ResnapshotRequired("replay_exceeds_client_capacity")
            client = EventSubscription(self, hive_id, loop)
            for event in replay:
                client.queue.append(event.frame)
                client.queued_bytes += event.size
            state.clients.add(client)
            if replay:
                self._wake(client)
            return client

    def unsubscribe(self, client: EventSubscription, *, reason: str = "client_closed") -> None:
        with self._lock:
            self._disconnect_locked(client, reason)

    def _take(self, client: EventSubscription) -> tuple[bytes | None, bool]:
        with self._lock:
            if client.queue:
                frame = client.queue.popleft()
                client.queued_bytes -= len(frame)
                if not client.queue:
                    client.wakeup.clear()
                return frame, False
            client.wakeup.clear()
            return None, client.closed

    async def events(self, request: Request):
        try:
            identity = canonical_hive_parameter(request, suffix=b"/events")
            raw_cursor = _cursor_values(request)
            subscription_id = _subscription_value(request)
            if raw_cursor is None:
                return _resnapshot("cursor_required")
            cursor = EventCursor.parse(raw_cursor)
            installed = await asyncio.to_thread(self.feed.installed_snapshot, identity)
            if installed is None:
                return _resnapshot("snapshot_required")
            client = self.subscribe(
                identity,
                subscription_id=subscription_id,
                cursor=cursor,
                loop=asyncio.get_running_loop(),
            )
            self._start_pump(identity)
        except OperatorSourceError as exc:
            return JSONResponse(error_payload(exc), status_code=exc.status_code)
        except ResnapshotRequired as exc:
            return _resnapshot(exc.code)

        async def stream() -> AsyncIterator[bytes]:
            async for frame in client.frames():
                yield frame

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    def _has_clients(self, hive_id: str) -> bool:
        with self._lock:
            state = self._hives.get(hive_id)
            return bool(state and state.clients)

    def _start_pump(self, hive_id: str) -> None:
        if self._closing or self._closed:
            return
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        task = self._pumps.get(hive_id)
        if task is None or task.done():
            self._pumps[hive_id] = self._loop.create_task(
                self._pump(hive_id), name=f"operator-sse:{hive_id}"
            )

    async def _run_feed_call(
        self, function: Callable[..., _FeedResult], *args: object
    ) -> _FeedResult:
        """Run one feed call without abandoning its worker during cancellation.

        ``asyncio`` cannot stop a thread-pool function once it has begun. Shielding and then
        draining the future makes the relay's observer lifetime cover every source install the
        worker can still perform.
        """

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, function, *args)
        with self._lock:
            self._workers.add(future)
        cancelled = False
        try:
            while True:
                try:
                    result = await asyncio.shield(future)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if future.done():
                        result = future.result()
                        break
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            with self._lock:
                self._workers.discard(future)

    async def _pump(self, hive_id: str) -> None:
        try:
            while (
                not self._closing
                and not self._closed
                and self.runtime.accepting
                and self._has_clients(hive_id)
            ):
                await asyncio.sleep(self.poll_interval)
                try:
                    await self._run_feed_call(self.feed.snapshot_with_cursor, hive_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                with self._lock:
                    state = self._hives.get(hive_id)
                    heartbeat_due = bool(
                        state
                        and state.clients
                        and self._monotonic() - state.last_emit >= self.heartbeat_interval
                    )
                if heartbeat_due:
                    try:
                        await self._run_feed_call(
                            self.feed.allocate_events, hive_id, self._heartbeat
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue
        finally:
            current = asyncio.current_task()
            if self._pumps.get(hive_id) is current:
                self._pumps.pop(hive_id, None)

    def retained_state(self) -> dict[str, object]:
        with self._lock:
            return {
                "events": len(self._retained),
                "bytes": self._retained_bytes,
                "clients": sum(len(state.clients) for state in self._hives.values()),
                "slowDisconnects": self._slow_disconnects,
                "inFlightFeedCalls": len(self._workers),
                "closing": self._closing,
                "hives": {
                    hive_id: {
                        "events": len(state.history),
                        "bytes": state.history_bytes,
                        "sequence": state.sequence,
                        "producerEpoch": state.producer_epoch,
                    }
                    for hive_id, state in self._hives.items()
                },
            }

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            tasks = tuple(self._pumps.values())
            for task in tasks:
                task.cancel()
            self.feed.cancel_source_reads()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            with self._lock:
                if self._workers:
                    raise RuntimeError("operator SSE close left an in-flight feed call")
                for state in self._hives.values():
                    for client in tuple(state.clients):
                        self._disconnect_locked(client, "daemon_shutdown")
                    self._clear_history_locked(state)
            self._remove_transition()
            self._remove_install()
            self._closed = True

    def component(self) -> LifespanComponent:
        @asynccontextmanager
        async def lifespan(_app):
            self._loop = asyncio.get_running_loop()
            try:
                yield
            finally:
                await self.close()

        return LifespanComponent(
            name="operator-sse",
            lifespan=lifespan,
            startup_phase=StartupPhase.RESOURCES,
            shutdown_phase=ShutdownPhase.CLOSE_SESSIONS,
        )
