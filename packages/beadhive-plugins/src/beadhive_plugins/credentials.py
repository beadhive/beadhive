"""The shared credential value type, injection port and output scrubber (bh-99xst).

Stdlib only, like the rest of this library package, and nothing here touches the network, the
environment or a subprocess. It holds the three things root and a plugin package must agree on
byte for byte (``docs/design/tracker-adapter-plugin-adr.md`` O1):

* :class:`ResolvedToken` - the value type root BUILDS and a plugin RECEIVES. Redaction in
  ``repr`` and ``str`` is a property of this one class, so root cannot hand over a leaky object.
* :class:`TokenSource` - the injected port: a zero-argument callable, so a plugin cannot reach
  for the environment, the ``gh`` binary or ``beadhive.run``. It is called once per operation
  and its result is never cached, so a rotated token is picked up.
* :func:`scrub` - the one rendering of untrusted text for output, logs and errors.

A credential is never stored, logged or echoed: the value lives in memory for one operation, and
only its provenance (``env:GH_TOKEN``) may be shown.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "REDACTED",
    "TOKEN_PATTERN",
    "ResolvedToken",
    "TokenSource",
    "scrub",
]

#: What a redacted credential is replaced with.
REDACTED = "***"

#: GitHub token shapes: fine-grained PATs (``github_pat_``) and the ``gh[pousr]_`` family
#: (classic ``ghp_``, OAuth ``gho_``, user-to-server ``ghu_``, installation ``ghs_`` and refresh
#: ``ghr_``). Defence in depth for a token the exact-value pass was never told about, so it is
#: deliberately unanchored: it redacts a shape wherever it appears, even glued to other text.
TOKEN_PATTERN = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}")


@dataclass(frozen=True)
class ResolvedToken:
    """A credential plus where it came from.

    ``value`` is excluded from ``repr`` and ``str()`` prints only the provenance, so an
    accidental f-string, log field or exception message cannot leak it.
    """

    value: str = field(repr=False)
    #: Provenance only: ``"env:GITHUB_TOKEN"``, ``"env:GH_TOKEN"``, ``"gh-auth-token"``.
    source: str

    def __post_init__(self) -> None:
        # Static messages: never interpolate the value.
        if not self.value.strip():
            raise ValueError("a resolved token needs a non-blank value")
        if not self.source.strip():
            raise ValueError("a resolved token needs a provenance")

    def __str__(self) -> str:
        return f"<token from {self.source}>"


@runtime_checkable
class TokenSource(Protocol):
    """Zero-argument so the plugin cannot reach for ambient state; called once per operation."""

    def __call__(self) -> ResolvedToken | None: ...


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    """Redact credentials from *text*.

    Exact-value replacement of every non-empty entry of *secrets* (longest first, so a secret
    that contains another leaves no residue), then every :data:`TOKEN_PATTERN` shape.
    """
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return TOKEN_PATTERN.sub(REDACTED, text)
