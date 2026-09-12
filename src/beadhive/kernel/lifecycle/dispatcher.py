"""Deterministic delivery for explicitly bound lifecycle subscribers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, TypeVar, cast

from beadhive.kernel.telemetry.contracts import (
    AttributeKey,
    ErrorClassification,
    EventError,
    Outcome,
    SemanticEventName,
    SemanticTelemetryPort,
    TelemetryAttribute,
    TelemetryObservation,
    activate_non_fatal,
)

from .contracts import (
    EVENTS_BY_ID,
    CompensationMode,
    Criticality,
    LifecycleEvent,
    SubscriberBinding,
)

ContextT = TypeVar("ContextT")


def _canonical_event(event: LifecycleEvent[ContextT]) -> LifecycleEvent[ContextT]:
    canonical = EVENTS_BY_ID.get(event.id)
    if canonical is None or (
        event.family is not canonical.family
        or event.phase is not canonical.phase
        or event.order != canonical.order
        or event.context_type is not canonical.context_type
    ):
        raise ValueError(f"{event.id} is not a kernel-owned lifecycle event contract")
    return cast(LifecycleEvent[ContextT], canonical)


class DeliveryStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed-out"


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    number: int
    status: DeliveryStatus
    duration_seconds: float
    error: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    plugin_id: str
    subscription_id: str
    event_id: str
    criticality: Criticality
    attempts: tuple[DeliveryAttempt, ...]

    @property
    def status(self) -> DeliveryStatus:
        return self.attempts[-1].status


@dataclass(frozen=True, slots=True)
class CompensationResult:
    plugin_id: str
    subscription_id: str
    action_id: str
    attempts: tuple[DeliveryAttempt, ...]

    @property
    def status(self) -> DeliveryStatus:
        return self.attempts[-1].status


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    event_id: str
    deliveries: tuple[DeliveryResult, ...]
    compensations: tuple[CompensationResult, ...] = ()


class LifecycleDeliveryError(RuntimeError):
    """A blocking subscriber failed after completed work was compensated."""

    def __init__(self, report: DeliveryReport) -> None:
        self.report = report
        failure = report.deliveries[-1]
        super().__init__(
            f"blocking lifecycle subscriber {failure.subscription_id!r} "
            f"{failure.status.value} for {report.event_id}"
        )


class LifecycleDispatcher:
    """An immutable coordinator over explicit bindings, not a process-global event bus."""

    def __init__(
        self,
        bindings: tuple[SubscriberBinding[Any], ...] = (),
        *,
        telemetry: SemanticTelemetryPort | None = None,
    ) -> None:
        for binding in bindings:
            _canonical_event(binding.event)
        identities = [(binding.plugin_id, binding.subscription_id) for binding in bindings]
        if len(identities) != len(set(identities)):
            raise ValueError("lifecycle subscriber identities must be unique")
        self._bindings = tuple(
            sorted(
                bindings,
                key=lambda binding: (
                    binding.event.id,
                    binding.policy.order,
                    binding.plugin_id,
                    binding.subscription_id,
                ),
            )
        )
        self._telemetry = telemetry

    def bindings_for(
        self, event: LifecycleEvent[ContextT]
    ) -> tuple[SubscriberBinding[ContextT], ...]:
        canonical = _canonical_event(event)
        return tuple(binding for binding in self._bindings if binding.event == canonical)

    async def dispatch(self, event: LifecycleEvent[ContextT], context: ContextT) -> DeliveryReport:
        canonical = _canonical_event(event)
        if not isinstance(context, canonical.context_type):
            raise TypeError(
                f"{canonical.id} requires {canonical.context_type.__name__}, "
                f"got {type(context).__name__}"
            )
        deliveries: list[DeliveryResult] = []
        completed: list[SubscriberBinding[ContextT]] = []
        for binding in self.bindings_for(canonical):
            result = await self._invoke(canonical, binding, context, compensation=False)
            deliveries.append(
                DeliveryResult(
                    plugin_id=binding.plugin_id,
                    subscription_id=binding.subscription_id,
                    event_id=canonical.id,
                    criticality=binding.policy.criticality,
                    attempts=result,
                )
            )
            if result[-1].status is DeliveryStatus.SUCCEEDED:
                if binding.policy.compensation.mode is CompensationMode.REQUIRED:
                    completed.append(binding)
                continue
            if binding.policy.criticality is Criticality.BEST_EFFORT:
                continue
            compensations = await self._compensate(canonical, reversed(completed), context)
            raise LifecycleDeliveryError(
                DeliveryReport(canonical.id, tuple(deliveries), compensations)
            )
        return DeliveryReport(canonical.id, tuple(deliveries))

    async def _compensate(
        self,
        event: LifecycleEvent[ContextT],
        bindings: Iterable[SubscriberBinding[ContextT]],
        context: ContextT,
    ) -> tuple[CompensationResult, ...]:
        results: list[CompensationResult] = []
        for binding in bindings:
            attempts = await self._invoke(event, binding, context, compensation=True)
            action_id = binding.policy.compensation.action_id
            assert action_id is not None
            results.append(
                CompensationResult(
                    plugin_id=binding.plugin_id,
                    subscription_id=binding.subscription_id,
                    action_id=action_id,
                    attempts=attempts,
                )
            )
        return tuple(results)

    async def _invoke(
        self,
        event: LifecycleEvent[ContextT],
        binding: SubscriberBinding[ContextT],
        context: ContextT,
        *,
        compensation: bool,
    ) -> tuple[DeliveryAttempt, ...]:
        callback = binding.compensator if compensation else binding.subscriber
        assert callback is not None
        attempts: list[DeliveryAttempt] = []
        for number in range(1, binding.policy.retry.max_attempts + 1):
            started = time.monotonic()
            observation = self._begin_delivery(event, binding, context, number=number)
            try:
                with activate_non_fatal(self._telemetry, observation):
                    async with asyncio.timeout(binding.policy.timeout_seconds):
                        await callback(context)
            except asyncio.CancelledError:
                self._complete_delivery(
                    observation,
                    Outcome.CANCELLED,
                    EventError(ErrorClassification.CANCELLATION, "lifecycle.cancelled"),
                )
                raise
            except TimeoutError as exc:
                self._complete_delivery(
                    observation,
                    Outcome.TIMED_OUT,
                    EventError(ErrorClassification.TIMEOUT, "lifecycle.timeout", True),
                )
                attempts.append(
                    DeliveryAttempt(
                        number,
                        DeliveryStatus.TIMED_OUT,
                        time.monotonic() - started,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            except Exception as exc:
                self._complete_delivery(
                    observation,
                    Outcome.FAILED,
                    EventError(ErrorClassification.INTERNAL, "lifecycle.failed"),
                )
                attempts.append(
                    DeliveryAttempt(
                        number,
                        DeliveryStatus.FAILED,
                        time.monotonic() - started,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                self._complete_delivery(observation, Outcome.SUCCEEDED)
                attempts.append(
                    DeliveryAttempt(
                        number,
                        DeliveryStatus.SUCCEEDED,
                        time.monotonic() - started,
                    )
                )
                break
            if number < binding.policy.retry.max_attempts:
                await asyncio.sleep(binding.policy.retry.backoff_seconds)
        return tuple(attempts)

    def _begin_delivery(
        self,
        event: LifecycleEvent[ContextT],
        binding: SubscriberBinding[ContextT],
        context: ContextT,
        *,
        number: int,
    ) -> TelemetryObservation | None:
        if self._telemetry is None:
            return None
        try:
            identity = replace(
                self._telemetry.identity,
                plugin_id=binding.plugin_id,
                plugin_version=None,
            )
            attributes = (
                TelemetryAttribute(AttributeKey.LIFECYCLE_EVENT, event.id),
                TelemetryAttribute(AttributeKey.OPERATION_KIND, "lifecycle"),
                TelemetryAttribute(AttributeKey.RETRY_COUNT, number - 1),
                TelemetryAttribute(AttributeKey.SURFACE, "internal"),
            )
            return self._telemetry.begin(
                SemanticEventName.LIFECYCLE_DELIVERY,
                correlation_id=context.correlation_id,
                identity=identity,
                attributes=attributes,
            )
        except BaseException:
            return None

    def _complete_delivery(
        self,
        observation: TelemetryObservation | None,
        outcome: Outcome,
        error: EventError | None = None,
    ) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.complete(observation, outcome, error=error)
        except BaseException:
            return
