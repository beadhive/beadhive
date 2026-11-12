"""Bead-token recognition shared by every reader: which strings look like a bead id.

Readers do no resolution; they only mark tokens. With ``namespaces`` (the id prefixes of the
registered ledgers) the pattern is :func:`beadlog_sources.commits.candidate_pattern`, which finds
every ``<ns>-<id>`` including digit-free ids such as ``bh-art``. Without them a generic shape is
used: a short lowercase prefix, a hyphen and an id segment that contains a digit, so ordinary
hyphenated words are not matched; it therefore misses digit-free ids.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .commits import candidate_pattern

TOKEN_MODES = ("namespaces", "generic")

GENERIC_BEAD_TOKEN = re.compile(
    r"(?<![0-9A-Za-z_-])[a-z][a-z0-9]{1,11}-(?=[0-9a-z]*[0-9])[0-9a-z]+(?:[.-][0-9a-z]+)*"
    r"(?![0-9A-Za-z_])",
    re.IGNORECASE,
)


def bead_token_pattern(namespaces: Sequence[str] | None) -> re.Pattern[str]:
    """The token pattern for ``namespaces`` (``None`` or empty: the generic shape)."""
    names = [name for name in (namespaces or ()) if name]
    return candidate_pattern(names) if names else GENERIC_BEAD_TOKEN


def token_mode(namespaces: Sequence[str] | None) -> str:
    """``"namespaces"`` when a namespace set anchors the pattern, else ``"generic"``."""
    return "namespaces" if any(namespaces or ()) else "generic"


def find_bead_tokens(text: str, pattern: re.Pattern[str]) -> list[str]:
    """The tokens of ``text``, lowercased, in order of first appearance, de-duplicated."""
    seen: dict[str, None] = {}
    for match in pattern.finditer(text):
        seen.setdefault(match.group(0).lower(), None)
    return list(seen)


__all__ = [
    "GENERIC_BEAD_TOKEN",
    "TOKEN_MODES",
    "bead_token_pattern",
    "find_bead_tokens",
    "token_mode",
]
