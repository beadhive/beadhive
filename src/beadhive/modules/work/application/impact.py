"""Compatibility import path for the public impact-resolution policy."""

from __future__ import annotations

from ..contracts.impact_resolution import (
    DEFAULT_TIMEOUT_SECONDS,
    FailClosedResolver,
    NativeFullResolver,
    select_resolver,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FailClosedResolver",
    "NativeFullResolver",
    "select_resolver",
]
