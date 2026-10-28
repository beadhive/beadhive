"""The leak check: what must never leave in an outbound payload (``bh-6wj9y`` R10).

A pure function over rendered text and a :class:`LeakPolicy` (external-intake-module-adr.md
section 13). It runs at draft (O1), at every render and, finally, on the exact bytes a send would
post (O9). Standard library only: the policy (fleet prefixes, closed dimensions, path roots,
owned label prefixes) is built in the composition root and passed in, and the secret scan is the
injected sanitizer seam (``Sanitizer`` in the contracts; the shared ``scrub()`` until
``bh-o2mjz`` lands).

Finding kinds and whether a named per-finding ``--publish`` may override one:

========================  ===========================================================  ==========
kind                      detects                                                      override
========================  ===========================================================  ==========
``secret``                a sanitizer scan hit                                         never
``no_sanitizer``          no sanitizer is bound at all (fail closed)                   never
``bead_id``               ``<prefix>-<id>`` for every fleet prefix                     yes
``branch``                ``wt/bead/...``, ``wt/batch/...``, ``mol/...``               yes
``private_label``         ``<dimension>:<value>`` of a closed or configured dimension  yes
``seat``                  seat identities (``dev/...``, ``disp/...``, ...)             yes
``path``                  host paths: ``.bh/``, home directories, configured roots     yes
``label_not_owned``       a label operation outside the owned prefixes                 never
``closing_keyword``       ``fixes #N``, ``closes #N``, ...                             warning
========================  ===========================================================  ==========

A finding never carries a secret's text: its ``match`` is redacted. Ids are ``<kind>:<n>`` (``n``
counts from 1 per kind, in text order), the names an operator publishes by.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

#: What a secret's text is replaced with in a finding.
REDACTED = "***"


class LeakKind(StrEnum):
    SECRET = "secret"
    NO_SANITIZER = "no_sanitizer"
    BEAD_ID = "bead_id"
    BRANCH = "branch"
    PRIVATE_LABEL = "private_label"
    SEAT = "seat"
    PATH = "path"
    LABEL_NOT_OWNED = "label_not_owned"
    CLOSING_KEYWORD = "closing_keyword"


#: Findings no override can publish: the operation stays ``held`` until the text changes.
NEVER_OVERRIDABLE = frozenset({LeakKind.SECRET, LeakKind.NO_SANITIZER, LeakKind.LABEL_NOT_OWNED})
#: Findings that only warn: they never hold an operation.
WARNINGS = frozenset({LeakKind.CLOSING_KEYWORD})

#: Internal branch shapes (``bh-6wj9y`` R10). A policy may add more.
DEFAULT_BRANCH_PATTERNS = (r"wt/bead/\S+", r"wt/batch/\S+", r"mol/\S+")
#: Seat prefixes of the roles matrix, legacy spellings included. A policy may replace them.
DEFAULT_SEAT_PREFIXES = (
    "dev/",
    "disp/",
    "rev/",
    "review/",
    "merge/",
    "plan/",
    "analyst/",
    "super/",
    "dir/",
    "cust/",
    "ctrl/",
    "warden/",
    "release/",
    "releaser/",
    "ops/",
    "crew/",
    "coord/",
)
#: Host path shapes that are always private; the policy's ``path_roots`` add exact roots.
DEFAULT_PATH_PATTERNS = (r"(?:\S*/)?\.bh/\S*", r"/home/[^/\s]+\S*", r"/Users/[^/\s]+\S*")

_CLOSING = re.compile(
    r"(?<![A-Za-z0-9])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
    r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SanitizerFinding:
    """One sanitizer hit: its kind and span in the scanned text, never the text itself."""

    kind: str
    start: int
    end: int


class Scanner(Protocol):
    """What the leak check needs of a sanitizer (the contracts' ``Sanitizer`` satisfies it)."""

    def scan(self, text: str) -> Sequence[SanitizerFinding]: ...


@dataclass(frozen=True)
class LeakPolicy:
    """What counts as internal, built in the composition root.

    ``bead_prefixes`` is every fleet prefix, operational and public; it has no default, because a
    check without it would pass every bead id. ``private_dimensions`` is
    ``registry.closed_dimensions`` plus configured ones. ``owned_label_prefixes`` is the
    adapter's ``write.labels.owned_prefixes``; empty means no label may be written."""

    bead_prefixes: frozenset[str]
    private_dimensions: frozenset[str] = frozenset()
    owned_label_prefixes: tuple[str, ...] = ()
    path_roots: tuple[str, ...] = ()
    seat_prefixes: tuple[str, ...] = DEFAULT_SEAT_PREFIXES
    branch_patterns: tuple[str, ...] = DEFAULT_BRANCH_PATTERNS

    def owns(self, label: str) -> bool:
        return any(label.startswith(prefix) for prefix in self.owned_label_prefixes if prefix)


@dataclass(frozen=True)
class LeakFinding:
    """One reason a payload may not leave as it is. ``match`` is redacted for a secret."""

    id: str
    kind: LeakKind
    match: str
    start: int = -1  # -1: not a span of the text (a label, or no sanitizer at all)

    @property
    def overridable(self) -> bool:
        return self.kind not in NEVER_OVERRIDABLE

    @property
    def warning(self) -> bool:
        return self.kind in WARNINGS


@dataclass(frozen=True)
class Override:
    """A deliberate publish of one finding, recorded with the operator who made it."""

    finding: str  # the finding id the operator named, ``<kind>:<n>``
    kind: LeakKind
    match: str
    actor: str


_EDGE = r"(?<![A-Za-z0-9_./-])"
_TAIL = r"(?![A-Za-z0-9_-])"


def _bead_pattern(prefixes: Collection[str]) -> re.Pattern[str] | None:
    names = sorted({p for p in prefixes if p}, key=len, reverse=True)
    if not names:
        return None
    alternatives = "|".join(re.escape(p) for p in names)
    return re.compile(rf"(?<![A-Za-z0-9_-])(?:{alternatives})-[a-z0-9]{{2,}}(?:\.[0-9]+)*{_TAIL}")


def _spans(pattern: re.Pattern[str] | None, text: str) -> list[tuple[int, str]]:
    if pattern is None:
        return []
    return [(m.start(), m.group(0)) for m in pattern.finditer(text)]


def _alternation(parts: Iterable[str]) -> re.Pattern[str] | None:
    parts = [p for p in parts if p]
    return re.compile("|".join(f"(?:{p})" for p in parts)) if parts else None


def check(
    text: str,
    policy: LeakPolicy,
    *,
    sanitizer: Scanner | None,
    labels: Iterable[str] = (),
) -> tuple[LeakFinding, ...]:
    """Every finding in ``text`` (and, for a label operation, its ``labels``), in a stable order:
    by kind as declared, then by position. ``sanitizer=None`` is itself a finding (fail closed)."""
    found: dict[LeakKind, list[tuple[int, str]]] = {kind: [] for kind in LeakKind}
    if sanitizer is None:
        found[LeakKind.NO_SANITIZER].append((-1, "no sanitizer is bound"))
    else:
        for hit in sanitizer.scan(text):
            found[LeakKind.SECRET].append((hit.start, REDACTED))
    found[LeakKind.BEAD_ID] = _spans(_bead_pattern(policy.bead_prefixes), text)
    found[LeakKind.BRANCH] = _spans(
        _alternation(r"(?<![A-Za-z0-9_-])" + p for p in policy.branch_patterns), text
    )
    dims = sorted({d for d in policy.private_dimensions if d}, key=len, reverse=True)
    if dims:
        alternatives = "|".join(re.escape(d) for d in dims)
        private = re.compile(rf"{_EDGE}(?:{alternatives}):[A-Za-z0-9][A-Za-z0-9_.-]*")
        found[LeakKind.PRIVATE_LABEL] = _spans(private, text)
    seats = sorted({s for s in policy.seat_prefixes if s}, key=len, reverse=True)
    if seats:
        alternatives = "|".join(re.escape(s) for s in seats)
        seat = re.compile(rf"(?<![A-Za-z0-9_./:-])(?:{alternatives})[A-Za-z0-9][A-Za-z0-9_.-]*")
        found[LeakKind.SEAT] = _spans(seat, text)
    roots = [re.escape(r) + r"\S*" for r in policy.path_roots if r]
    found[LeakKind.PATH] = _spans(_alternation([*DEFAULT_PATH_PATTERNS, *roots]), text)
    found[LeakKind.LABEL_NOT_OWNED] = [(-1, label) for label in labels if not policy.owns(label)]
    found[LeakKind.CLOSING_KEYWORD] = _spans(_CLOSING, text)
    findings: list[LeakFinding] = []
    for kind in LeakKind:
        hits = sorted(found[kind]) if kind is not LeakKind.LABEL_NOT_OWNED else found[kind]
        for n, (start, match) in enumerate(hits, start=1):
            findings.append(LeakFinding(f"{kind.value}:{n}", kind, match, start))
    return tuple(findings)


def is_published(finding: LeakFinding, overrides: Iterable[Override]) -> bool:
    """True when an override publishes ``finding`` (same kind and text; an id moves with edits)."""
    return finding.overridable and any(
        o.kind is finding.kind and o.match == finding.match for o in overrides
    )


def unresolved(
    findings: Iterable[LeakFinding], overrides: Iterable[Override] = ()
) -> tuple[LeakFinding, ...]:
    """The findings that hold an operation: neither a warning nor deliberately published."""
    overrides = tuple(overrides)
    return tuple(f for f in findings if not f.warning and not is_published(f, overrides))


__all__ = [
    "DEFAULT_BRANCH_PATTERNS",
    "DEFAULT_PATH_PATTERNS",
    "DEFAULT_SEAT_PREFIXES",
    "NEVER_OVERRIDABLE",
    "REDACTED",
    "WARNINGS",
    "LeakFinding",
    "LeakKind",
    "LeakPolicy",
    "Override",
    "SanitizerFinding",
    "Scanner",
    "check",
    "is_published",
    "unresolved",
]
