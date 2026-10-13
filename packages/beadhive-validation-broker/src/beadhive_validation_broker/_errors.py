"""Broker error types (validation-broker-adr, "Requirements for the broker").

Messages never carry a secret. None of these is a verdict: a failed ``report`` or an unavailable
broker never changes whether a key's result counts.
"""

from __future__ import annotations

__all__ = [
    "BrokerError",
    "BrokerUnavailable",
    "GrantRefused",
    "GrantTimeout",
    "LeaseLost",
]


class BrokerError(Exception):
    """Base class for broker failures."""


class BrokerUnavailable(BrokerError):
    """Backend down or unreachable. The client falls back to host slots, loudly; never a red
    verdict."""


class GrantRefused(BrokerError):
    """The reservation can never fit: it exceeds the budget, or exceeds its parent's grant. The
    broker refuses rather than hold and wait."""


class LeaseLost(BrokerError):
    """The lease is gone: cancelled, reclaimed as an orphan, parent released, or lost."""


class GrantTimeout(BrokerError):
    """``wait_grant`` timed out; the job stays queued unless the caller cancels it."""
