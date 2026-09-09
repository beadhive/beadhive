"""Narrow telemetry ports consumed by operation projection adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class TraceVerb(Protocol):
    """Decorate one operation handler while preserving its callable signature."""

    def __call__(self, operation: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...
