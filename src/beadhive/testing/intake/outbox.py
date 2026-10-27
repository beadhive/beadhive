"""``SequentialIdSource`` and ``RecordingSanitizer``: deterministic stand-ins for the outbox's
id and sanitizer ports (``bh-6wj9y`` R14).

The id source mints well-formed opaque ids (``ob1_``/``gr1_`` plus 26 base32 characters) from a
counter instead of random bytes, so a scenario's ids are the same on every run. The sanitizer
flags configured secret strings and records every text it was asked to scan, so a test can
prove the exact bytes were checked. No clock, no randomness, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from beadhive.modules.intake.contracts import SanitizerFinding, mint_grant_id, mint_outbox_id


@dataclass
class SequentialIdSource:
    """An :class:`~beadhive.modules.intake.contracts.IdSource` counting from ``start``."""

    start: int = 1
    _outbox: int = field(default=0, init=False)
    _grant: int = field(default=0, init=False)

    def outbox_id(self) -> str:
        self._outbox += 1
        return mint_outbox_id((self.start + self._outbox - 1).to_bytes(16, "big"))

    def grant_id(self) -> str:
        self._grant += 1
        return mint_grant_id((self.start + self._grant - 1).to_bytes(16, "big"))


@dataclass
class RecordingSanitizer:
    """A :class:`~beadhive.modules.intake.contracts.Sanitizer` over a fixed set of secrets."""

    secrets: tuple[str, ...] = ()
    scanned: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, secrets: Iterable[str]) -> RecordingSanitizer:
        return cls(tuple(s for s in secrets if s))

    def scan(self, text: str) -> tuple[SanitizerFinding, ...]:
        self.scanned.append(text)
        hits: list[SanitizerFinding] = []
        for secret in self.secrets:
            start = text.find(secret)
            while start >= 0:
                hits.append(SanitizerFinding("secret", start, start + len(secret)))
                start = text.find(secret, start + 1)
        return tuple(sorted(hits, key=lambda h: h.start))

    def scrub(self, text: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text


__all__ = ["RecordingSanitizer", "SequentialIdSource"]
