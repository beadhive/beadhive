"""``InProcessIntakeLock``, ``MemoryCheckpointStore`` and ``MemoryOutboxView``: deterministic
stand-ins for intake's lock, checkpoint and outbox read ports (external-intake-module-adr.md
sections 4 and 9).

The lock never waits: a second hold of a held binding is refused at once with
``IntakeRefused(INTAKE_BUSY)``, so a test can prove an operation refused without any timing. The
epoch is a plain settable integer (the root's is ``guard.live_epoch``). No thread, no clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from beadhive.modules.intake.contracts import (
    Checkpoint,
    IntakeRefused,
    RefusalReason,
    ThreadActor,
)


class InProcessIntakeLock:
    """An :class:`~beadhive.modules.intake.contracts.IntakeLock` for one process and one test."""

    def __init__(self, epoch: int = 1) -> None:
        self.epoch = epoch
        self.holds: list[str] = []  # every binding key ever held, in order
        self._held: set[str] = set()

    @contextmanager
    def hold(self, binding_key: str) -> Iterator[int]:
        if binding_key in self._held:
            raise IntakeRefused(
                RefusalReason.INTAKE_BUSY, f"{binding_key} is held by another intake operation"
            )
        self._held.add(binding_key)
        self.holds.append(binding_key)
        try:
            yield self.epoch
        finally:
            self._held.discard(binding_key)

    def held(self, binding_key: str) -> bool:
        return binding_key in self._held


class MemoryCheckpointStore:
    """A :class:`~beadhive.modules.intake.contracts.CheckpointStore` in a dict, recording every
    ``put`` (key and token, in order) so a test can assert when a mark moved."""

    def __init__(self) -> None:
        self.marks: dict[str, Checkpoint] = {}
        self.puts: list[tuple[str, str]] = []

    def get(self, binding_key: str) -> Checkpoint | None:
        return self.marks.get(binding_key)

    def put(self, binding_key: str, checkpoint: Checkpoint) -> None:
        self.puts.append((binding_key, checkpoint.token))
        self.marks[binding_key] = checkpoint


@dataclass
class MemoryOutboxView:
    """An :class:`~beadhive.modules.intake.contracts.OutboxView` over plain sets a test fills."""

    receipts: set[str] = field(default_factory=set)
    pending_ids: set[str] = field(default_factory=set)
    issued_ids: set[str] = field(default_factory=set)
    actors: set[ThreadActor] = field(default_factory=set)

    def receipt_ids(self) -> frozenset[str]:
        return frozenset(self.receipts)

    def pending(self, outbox_id: str) -> bool:
        return outbox_id in self.pending_ids

    def issued(self, outbox_id: str) -> bool:
        return outbox_id in self.issued_ids

    def known_actors(self) -> frozenset[ThreadActor]:
        return frozenset(self.actors)


__all__ = ["InProcessIntakeLock", "MemoryOutboxView"]
