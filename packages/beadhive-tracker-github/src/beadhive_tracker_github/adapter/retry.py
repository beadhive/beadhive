"""The adapter-owned retry layer: a pure classification and a pure decision.

tracker-adapter-port-adr.md section 1 ("The adapter owns the retry decision and the sleep seam"):
githubkit's own retry sleeps through a module attribute (``time.sleep``) and its primary
rate-limit path reads the REAL clock and sleeps 60 s on a zero delay. So githubkit's retry is
switched off (``auto_retry=False``: its sleep is clamped to never) and this module decides
instead, from the response alone plus an injected clock:

* :func:`classify` turns one failed attempt into a :class:`Refusal`: the port error to raise if
  no retry happens, whether a retry may happen at all, and the wait GitHub asked for.
* :func:`next_wait` applies the :class:`~beadhive.modules.tracker.contracts.RetryBudget`: the
  seconds to sleep before the next attempt, or ``None`` to raise. A wait longer than the budget
  allows is surfaced as the error (``RateLimited.retry_after`` says how long), never slept.

What is retried (port ADR section 3, "Rate-limit semantics"):

* a **rate limit**, for reads and writes alike, because a rate-limit answer proves nothing was
  done. ``Retry-After`` is a secondary limit (``throttle``); ``X-RateLimit-Remaining: 0`` waits
  until ``X-RateLimit-Reset`` on the injected clock (``quota``, or ``throttle`` when GitHub's
  message names a secondary limit), at least :data:`MIN_RESET_WAIT` so a reset already in the
  past (clock skew) cannot spin; neither header means GitHub's documented "wait one minute";
* a **server error or a dropped connection on a read** (``TransientFailure``), with GitHub's
  ``Retry-After`` when given, else an exponential backoff of 1, 2, 4 ... seconds capped at the
  budget's single wait.

What is never retried: any other refusal, and a write answered ``5xx`` or cut off in transit,
which is ``WriteOutcomeUnknown`` (the comment may exist; the consumer reconciles by marker).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from beadhive.modules.tracker.contracts import (
    Operation,
    RateLimited,
    RetryBudget,
    TrackerError,
    TransientFailure,
    WriteOutcomeUnknown,
)

from .diagnostics import clean, diagnose, is_rate_limited, refusal_error

__all__ = [
    "DEFAULT_LIMIT_WAIT",
    "MIN_RESET_WAIT",
    "Refusal",
    "backoff",
    "classify",
    "classify_transport_failure",
    "next_wait",
    "rate_limit_wait",
]

#: GitHub's documented wait when a rate limit names no time ("wait at least one minute").
DEFAULT_LIMIT_WAIT = 60.0

#: The shortest wait for a primary limit whose reset is already past on our clock.
MIN_RESET_WAIT = 1.0


@dataclass(frozen=True)
class Refusal:
    """One failed attempt. ``wait`` is what the tracker asked for; ``None`` with ``retryable``
    means "back off by attempt count"."""

    error: TrackerError
    retryable: bool
    wait: float | None = None


def _seconds(value: str, now: datetime) -> float | None:
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:  # the HTTP-date form of Retry-After
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((moment - now).total_seconds(), 0.0)


def rate_limit_wait(
    headers: Mapping[str, str], message: str, *, now: datetime
) -> tuple[float, str]:
    """``(seconds, kind)`` for a rate-limit answer, computed on the injected clock ``now``."""
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        seconds = _seconds(retry_after, now)
        return (DEFAULT_LIMIT_WAIT if seconds is None else seconds), "throttle"
    if headers.get("x-ratelimit-remaining") != "0":
        return DEFAULT_LIMIT_WAIT, "throttle"  # a bare 429: a burst limit with no stated time
    kind = "throttle" if "secondary rate" in message.lower() else "quota"
    try:
        moment = datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), tz=UTC)
    except (KeyError, ValueError, OverflowError, OSError):
        return DEFAULT_LIMIT_WAIT, kind
    return max((moment - now).total_seconds(), MIN_RESET_WAIT), kind


def classify(
    status: int,
    headers: Mapping[str, str],
    message: str,
    *,
    write: bool,
    now: datetime,
    source: str | None,
    operation: Operation | None,
    secrets: Sequence[str] = (),
) -> Refusal:
    """The refusal for one error response (``status >= 400``)."""
    line = diagnose(status, headers, message, source=source, secrets=secrets)
    if is_rate_limited(status, headers):
        seconds, kind = rate_limit_wait(headers, message, now=now)
        error = RateLimited(line, retry_after=timedelta(seconds=seconds), kind=kind)
        return Refusal(error, retryable=True, wait=seconds)
    if status >= 500:
        if write:
            return Refusal(
                WriteOutcomeUnknown(f"GitHub answered {status} to a write; it may have applied"),
                retryable=False,
            )
        retry_after = headers.get("retry-after")
        wait = _seconds(retry_after, now) if retry_after is not None else None
        detail = clean(message, secrets)
        text = f"GitHub answered {status}" + (f": {detail}" if detail else "")
        return Refusal(TransientFailure(text), retryable=True, wait=wait)
    return Refusal(
        refusal_error(
            status, headers, message, source=source, operation=operation, secrets=secrets
        ),
        retryable=False,
    )


def classify_transport_failure(*, write: bool, timeout: bool) -> Refusal:
    """A request that got no response (connection refused or reset, a timeout)."""
    what = "timed out" if timeout else "did not complete"
    if write:
        return Refusal(
            WriteOutcomeUnknown(f"the write {what}; it may have applied"), retryable=False
        )
    return Refusal(TransientFailure(f"the GitHub request {what}"), retryable=True)


def backoff(attempt: int, budget: RetryBudget) -> float:
    """1, 2, 4 ... seconds by attempt, capped at the budget's single wait."""
    return min(2.0 ** min(attempt, 30), budget.max_single_wait.total_seconds())


def next_wait(
    refusal: Refusal, *, attempt: int, waited: float, budget: RetryBudget
) -> float | None:
    """Seconds to sleep before retry number ``attempt + 1``, or ``None`` to raise the error.

    ``attempt`` is the number of retries already made and ``waited`` the seconds already slept
    for this request."""
    if not refusal.retryable or attempt >= budget.max_retries:
        return None
    wait = refusal.wait if refusal.wait is not None else backoff(attempt, budget)
    if wait > budget.max_single_wait.total_seconds():
        return None
    if waited + wait > budget.max_total_wait.total_seconds():
        return None
    return wait
