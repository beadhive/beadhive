"""Framework-neutral semantic outcome collection for Herdr application services."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import NoReturn

from .application_contracts import ApplicationError, ApplicationNotice


@dataclass(slots=True)
class OutcomeCollector:
    notices: list[ApplicationNotice] = field(default_factory=list)
    payload: Mapping[str, object] | None = None


class StopApplication(Exception):
    """Internal control transfer carrying a stable semantic application error."""

    def __init__(self, error: ApplicationError) -> None:
        super().__init__(error.detail)
        self.error = error


class Stop(StopApplication):
    """Framework-neutral early completion used by legacy helper seams."""

    def __init__(self, exit_code: int = 0) -> None:
        collector = _COLLECTOR.get()
        detail = collector.notices[-1].detail if collector and collector.notices else ""
        super().__init__(ApplicationError("application_stopped", detail, exit_code))


class InvalidInput(StopApplication):
    """Framework-neutral invalid-input refusal."""

    def __init__(self, detail: str, *, param_hint: str | None = None) -> None:
        super().__init__(ApplicationError("invalid_parameter", detail, 2, param_hint))


_COLLECTOR: ContextVar[OutcomeCollector | None] = ContextVar(
    "beadhive_herdr_application_outcome",
    default=None,
)


@contextmanager
def collect() -> Iterator[OutcomeCollector]:
    collector = OutcomeCollector()
    token = _COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _COLLECTOR.reset(token)


def _active() -> OutcomeCollector:
    collector = _COLLECTOR.get()
    if collector is None:
        collector = OutcomeCollector()
        _COLLECTOR.set(collector)
    return collector


def notice(detail: object = "", *, nl: bool = True, err: bool = False) -> None:
    """Record a semantic notice without writing to a terminal or process stream."""

    _active().notices.append(
        ApplicationNotice(str(detail), severity="error" if err else "info", newline=nl)
    )


def emit_payload(payload: Mapping[str, object]) -> None:
    """Record a structured semantic payload for the outer presentation adapter."""

    collector = _active()
    if collector.payload is not None:
        raise RuntimeError("Herdr application produced more than one semantic payload")
    collector.payload = dict(payload)


def stop(exit_code: int = 0, *, code: str = "application_stopped") -> NoReturn:
    raise StopApplication(ApplicationError(code, "", exit_code))


def fail(
    detail: str,
    *,
    exit_code: int = 1,
    code: str = "application_failed",
) -> NoReturn:
    raise StopApplication(ApplicationError(code, detail, exit_code))


def invalid(detail: str, *, parameter: str | None = None) -> NoReturn:
    raise StopApplication(ApplicationError("invalid_parameter", detail, 2, parameter))
