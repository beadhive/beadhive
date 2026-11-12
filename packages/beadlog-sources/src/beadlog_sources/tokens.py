"""Bead-token recognition shared by every reader: which strings look like a bead id.

Readers do no resolution; they only mark tokens. With ``namespaces`` (the id prefixes of the
registered ledgers) the pattern is :func:`candidate_pattern`, which finds every ``<ns>-<id>``
including digit-free ids such as ``bh-art``. Without them a generic shape is used: a short
lowercase prefix, a hyphen and an id segment that contains a digit, so ordinary hyphenated words
are not matched; it therefore misses digit-free ids.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

TOKEN_MODES = ("namespaces", "generic")

# A bead ID is ``<namespace>-<slug>`` with optional dotted child suffixes, where the slug is
# base36-ish and may itself carry hyphenated segments (`bh-4o07n`, `bhui-do1`, `bh-baml-8u9.17`).
# Anchored on the namespace so it does not match every hyphenated English word, but greedy to the
# right so a multi-segment ID arrives as ONE token; resolution (the ``git-commits`` strategy's
# ``resolve_candidate``) trims back to the longest segment that actually resolves. A token glued
# to surrounding word characters is not matched.
CANDIDATE_TEMPLATE = r"(?<![0-9A-Za-z_-])(?:{ns})-[0-9a-z]+(?:[.-][0-9a-z]+)*(?![0-9A-Za-z_])"


def candidate_pattern(namespaces: Iterable[str]) -> re.Pattern[str]:
    """The candidate regex for one hive's namespace set (the leading segments of its bead ids)."""
    names = sorted({str(name) for name in namespaces}, key=len, reverse=True)
    if not names:
        raise ValueError("no namespaces to anchor on: the live bead ID set was empty")
    alt = "|".join(re.escape(name) for name in names)
    return re.compile(CANDIDATE_TEMPLATE.format(ns=alt), re.IGNORECASE)


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
    "CANDIDATE_TEMPLATE",
    "GENERIC_BEAD_TOKEN",
    "TOKEN_MODES",
    "bead_token_pattern",
    "candidate_pattern",
    "find_bead_tokens",
    "token_mode",
]
