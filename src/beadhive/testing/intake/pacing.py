"""``MemoryWriteLedger`` and ``FakePacerClock``: the write pacer's ledger and clocks without a
file or real time (``bh-eqtxv``).

The memory ledger applies the same :func:`~beadhive.modules.intake.contracts.ledger_step` rule
the file ledger applies under its lock, so pacers that share ONE memory ledger behave like two
commands on one host. The fake clock gives the pacer a monotonic and a wall clock that move only
when the pacer waits (or a test calls :meth:`FakePacerClock.advance`), so a unit test never
sleeps: a wait advances both clocks by its seconds, unless the stop it is given is already set
(then it returns at once, as ``Event.wait`` does). No I/O, no real clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from beadhive.modules.intake.contracts import SendStop, SlotGranted, ledger_step


@dataclass
class MemoryWriteLedger:
    """A :class:`~beadhive.modules.intake.contracts.WriteLedger` over a list of wall-clock
    entries. ``taken`` counts the slots recorded and ``overdrafts`` those over the hour; a peek
    (``record=False``) counts nothing."""

    entries: list[float] = field(default_factory=list)
    taken: int = 0
    overdrafts: int = 0

    def take(
        self,
        now: float,
        *,
        per_minute: int,
        per_hour: int,
        need: int = 1,
        overdraw: bool = False,
        record: bool = True,
    ):
        answer, kept = ledger_step(
            self.entries,
            now,
            per_minute=per_minute,
            per_hour=per_hour,
            need=need,
            overdraw=overdraw,
            record=record,
        )
        self.entries = list(kept)
        if record and isinstance(answer, SlotGranted):
            self.taken += 1
            self.overdrafts += answer.overdraft
        return answer


@dataclass
class FakePacerClock:
    """The pacer's injected ``monotonic``, ``wall`` and ``wait``: both clocks advance together,
    and only by a wait or :meth:`advance`. ``waits`` records every wait asked for, and whether a
    stop could cut it short; ``on_wait`` (optional) runs inside each wait, before time moves, so a
    test can set the stop mid-wait."""

    mono: float = 1000.0
    now: float = 1_800_000_000.0
    waits: list[tuple[float, bool]] = field(default_factory=list)
    on_wait: object = None

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += seconds

    def step_wall(self, seconds: float) -> None:
        """Move the wall clock alone (an NTP step): the monotonic clock is untouched."""
        self.now += seconds

    def wait(self, seconds: float, stop: SendStop | None) -> None:
        self.waits.append((seconds, stop is not None))
        if callable(self.on_wait):
            self.on_wait(seconds)
        if stop is not None and stop.is_set():
            return
        self.advance(max(0.0, seconds))
