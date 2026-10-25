"""Prototype of the stdlib-only credential module the plugin layer needs (feeds ADR open item O1).

Proposed home: ``packages/beadhive-plugins/src/beadhive_plugins/credentials.py`` (a library
package: stdlib only, imported statically by root and, through its public ``__all__``, by plugin
packages). It holds the three things both sides must agree on byte for byte:

* ``ResolvedToken``: the value type root BUILDS and the adapter RECEIVES. Redaction in ``repr``
  and ``str`` must be a property of the one definition, or root could hand over a leaky object.
* ``TokenSource``: the injected port (a zero-argument callable, so the plugin never sees the
  environment, the ``gh`` binary or ``beadhive.run``).
* ``scrub``: the one rendering of untrusted text for output, logs and errors.

Nothing here talks to the network, the environment or a subprocess.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

REDACTED = "***"
TOKEN_PATTERN = re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b")


@dataclass(frozen=True)
class ResolvedToken:
    value: str = field(repr=False)
    source: str  # provenance only: "env:GH_TOKEN", "gh-auth-token", "injected"

    def __str__(self) -> str:
        return f"<token from {self.source}>"


@runtime_checkable
class TokenSource(Protocol):
    """Zero-argument so the plugin cannot reach for ambient state; called once per operation."""

    def __call__(self) -> ResolvedToken | None: ...


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    """Exact-value replace (longest secret first) plus the token-shape pattern."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return TOKEN_PATTERN.sub(REDACTED, text)


# --- the ROOT side of the seam (proposed home: a root credentials module, not the library) ------

#: The logical source names ``credential.sources`` may carry. Each maps to something the MANIFEST
#: declares: ``env`` to its ``environment-variable`` sources, ``gh`` to its optional ``gh``
#: executable. A name outside this set is a config error.
SOURCE_NAMES = ("env", "gh")


def build_token_source(
    names: Sequence[str],
    *,
    environ: Mapping[str, str],
    env_order: Sequence[str],
    gh_auth_token: Callable[[], str | None],
) -> TokenSource:
    """Resolve in the order of ``names`` (blank counts as unset); never cache, never log."""
    unknown = [n for n in names if n not in SOURCE_NAMES]
    if unknown:
        raise ValueError(f"credential.sources: unknown source name(s) {unknown}")

    def source() -> ResolvedToken | None:
        for name in names:
            if name == "env":
                for var in env_order:
                    if value := (environ.get(var) or "").strip():
                        return ResolvedToken(value, f"env:{var}")
            elif value := (gh_auth_token() or "").strip():
                return ResolvedToken(value, "gh-auth-token")
        return None

    return source
