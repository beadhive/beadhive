"""The paced outbox write path's records and its one pure counting rule (``bh-eqtxv``).

intake-outbox-paced-send-design.md: every outbox write REQUEST takes a slot in a host-level write
ledger shared by every command and hive on the host, at most ``per_minute`` starts in any minute
and ``per_hour`` in any rolling hour. This module holds what does not depend on where the ledger
lives:

* :class:`StopReason` and :class:`SendStop`, the command's ONE stop (the first stop wins), set by
  the pacer (``rate_limited``, ``hourly_budget``), the sender (``not_primary``, ``error``) or the
  composition root's interrupt handler (``interrupted``);
* :class:`SendStopped`, raised by the hook before a write-ahead when the command stopped or the
  hour cannot fit the operation;
* :class:`PacingSummary`, the batch outcome's ``pacing`` block;
* :func:`ledger_step`, the one read-prune-count-append rule every ledger implementation applies
  under its own lock, and its three answers :class:`SlotGranted`, :class:`MinuteFull` and
  :class:`HourFull`.

Ledger entries are wall-clock seconds (they compare across processes). An entry dated in the
future (the wall clock stepped back) is KEPT and counted until real time passes it by the window,
which errs towards fewer writes. Standard library only.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "LEDGER_HOUR",
    "LEDGER_MINUTE",
    "MAX_WRITES_PER_HOUR",
    "MAX_WRITES_PER_MINUTE",
    "HourFull",
    "LedgerAnswer",
    "MinuteFull",
    "PacingSummary",
    "SendStop",
    "SendStopped",
    "SlotGranted",
    "StopReason",
    "ledger_step",
]

#: GitHub's documented general content-creation limits: the hard upper bounds of
#: ``intake.external.outbox.send_writes_per_minute`` / ``send_writes_per_hour``, and their
#: defaults. They move only by a code change after a measured decision.
MAX_WRITES_PER_MINUTE = 80
MAX_WRITES_PER_HOUR = 500

#: The two windows, in seconds.
LEDGER_MINUTE = 60.0
LEDGER_HOUR = 3600.0


class StopReason(StrEnum):
    """Why a batch stopped (the batch JSON's ``stopped``); the first stop wins."""

    NOT_PRIMARY = "not_primary"  # the host check refused: nothing else could be sent either
    RATE_LIMITED = "rate_limited"  # GitHub answered a rate limit (first wait, or an O11)
    HOURLY_BUDGET = "hourly_budget"  # the next operation does not fit, or a request overdrew it
    INTERRUPTED = "interrupted"  # the first SIGINT: the operation in flight was drained
    ERROR = "error"  # an unexpected exception from one operation


class SendStop:
    """The command's one stop event and reason (the first :meth:`set` wins).

    Shared by the pacer (which waits on it, so a stop ends a pacer wait at once), the sender
    (which checks it before every write-ahead) and the composition root's ``SIGINT`` handler.
    Never a signal handler itself: this layer installs none."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: StopReason | None = None
        self.detail = ""

    def set(self, reason: StopReason, detail: str = "") -> bool:
        """Stop with ``reason`` unless already stopped; ``True`` when this call stopped it."""
        if self.reason is not None:
            return False
        self.reason, self.detail = reason, detail
        self._event.set()
        return True

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for a stop; ``True`` when stopped (the composition root's real
        wait; tests inject their own)."""
        return self._event.wait(max(0.0, seconds))


class SendStopped(Exception):
    """Raised by the hook before a write-ahead: the command stopped (``reason``), so this
    operation is not started and stays ``approved`` with its grant."""

    def __init__(self, reason: StopReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


@dataclass(frozen=True)
class PacingSummary:
    """The batch JSON's ``pacing`` block: the limits applied, the write requests this command
    started (each a ledger slot) and the seconds it waited for them. ``budget_frees_at`` is the
    wall-clock time (epoch seconds) at which enough of the hour frees for the operation the
    hourly budget stopped on (``None`` unless it did)."""

    writes_per_minute: int
    writes_per_hour: int
    requests: int = 0
    waited_seconds: float = 0.0
    budget_frees_at: float | None = None


@dataclass(frozen=True)
class SlotGranted:
    """The slot was appended. ``overdraft``: the hour was already spent (a mid-operation request,
    never refused, recorded so the following admissions pay for it)."""

    overdraft: bool = False


@dataclass(frozen=True)
class MinuteFull:
    """The minute window holds ``per_minute`` entries: retry at ``until`` (wall clock)."""

    until: float


@dataclass(frozen=True)
class HourFull:
    """The rolling hour cannot fit the requests asked for: enough entries leave it at ``until``
    (wall clock)."""

    until: float


LedgerAnswer = SlotGranted | MinuteFull | HourFull


def ledger_step(
    entries: Iterable[float],
    now: float,
    *,
    per_minute: int,
    per_hour: int,
    need: int = 1,
    overdraw: bool = False,
    record: bool = True,
) -> tuple[LedgerAnswer, tuple[float, ...]]:
    """One slot decision over ``entries`` at wall time ``now``: ``(answer, entries to keep)``.

    The entries kept are those newer than the hour (``t > now - 3600``; a future entry is kept),
    sorted, plus ``now`` when the slot was granted and ``record`` is set. ``need`` is how many
    requests the operation expects: the hour must fit all of them (``count + need <= per_hour``),
    else :class:`HourFull`, unless ``overdraw`` (a request already inside an operation: recorded
    even over the hour, as an overdraft). The minute must hold fewer than ``per_minute`` entries,
    else :class:`MinuteFull` with the moment its oldest entry that blocks leaves the window.

    ``record=False`` only PEEKS (the hook before a write-ahead: is there room?) and appends
    nothing; an entry is recorded only when a request actually starts, so the entries are the
    request starts themselves. At most ONE entry is appended per call."""
    kept = tuple(sorted(t for t in entries if t > now - LEDGER_HOUR))
    count = len(kept)
    if not overdraw and count + need > per_hour:
        excess = count + need - per_hour
        until = kept[excess - 1] + LEDGER_HOUR if 0 < excess <= count else now + LEDGER_HOUR
        if excess > count and kept:
            until = kept[-1] + LEDGER_HOUR  # the operation needs more than the whole hour holds
        return HourFull(until), kept
    minute = [t for t in kept if t > now - LEDGER_MINUTE]
    if len(minute) >= per_minute:
        return MinuteFull(minute[len(minute) - per_minute] + LEDGER_MINUTE), kept
    if not record:
        return SlotGranted(), kept
    return SlotGranted(overdraft=count + 1 > per_hour), tuple(sorted((*kept, now)))
