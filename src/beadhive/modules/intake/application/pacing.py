"""``WritePacer``: one command's pacer for the outbox's GitHub write requests (``bh-eqtxv``).

intake-outbox-paced-send-design.md, "The pacer and the write ledger". The pacer sits at two seams:

* :meth:`WritePacer.admit` is the sender's hook right before an operation's write-ahead. It
  raises :class:`~..domain.pacing.SendStopped` when the command stopped, when the operation's
  expected requests do not fit in the rolling hour (then it also stops the command with
  ``hourly_budget``: nothing later starts), or when the ledger cannot be used (``error``).
  Otherwise it waits until the spacing has passed and the minute has room, and returns. It only
  PEEKS at the ledger: it records nothing. Its wait is on the command's stop, so a stop ends it
  at once (nothing durable was written yet).
* :meth:`WritePacer.gate` is the adapter's write gate (the tracker port's ``WriteGate``), called
  before every attempt of a write. EVERY request, the operation's first included, records its
  own ledger entry here, as it starts, waiting for the spacing or the minute if it must, so the
  ledger's entries are the request starts themselves (the time between the hook and the first
  request, a write-ahead or a definitions read, can never put a 81st start in a minute). It is
  never refused (the operation is past its write-ahead): over the hour it is recorded as an
  overdraft and the command stops with ``hourly_budget`` so no further operation starts; a
  ledger that cannot be written lets the request go and stops the command with ``error``.
* :meth:`WritePacer.observe` is the adapter's wait observer: a ``RateLimited`` wait stops the
  command with ``rate_limited`` at the first such wait (``retry-after: 0`` included); any wait
  moves the next slot past it.

Spacing inside the command is on the injected MONOTONIC clock (``next = last + 60 / per_minute``),
so a wall-clock step never changes it; the ledger's entries are on the injected WALL clock (they
compare across processes). ``wait(seconds, stop)`` is the injected wait: it returns after
``seconds``, or earlier when ``stop`` is given and set (``None``: a wait no stop cuts short).
This layer never sleeps, reads a clock or installs a signal handler itself.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from beadhive.modules.tracker.contracts import RateLimited, TrackerError

from ..contracts import WriteLedger
from ..domain import (
    LEDGER_MINUTE,
    MAX_WRITES_PER_HOUR,
    MAX_WRITES_PER_MINUTE,
    HourFull,
    IntakeStoreError,
    MinuteFull,
    PacingSummary,
    SendStop,
    SendStopped,
    SlotGranted,
    StopReason,
)

__all__ = ["WritePacer", "check_pace"]

#: The shortest wait for a full minute whose oldest entry is (by the wall clock) already out of
#: it: never spin on the ledger.
_MIN_RETRY = 0.05


def check_pace(per_minute: object, per_hour: object) -> tuple[int, int]:
    """``(per_minute, per_hour)`` when both are whole numbers within GitHub's documented limits,
    else ``ValueError`` naming the bound (a safety limit: refused, never clamped)."""
    for name, value, bound in (
        ("send_writes_per_minute", per_minute, MAX_WRITES_PER_MINUTE),
        ("send_writes_per_hour", per_hour, MAX_WRITES_PER_HOUR),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= bound:
            raise ValueError(f"{name} must be a whole number from 1 to {bound}")
    assert isinstance(per_minute, int) and isinstance(per_hour, int)
    return per_minute, per_hour


def _clock_time(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class WritePacer:
    """The :class:`~..contracts.SendPacer` of one command over the host's shared
    :class:`~..contracts.WriteLedger` (see the module docstring)."""

    def __init__(
        self,
        ledger: WriteLedger,
        *,
        per_minute: int,
        per_hour: int,
        monotonic: Callable[[], float],
        wall: Callable[[], float],
        wait: Callable[[float, SendStop | None], None],
    ) -> None:
        self._per_minute, self._per_hour = check_pace(per_minute, per_hour)
        self._ledger = ledger
        self._monotonic = monotonic
        self._wall = wall
        self._wait = wait
        self._spacing = LEDGER_MINUTE / self._per_minute
        self._stop = SendStop()
        self._next: float | None = None  # the monotonic time the next request may start
        self._first = False  # the admitted operation's first request is still to come
        self._requests = 0
        self._waited = 0.0
        self._frees_at: float | None = None

    # -- the sender's side ---------------------------------------------------------------------

    @property
    def stop(self) -> SendStop:
        return self._stop

    def begin(self, stop: SendStop) -> None:
        """Bind the command's one stop (the pacer waits on it and sets it)."""
        self._stop = stop

    def admit(self, requests: int) -> None:
        """Before an operation's write-ahead: stop, or wait until ``requests`` fit in the hour and
        the minute has room (see the module docstring). Records nothing."""
        need = max(1, int(requests))
        while True:
            self._check()
            delay = self._spacing_delay()
            if delay > 0:
                self._pause(delay, interruptible=True)
                continue
            now = self._wall()
            try:
                answer = self._ledger.take(
                    now,
                    per_minute=self._per_minute,
                    per_hour=self._per_hour,
                    need=need,
                    record=False,
                )
            except IntakeStoreError as exc:
                detail = self._ledger_failed(exc)
                raise SendStopped(StopReason.ERROR, detail) from exc
            if isinstance(answer, SlotGranted):
                self._next = self._monotonic() + self._spacing  # the next admission's spacing
                self._first = True
                return
            if isinstance(answer, HourFull):
                self._frees_at = answer.until
                detail = (
                    f"{need} write request(s) do not fit in {self._per_hour} an hour on this "
                    f"host; enough of the hour frees at {_clock_time(answer.until)}: run "
                    "send-approved again then (the operations not started stay approved)"
                )
                self._stop.set(StopReason.HOURLY_BUDGET, detail)
                raise SendStopped(StopReason.HOURLY_BUDGET, detail)
            assert isinstance(answer, MinuteFull)
            self._pause(max(answer.until - now, _MIN_RETRY), interruptible=True)

    def summary(self) -> PacingSummary:
        return PacingSummary(
            self._per_minute,
            self._per_hour,
            requests=self._requests,
            waited_seconds=round(self._waited, 3),
            budget_frees_at=self._frees_at,
        )

    # -- the adapter's side --------------------------------------------------------------------

    def gate(self, method: str) -> None:
        """Before every attempt of a write (the tracker port's ``WriteGate``): records the
        request's start in the ledger; never refuses. The admitted operation's first request
        was spaced at the hook, so only the minute can hold it here; every further one is spaced
        again."""
        first, self._first = self._first, False
        while True:
            delay = 0.0 if first else self._spacing_delay()
            if delay > 0:
                self._pause(delay, interruptible=False)  # mid-operation: a stop drains it
                continue
            now = self._wall()
            try:
                answer = self._ledger.take(
                    now,
                    per_minute=self._per_minute,
                    per_hour=self._per_hour,
                    overdraw=True,
                )
            except IntakeStoreError as exc:
                # the request still goes (never refused mid-write); no further operation starts
                self._ledger_failed(exc)
                self._took(first)
                return
            if isinstance(answer, MinuteFull):
                self._pause(max(answer.until - now, _MIN_RETRY), interruptible=False)
                continue
            self._took(first)
            if isinstance(answer, SlotGranted) and answer.overdraft:
                self._stop.set(
                    StopReason.HOURLY_BUDGET,
                    f"a request inside an operation went over {self._per_hour} write requests "
                    "an hour on this host (recorded); no further operation starts",
                )
            return

    def observe(self, error: TrackerError, seconds: float) -> None:
        """A retry wait the adapter is about to sleep (the tracker port's ``WaitObserver``)."""
        now = self._monotonic()
        later = now + max(0.0, seconds)
        self._next = later if self._next is None else max(self._next, later)
        if isinstance(error, RateLimited):
            self._stop.set(
                StopReason.RATE_LIMITED,
                f"GitHub answered a rate limit ({error.kind}) and asked for {seconds:g} s; the "
                "request in flight finishes its retries and no further operation starts",
            )

    # -- internals -----------------------------------------------------------------------------

    def _check(self) -> None:
        stop = self._stop
        if stop.is_set():
            reason = stop.reason or StopReason.INTERRUPTED
            raise SendStopped(reason, stop.detail)

    def _ledger_failed(self, exc: IntakeStoreError) -> str:
        detail = f"the outbox write ledger cannot be used: {exc.detail}"
        self._stop.set(StopReason.ERROR, detail)
        return detail

    def _spacing_delay(self) -> float:
        return 0.0 if self._next is None else self._next - self._monotonic()

    def _took(self, first: bool = False) -> None:
        """A request started. The admitted operation's first keeps the spacing its admission
        set (so the lag after the hook never slows the pace); every other one spaces the next."""
        self._requests += 1
        if not first:
            self._next = self._monotonic() + self._spacing

    def _pause(self, seconds: float, *, interruptible: bool) -> None:
        before = self._monotonic()
        self._wait(seconds, self._stop if interruptible else None)
        self._waited += max(0.0, self._monotonic() - before)
