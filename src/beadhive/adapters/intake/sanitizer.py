"""``ScrubSanitizer``: the intake ``Sanitizer`` port over the shared credential scrubber.

The interim egress sanitizer (external-intake-module-adr.md section 13): until ``bh-o2mjz``
lands its own, the root binds this one, built on ``beadhive_plugins.credentials`` so the outbox
redacts exactly what every other output path redacts (exact secret values, longest first, then
the token-shape pattern). ``scan`` reports spans and kinds only, never the secret; the leak check
turns each into a never-overridable ``secret`` finding, so a payload carrying one stays held.
Pure; no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence

from beadhive.modules.intake.contracts import SanitizerFinding
from beadhive_plugins.credentials import TOKEN_PATTERN, scrub


class ScrubSanitizer:
    """Implements :class:`~beadhive.modules.intake.contracts.Sanitizer`."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def scan(self, text: str) -> tuple[SanitizerFinding, ...]:
        spans: list[tuple[int, int, str]] = []
        for secret in self._secrets:
            start = text.find(secret)
            while start >= 0:
                spans.append((start, start + len(secret), "secret"))
                start = text.find(secret, start + 1)
        spans.extend((m.start(), m.end(), "token") for m in TOKEN_PATTERN.finditer(text))
        hits: list[SanitizerFinding] = []
        for start, end, kind in sorted(spans):
            if hits and start < hits[-1].end:  # inside a longer hit already reported
                continue
            hits.append(SanitizerFinding(kind, start, end))
        return tuple(hits)

    def scrub(self, text: str) -> str:
        return scrub(text, self._secrets)


__all__ = ["ScrubSanitizer"]
