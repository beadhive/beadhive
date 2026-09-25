"""Authoritative per-hive OperatorEvent sequencing and SSE transport."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from . import operator_contract
from .daemon_auth import (
    AuthenticatedPrincipal,
    AuthenticationError,
    AuthFailureCode,
    CredentialSession,
    CredentialSessionRegistry,
    SecretBearer,
    authentication_error_response,
)
from .daemon_contract import AuthScope
from .host_daemon import (
    DaemonRuntime,
    LifespanComponent,
    ShutdownPhase,
    StartupPhase,
)
from .operator_api import canonical_hive_parameter, error_payload
from .operator_feed import ActivityInstall, FeedInstall, FeedPulse, FeedTransition, OperatorFeed
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
MAX_EVENT_CURSOR_EPOCH_LENGTH = 64
MAX_EVENT_CURSOR_SEQUENCE_DIGITS = 20
MAX_EVENT_CURSOR_LENGTH = MAX_EVENT_CURSOR_EPOCH_LENGTH + 1 + MAX_EVENT_CURSOR_SEQUENCE_DIGITS

_CURSOR = re.compile(
    rf"^([A-Za-z0-9._~-]{{1,{MAX_EVENT_CURSOR_EPOCH_LENGTH}}}):"
    rf"(0|[1-9][0-9]{{0,{MAX_EVENT_CURSOR_SEQUENCE_DIGITS - 1}}})$"
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
        if (
            not isinstance(raw, str)
            or len(raw) > MAX_EVENT_CURSOR_LENGTH
            or len(raw.encode("utf-8", errors="surrogatepass")) > MAX_EVENT_CURSOR_LENGTH
        ):
            raise OperatorSourceError(
                "invalid_event_cursor",
                "Event cursor must be a bounded producerEpoch:sequence value.",
                status_code=400,
            )
        match = _CURSOR.fullmatch(raw)
        if match is None:
            raise OperatorSourceError(
                "invalid_event_cursor",
                "Event cursor must be producerEpoch:sequence.",
                status_code=400,
            )
        try:
            sequence = int(match.group(2))
        except ValueError:
            raise OperatorSourceError(
                "invalid_event_cursor",
                "Event cursor must be producerEpoch:sequence.",
                status_code=400,
            ) from None
        return cls(match.group(1), sequence)

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
    telemetry_connection: Any = None

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
        feed_runner: Callable[..., Awaitable[Any]] | None = None,
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
        self._feed_runner = feed_runner
        self._lock = threading.RLock()
        self._hives: dict[str, _HiveRelayState] = {}
        self._retained: OrderedDict[int, tuple[_HiveRelayState, RelayEvent]] = OrderedDict()
        self._retained_bytes = 0
        self._serial = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._removal_admissions: dict[str, int] = {}
        self._workers: set[asyncio.Future[object]] = set()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._slow_disconnects = 0
        self.telemetry: Any | None = None
        self._remove_transition = feed.register_transition_handler(self._on_transition)
        self._remove_install = feed.register_install_observer(self._on_install)
        self._remove_activity = feed.register_activity_observer(self._on_activity)

    def _state(self, hive_id: str) -> _HiveRelayState:
        return self._hives.setdefault(
            hive_id,
            _HiveRelayState(
                hive_id=hive_id,
                subscription_id=operator_contract.hive_subscription_id(hive_id),
            ),
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

    def _on_activity(self, install: ActivityInstall) -> None:
        with self._lock:
            if self._closing or self._closed:
                return
            state = self._hives.get(install.hive_id)
            if (
                state is None
                or not state.initialized
                or not state.clients
                or self._removal_admissions.get(install.hive_id, 0)
            ):
                return
        try:
            self.feed.allocate_events(
                install.hive_id,
                lambda pulse: self._allocate_activity(pulse, install),
            )
        except OperatorSourceError as exc:
            if exc.code not in {
                "snapshot_required",
                "hive_generation_expired",
                "hive_not_found",
            }:
                raise

    def _allocate_activity(self, pulse: FeedPulse, install: ActivityInstall) -> int:
        with self._lock:
            state = self._state(pulse.hive_id)
            if self._closed:
                return 1
            if (state.producer_epoch, state.sequence) != (
                pulse.producer_epoch,
                pulse.base_sequence,
            ):
                raise RuntimeError("activity does not continue the installed snapshot cursor")
            return self._publish_activity_locked(state, install)

    def _publish_activity_locked(self, state: _HiveRelayState, install: ActivityInstall) -> int:
        if install.reset_reason is not None:
            now = self._now_millis()
            events = [
                (
                    "runtime",
                    install.source_revision,
                    None,
                    {
                        "kind": "activity-reset",
                        "runId": install.run_id,
                        "producerEpoch": install.producer_epoch,
                        "reason": install.reset_reason,
                    },
                )
            ]
            self._preflight_batch(state, events, observed_at=now, generated_at=now)
            if self.telemetry is not None:
                self.telemetry.record_reset("source_discontinuity")
            self._append_locked(
                state,
                source=events[0][0],
                revision=events[0][1],
                observed_at=now,
                generated_at=now,
                entity=events[0][2],
                payload=events[0][3],
            )
            return 1
        if install.added_records is not None:
            if not install.added_records:
                raise RuntimeError("activity page install must add at least one record")
            added = operator_contract.run_activity_envelopes(
                install.added_records,
                producer_epoch=install.producer_epoch,
                sequence_offset=install.sequence_offset,
                first_occurred_at=install.first_occurred_at,
            )
        else:
            previous_count = len(install.previous_records)
            if tuple(install.current_records[:previous_count]) != install.previous_records:
                raise RuntimeError("activity install must append or carry an explicit reset")
            activities = operator_contract.run_activity_envelopes(
                install.current_records, producer_epoch=install.producer_epoch
            )
            added = activities[previous_count:]
        if not added:
            raise RuntimeError("activity install must add an event or carry an explicit reset")
        generated_at = self._now_millis()
        events = [
            (
                "runtime",
                str(activity["sourceRevision"]),
                None,
                {
                    "kind": "activity",
                    "runId": install.run_id,
                    "activity": activity,
                },
            )
            for activity in added
        ]
        observed_times = [int(activity["occurredAt"]) for activity in added]
        self._preflight_batch(
            state,
            events,
            observed_at=observed_times,
            generated_at=generated_at,
        )
        for event, observed_at in zip(events, observed_times, strict=True):
            self._append_locked(
                state,
                source=event[0],
                revision=event[1],
                observed_at=observed_at,
                generated_at=generated_at,
                entity=event[2],
                payload=event[3],
            )
        return len(added)

    def _on_transition(self, transition: FeedTransition) -> int:
        with self._lock:
            state = self._state(transition.hive_id)
            if self._closed:
                return 1
            if transition.reset_reason is not None:
                observed_at = self._now_millis()
                generated_at = int(transition.current.get("generatedAt", observed_at))
                events = [
                    (
                        "beads",
                        transition.source_revision,
                        None,
                        {"kind": "reset", "reason": transition.reset_reason},
                    )
                ]
                self._preflight_batch(
                    state,
                    events,
                    observed_at=observed_at,
                    generated_at=generated_at,
                    base_sequence=0,
                    producer_epoch=transition.producer_epoch,
                )
                if self.telemetry is not None:
                    self.telemetry.record_reset("source_discontinuity")
                self._clear_history_locked(state)
                for client in state.clients:
                    client.queue.clear()
                    client.queued_bytes = 0
                    client.wakeup.clear()
                state.producer_epoch = transition.producer_epoch
                state.sequence = 0
                self._append_locked(
                    state,
                    source=events[0][0],
                    revision=events[0][1],
                    observed_at=observed_at,
                    generated_at=generated_at,
                    entity=events[0][2],
                    payload=events[0][3],
                )
                # The reset is the last frame an old-epoch subscription may consume.  Detach
                # those clients from live publication without clearing the just-enqueued reset;
                # their generators drain it and then end, forcing a replacement snapshot before
                # any event in the new epoch can be applied.
                for client in tuple(state.clients):
                    self._retire_after_drain_locked(client, "resnapshot_required")
                return 1

            if state.initialized and (state.producer_epoch, state.sequence) != (
                transition.producer_epoch,
                transition.base_sequence,
            ):
                raise RuntimeError("feed transition does not continue the relay cursor")

            events = self._diff_events(transition)
            observed_at = self._now_millis()
            generated_at = int(transition.current.get("generatedAt", observed_at))
            self._preflight_batch(
                state,
                events,
                observed_at=observed_at,
                generated_at=generated_at,
                base_sequence=transition.base_sequence,
                producer_epoch=transition.producer_epoch,
            )
            if not state.initialized:
                state.producer_epoch = transition.producer_epoch
                state.sequence = transition.base_sequence
                state.initialized = True
            for source, revision, entity, payload in events:
                self._append_locked(
                    state,
                    source=source,
                    revision=revision,
                    observed_at=observed_at,
                    generated_at=generated_at,
                    entity=entity,
                    payload=payload,
                )
            return len(events)

    def _diff_events(
        self, transition: FeedTransition
    ) -> list[tuple[str, str, dict | None, dict[str, object]]]:
        scopes = ["snapshot"]
        if transition.previous.get("coverage") != transition.current.get("coverage"):
            scopes.append("coverage")
        return [
            (
                "beads",
                transition.source_revision,
                None,
                {
                    "kind": "invalidate",
                    "scopes": scopes,
                    "reason": "authoritative hive snapshot changed",
                },
            )
        ]

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
            now = self._now_millis()
            self._append_locked(
                state,
                source="supervisor",
                revision=pulse.source_revision,
                observed_at=now,
                generated_at=now,
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
        envelope = self._event_envelope(
            state,
            source=source,
            revision=revision,
            observed_at=observed_at,
            generated_at=generated_at,
            entity=entity,
            payload=payload,
            base_sequence=state.sequence,
            producer_epoch=state.producer_epoch,
        )
        self._validate_envelope(envelope)
        sequence = int(envelope["sequence"])
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
    def _event_envelope(
        state: _HiveRelayState,
        *,
        source: str,
        revision: str,
        observed_at: int,
        generated_at: int,
        entity: dict | None,
        payload: dict[str, object],
        base_sequence: int,
        producer_epoch: str,
    ) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "hiveId": state.hive_id,
            "subscriptionId": state.subscription_id,
            "producerEpoch": producer_epoch,
            "sequence": base_sequence + 1,
            "baseSequence": base_sequence,
            "observedAt": observed_at,
            "generatedAt": generated_at,
            "source": source,
            "revision": revision,
            "entity": entity,
            "payload": payload,
        }

    def _preflight_batch(
        self,
        state: _HiveRelayState,
        events: list[tuple[str, str, dict | None, dict[str, object]]],
        *,
        observed_at: int | list[int],
        generated_at: int,
        base_sequence: int | None = None,
        producer_epoch: str | None = None,
    ) -> None:
        if not events:
            raise RuntimeError("operator event batch must not be empty")
        observed_times = (
            observed_at if isinstance(observed_at, list) else [observed_at] * len(events)
        )
        if len(observed_times) != len(events):
            raise RuntimeError("operator event batch timestamps disagree")
        base = state.sequence if base_sequence is None else base_sequence
        epoch = state.producer_epoch if producer_epoch is None else producer_epoch
        for offset, (event, event_observed_at) in enumerate(
            zip(events, observed_times, strict=True)
        ):
            source, revision, entity, payload = event
            envelope = self._event_envelope(
                state,
                source=source,
                revision=revision,
                observed_at=event_observed_at,
                generated_at=generated_at,
                entity=entity,
                payload=payload,
                base_sequence=base + offset,
                producer_epoch=epoch,
            )
            self._validate_envelope(envelope)
            json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    @staticmethod
    def _validate_envelope(event: Mapping[str, object]) -> None:
        hive_id = event.get("hiveId")
        subscription_id = event.get("subscriptionId")
        if not isinstance(hive_id, str) or subscription_id != (
            operator_contract.hive_subscription_id(hive_id)
        ):
            raise RuntimeError("operator event subscription must match its canonical hive")
        sequence = event["sequence"]
        base = event["baseSequence"]
        if (
            type(sequence) is not int
            or sequence < 1
            or sequence > operator_contract.DEVELOPMENT_MAX_CURSOR_SEQUENCE
            or type(base) is not int
            or base != sequence - 1
        ):
            raise RuntimeError(
                "operator event sequence must fit the cursor contract and continue baseSequence"
            )
        for timestamp_field in ("observedAt", "generatedAt"):
            timestamp = event[timestamp_field]
            if (
                type(timestamp) is not int
                or not 0 <= timestamp <= operator_contract.DEVELOPMENT_MAX_JSON_SAFE_INTEGER
            ):
                raise RuntimeError(
                    f"operator event {timestamp_field} must be a JSON-safe timestamp"
                )
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
        self._sync_queue_telemetry_locked()

    def _drop_oldest_locked(self, state: _HiveRelayState) -> None:
        event = state.history.popleft()
        state.history_bytes -= event.size
        retained = self._retained.pop(event.serial, None)
        if retained is not None:
            self._retained_bytes -= event.size
        self._sync_queue_telemetry_locked()

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
        self._sync_queue_telemetry_locked()
        self._wake_locked(client)
        return True

    def _sync_queue_telemetry_locked(self) -> None:
        if self.telemetry is None:
            return
        self.telemetry.set_queue_depth(
            "sse-client",
            sum(len(client.queue) for state in self._hives.values() for client in state.clients),
        )
        self.telemetry.set_queue_depth("sse-replay", len(self._retained))

    def _wake_locked(self, client: EventSubscription) -> None:
        """Wake one client or atomically release every retained reference to it."""

        try:
            client.loop.call_soon_threadsafe(client.wakeup.set)
        except RuntimeError:
            self._detach_client_locked(client, "event_loop_closed", clear_queue=True)

    def _detach_client_locked(
        self,
        client: EventSubscription,
        reason: str,
        *,
        clear_queue: bool,
    ) -> bool:
        """Detach under ``_lock`` and report whether this was a new disconnect."""

        state = self._hives.get(client.hive_id)
        attached = state is not None and client in state.clients
        newly_closed = not client.closed
        client.closed = True
        client.close_reason = client.close_reason or reason
        if clear_queue:
            client.queue.clear()
            client.queued_bytes = 0
        if state is not None:
            state.clients.discard(client)
        if client.telemetry_connection is not None and self.telemetry is not None:
            self.telemetry.close_connection(client.telemetry_connection, reason=reason)
            client.telemetry_connection = None
        self._sync_queue_telemetry_locked()
        return newly_closed or attached

    def _disconnect_locked(self, client: EventSubscription, reason: str) -> None:
        disconnected = self._detach_client_locked(client, reason, clear_queue=True)
        if disconnected and reason == "slow_consumer":
            self._slow_disconnects += 1
            if self.telemetry is not None:
                self.telemetry.record_backpressure("sse", "slow_consumer")
            logger.warning(
                "operator SSE client disconnected after exceeding its bounded queue",
                extra={"hive_id": client.hive_id, "disconnect_reason": reason},
            )
        self._wake_locked(client)

    def _retire_after_drain_locked(self, client: EventSubscription, reason: str) -> None:
        """Detach a client while preserving already-queued terminal frames."""

        self._detach_client_locked(client, reason, clear_queue=False)
        self._wake_locked(client)

    def subscribe(
        self,
        hive_id: str,
        *,
        subscription_id: str,
        cursor: EventCursor,
        loop: asyncio.AbstractEventLoop,
    ) -> EventSubscription:
        with self._lock:
            state = self._hives.get(hive_id)
            if state is None or not state.initialized or self._removal_admissions.get(hive_id, 0):
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
            if self.telemetry is not None:
                client.telemetry_connection = self.telemetry.open_connection("sse")
            for event in replay:
                client.queue.append(event.frame)
                client.queued_bytes += event.size
            state.clients.add(client)
            self._sync_queue_telemetry_locked()
            if replay:
                self._wake_locked(client)
            return client

    def unsubscribe(self, client: EventSubscription, *, reason: str = "client_closed") -> None:
        with self._lock:
            self._disconnect_locked(client, reason)

    def _take(self, client: EventSubscription) -> tuple[bytes | None, bool]:
        with self._lock:
            if client.queue:
                frame = client.queue.popleft()
                client.queued_bytes -= len(frame)
                self._sync_queue_telemetry_locked()
                if not client.queue:
                    client.wakeup.clear()
                return frame, False
            client.wakeup.clear()
            return None, client.closed

    async def events(self, request: Request):
        app = request.scope.get("app")
        registry: CredentialSessionRegistry | None = getattr(
            getattr(app, "state", None), "credential_sessions", None
        )
        session: CredentialSession | None = None
        client: EventSubscription | None = None
        try:
            identity = canonical_hive_parameter(request, suffix=b"/events")
            raw_cursor = _cursor_values(request)
            subscription_id = _subscription_value(request)
            if raw_cursor is None:
                return _resnapshot("cursor_required")
            cursor = EventCursor.parse(raw_cursor)
            installed = await self._run_feed_call(self.feed.installed_snapshot, identity)
            if installed is None:
                return _resnapshot("snapshot_required")
            if registry is not None:
                bearer = getattr(request.state, "auth_bearer", None)
                principal = getattr(request.state, "auth_principal", None)
                if not isinstance(bearer, SecretBearer) or not isinstance(
                    principal, AuthenticatedPrincipal
                ):
                    raise AuthenticationError(AuthFailureCode.MISSING)

                async def close_for_auth(reason: AuthFailureCode) -> None:
                    current = client
                    if current is not None:
                        self.unsubscribe(current, reason=reason.value)

                session = registry.open(
                    bearer,
                    required_scope=AuthScope.OPERATOR_READ,
                    expected_principal=principal.principal,
                    close=close_for_auth,
                )
            client = self.subscribe(
                identity,
                subscription_id=subscription_id,
                cursor=cursor,
                loop=asyncio.get_running_loop(),
            )
            self._start_pump(identity, client=client)
        except AuthenticationError as exc:
            if session is not None and registry is not None:
                registry.unregister(session)
            return authentication_error_response(exc)
        except OperatorSourceError as exc:
            if session is not None and registry is not None:
                registry.unregister(session)
            return JSONResponse(error_payload(exc), status_code=exc.status_code)
        except ResnapshotRequired as exc:
            if self.telemetry is not None:
                reason = {
                    "cursor_epoch_expired": "unknown_epoch",
                    "cursor_in_future": "future_sequence",
                    "cursor_expired": "expired_cursor",
                    "cursor_gap": "retention_gap",
                    "replay_exceeds_client_capacity": "retention_gap",
                }.get(exc.code, "retention_gap")
                self.telemetry.record_replay_gap(reason)
            if session is not None and registry is not None:
                registry.unregister(session)
            return _resnapshot(exc.code)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for frame in client.frames():
                    yield frame
            finally:
                if session is not None and registry is not None:
                    registry.unregister(session)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    def _has_clients(self, hive_id: str) -> bool:
        with self._lock:
            state = self._hives.get(hive_id)
            return bool(state and state.clients)

    def _start_pump(self, hive_id: str, *, client: EventSubscription | None = None) -> None:
        with self._lock:
            state = self._hives.get(hive_id)
            if (
                self._closing
                or self._closed
                or self._removal_admissions.get(hive_id, 0)
                or state is None
                or not state.clients
                or (client is not None and (client.closed or client not in state.clients))
            ):
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

        if self._feed_runner is not None:
            return cast(_FeedResult, await self._feed_runner(function, *args))

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

    @staticmethod
    async def _cancel_and_drain_task(task: asyncio.Task[None]) -> bool:
        """Cancel one pump to completion and report cancellation of the calling task."""

        task.cancel()
        waiter = asyncio.gather(task, return_exceptions=True)
        cancelled = False
        while True:
            try:
                await asyncio.shield(waiter)
                return cancelled
            except asyncio.CancelledError:
                if waiter.done():
                    return cancelled
                cancelled = True

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
                except OperatorSourceError as exc:
                    if exc.code == "hive_not_found":
                        await self.remove_hive(hive_id)
                        return
                    continue
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
            with self._lock:
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

    def tracked_hive_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._hives) | frozenset(self._pumps)

    async def remove_hive(self, hive_id: str) -> None:
        """Drain and discard one exact hive without disturbing independent hive feeds."""

        with self._lock:
            self._removal_admissions[hive_id] = self._removal_admissions.get(hive_id, 0) + 1
            task = self._pumps.pop(hive_id, None)
        current = asyncio.current_task()
        cancelled = False
        if task is not None and task is not current:
            cancelled = await self._cancel_and_drain_task(task)

        # A direct snapshot request can still be finishing outside the pump.  Removing the feed
        # state first waits for that per-hive lock; relay cleanup afterwards cannot be undone by
        # a late install observer from the drained read.
        try:
            try:
                await self._run_feed_call(self.feed.begin_hive_removal, hive_id)
            except asyncio.CancelledError:
                cancelled = True
            with self._lock:
                state = self._hives.pop(hive_id, None)
                if state is not None:
                    for client in tuple(state.clients):
                        self._disconnect_locked(client, "hive_removed")
                    self._clear_history_locked(state)
        finally:
            with self._lock:
                try:
                    # Keep relay admission closed while feed admission opens.  An old
                    # subscription's delayed pump start carries its exact client token and cannot
                    # attach to a fresh relay generation after this lock is released.
                    self.feed.finish_hive_removal(hive_id)
                finally:
                    remaining = self._removal_admissions[hive_id] - 1
                    if remaining:
                        self._removal_admissions[hive_id] = remaining
                    else:
                        self._removal_admissions.pop(hive_id, None)
        if cancelled:
            raise asyncio.CancelledError

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
                self._hives.clear()
            self._remove_transition()
            self._remove_install()
            self._remove_activity()
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
