"""Common semantic execution seam for canonical application operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ParamSpec, Protocol, TypeVar

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

from ._registry import canonical_operation_name

P = ParamSpec("P")
ResultT = TypeVar("ResultT")


class OperationContract(Protocol):
    name: str
    kind: str


_OPERATION_KINDS = {
    "action": "command",
    "read-resource": "resource",
}


class OperationExecutor:
    """Run sync or async handlers while semantic telemetry remains strictly observational."""

    def __init__(
        self, telemetry: SemanticTelemetryPort | None = None, *, surface: str = "internal"
    ):
        self._telemetry = telemetry
        self._surface = surface

    def _begin(self, operation: OperationContract) -> TelemetryObservation | None:
        if self._telemetry is None:
            return None
        try:
            operation_kind = operation.kind
            operation_name = operation.name
            surface = self._surface
            if type(operation_kind) is not str:
                raise TypeError("operation.kind must be a built-in string")
            bound_operation_name = canonical_operation_name(operation_name)
            attributes = [
                TelemetryAttribute(AttributeKey.OPERATION_KIND, _OPERATION_KINDS[operation_kind]),
                TelemetryAttribute(AttributeKey.SURFACE, surface),
            ]
            if bound_operation_name is not None:
                attributes.insert(
                    1, TelemetryAttribute(AttributeKey.OPERATION_NAME, bound_operation_name)
                )
            return self._telemetry.begin(
                SemanticEventName.OPERATION_EXECUTION, attributes=tuple(attributes)
            )
        except BaseException:
            return None

    def _complete(
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

    def execute(
        self,
        operation: OperationContract,
        handler: Callable[P, ResultT],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> ResultT:
        observation = self._begin(operation)
        try:
            with activate_non_fatal(self._telemetry, observation):
                result = handler(*args, **kwargs)
        except asyncio.CancelledError:
            self._complete(
                observation,
                Outcome.CANCELLED,
                EventError(ErrorClassification.CANCELLATION, "operation.cancelled"),
            )
            raise
        except TimeoutError:
            self._complete(
                observation,
                Outcome.TIMED_OUT,
                EventError(ErrorClassification.TIMEOUT, "deadline.exceeded", True),
            )
            raise
        except Exception:
            self._complete(
                observation,
                Outcome.FAILED,
                EventError(ErrorClassification.INTERNAL, "operation.failed"),
            )
            raise
        self._complete(observation, Outcome.SUCCEEDED)
        return result

    async def execute_async(
        self,
        operation: OperationContract,
        handler: Callable[P, Awaitable[ResultT]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> ResultT:
        observation = self._begin(operation)
        try:
            with activate_non_fatal(self._telemetry, observation):
                result = await handler(*args, **kwargs)
        except asyncio.CancelledError:
            self._complete(
                observation,
                Outcome.CANCELLED,
                EventError(ErrorClassification.CANCELLATION, "operation.cancelled"),
            )
            raise
        except TimeoutError:
            self._complete(
                observation,
                Outcome.TIMED_OUT,
                EventError(ErrorClassification.TIMEOUT, "deadline.exceeded", True),
            )
            raise
        except Exception:
            self._complete(
                observation,
                Outcome.FAILED,
                EventError(ErrorClassification.INTERNAL, "operation.failed"),
            )
            raise
        self._complete(observation, Outcome.SUCCEEDED)
        return result
