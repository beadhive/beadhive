"""Doc metadata normalization: status / date / owner / supersedes of a parsed document.

Pure and format-neutral: it reads a :class:`~beadlog_sources.doc_formats.ParsedDoc` (markdown
frontmatter and HTML ``<meta>`` / visible Status line both arrive as ``metadata`` with lowercased
keys), the ``## Status`` section of MADR / Nygard ADRs, and ``**Status:**`` / ``* Status:`` /
``Status:`` lines of the body, and fills the ``design-record`` attrs.

Sources, in precedence order (the first one that holds a value decides; a later source is never
consulted to "rescue" an unrecognised value): metadata ``status``, a ``Status`` heading section,
a ``Status:`` line in the body.

- **status** is normalized through the table-driven :data:`STATUS_SYNONYMS` onto
  :data:`~beadlog_sources.evidence.DECLARED_STATUSES`. An unrecognised value is ``None`` here and
  stays only in ``raw``; it is never coerced. ``Superseded by ADR-12`` is ``superseded`` plus the
  ``superseded_by`` entry ``ADR-12`` (the replacing doc; it is NOT a ``supersedes`` entry).
- **date** is ``declared_date`` (``date?``): a day-only input stays ``YYYY-MM-DD`` (never padded
  with a time); an input with a time AND a zone becomes a canonical BeadLog timestamp; anything
  else (including a time without a zone, which would need a guessed zone) is ``None``.
- **owner** is the first of :data:`OWNER_KEYS`, as written.
- **supersedes** is only what this doc replaces (``supersedes`` / ``replaces`` keys): strings as
  written (doc paths or ids) plus ``bead-token`` refs (``where="supersedes"``). **superseded_by**
  (an extra attr) is what replaces this doc, from a ``Superseded by X`` status, with
  ``where="superseded-by"`` refs.

``raw`` keeps what was read for every field that had a value, whether or not it normalized.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from beadlog import format_timestamp

from .doc_formats import ParsedDoc
from .evidence import DECLARED_STATUSES, Ref
from .tokens import GENERIC_BEAD_TOKEN, find_bead_tokens

STATUS_SYNONYMS: Mapping[str, str] = MappingProxyType(
    {
        **dict.fromkeys(
            ("proposed", "proposal", "rfc", "pending", "under review", "in review"), "proposed"
        ),
        **dict.fromkeys(("draft", "wip", "work in progress", "drafting"), "draft"),
        **dict.fromkeys(
            ("accepted", "approved", "adopted", "agreed", "decided", "ratified", "final"),
            "accepted",
        ),
        **dict.fromkeys(
            (
                "delivered",
                "implemented",
                "done",
                "complete",
                "completed",
                "shipped",
                "released",
                "landed",
            ),
            "delivered",
        ),
        **dict.fromkeys(("superseded", "replaced", "obsoleted", "obsolete"), "superseded"),
        **dict.fromkeys(("rejected", "declined", "denied", "withdrawn", "dropped"), "rejected"),
        **dict.fromkeys(("deprecated", "retired", "sunset"), "deprecated"),
    }
)

STATUS_KEYS = ("status",)
DATE_KEYS = ("date", "created", "decided")
OWNER_KEYS = ("owner", "author", "authors", "deciders", "decision-makers")
SUPERSEDES_KEYS = ("supersedes", "replaces")

_SUPERSEDED_BY = re.compile(r"^(?:superseded|replaced|obsoleted)\s+by\s+(.+)$", re.IGNORECASE)
_STATUS_LINE = re.compile(
    r"^\s*(?:[*+-]\s+)?(?:\*\*|__)?status(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(\S.*)$", re.IGNORECASE
)
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
_PAREN = re.compile(r"\s*[(\[][^)\]]*[)\]]")
_SPLIT = re.compile(r"\s*(?:,|;|\band\b)\s*")
_REST_OK = re.compile(r"^(?:by|in|on|since|with|via|as of)\b")
_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_DAY = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DAY_LONG = re.compile(
    r"^(?:[A-Za-z]+,?\s+)?([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$"
)
_DAY_LONG_DMY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})$")
_MOMENT = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(Z|UTC|[+-]\d{2}(?::?\d{2})?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DocMetadata:
    """The normalized metadata of one document; ``raw`` holds what was read, per field."""

    declared_status: str | None = None
    declared_date: str | None = None
    owner: str | None = None
    supersedes: tuple[str, ...] = ()
    superseded_by: tuple[str, ...] = ()
    refs: tuple[Ref, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def attrs(self) -> dict[str, Any]:
        """The ``design-record`` attrs."""
        return {
            "declared_status": self.declared_status,
            "declared_date": self.declared_date,
            "owner": self.owner,
            "supersedes": list(self.supersedes),
            "superseded_by": list(self.superseded_by),
        }


# -- text helpers ----------------------------------------------------------------------------


def _plain(text: str) -> str:
    """``text`` without emphasis / code marks, link syntax (the link text is kept) and padding."""
    text = _LINK.sub(lambda m: m.group(1) or m.group(2), text)
    return " ".join(text.replace("**", "").replace("__", "").replace("`", "").split()).strip("*_ ")


def _unwrap(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] + text[-1] in ("[]", '""', "''"):
        text = text[1:-1].strip()
    return text


def _first(metadata: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


# -- status ----------------------------------------------------------------------------------


def _split_refs(text: str) -> list[str]:
    """Doc paths / ids as written: link targets win over link text, parentheticals are dropped."""
    text = _LINK.sub(lambda m: m.group(2) or m.group(1), _unwrap(text))
    items = [_unwrap(_PAREN.sub("", part)).strip(" .`*_") for part in _SPLIT.split(text)]
    return [item for item in items if item]


def normalize_status(raw: str) -> tuple[str | None, tuple[str, ...]]:
    """``(declared_status, superseded_by)`` of a raw status text; ``(None, ())`` when unknown.

    The leading phrase decides (``Accepted (2026-01-02)``, ``Implemented - shipped in 1.2``);
    ``Superseded by X`` also yields ``X``.
    """
    text = _plain(raw).rstrip(".").strip()
    folded = text.lower()
    by = _SUPERSEDED_BY.match(" ".join(raw.replace("**", "").replace("__", "").split()))
    if by:
        return "superseded", tuple(_split_refs(by.group(1)))
    if folded in STATUS_SYNONYMS:
        return STATUS_SYNONYMS[folded], ()
    words = folded.split()
    for count in range(min(3, len(words) - 1), 0, -1):
        head = " ".join(words[:count])
        status = STATUS_SYNONYMS.get(head.strip(",;:-"))
        if status is None:
            continue
        rest = folded[len(" ".join(words[:count])) :].lstrip()
        if _REST_OK.match(rest) or not rest[:1].isalpha():
            return status, ()
    stripped = re.split(r"\s*[(\[:,;]|\s+[-–—]\s", folded, maxsplit=1)[0].strip()
    return STATUS_SYNONYMS.get(stripped), ()


def _section_status(parsed: ParsedDoc) -> str | None:
    for section in parsed.sections:
        if section.heading.strip().strip("#*: ").lower() == "status":
            for line in section.text.splitlines():
                line = line.strip().lstrip("*+-").strip()
                if line:
                    return line
    return None


def _line_status(parsed: ParsedDoc) -> str | None:
    fence = ""
    for line in parsed.body.splitlines():
        marker = _FENCE.match(line)
        if marker:
            fence = "" if fence and marker.group(1)[0] == fence else (fence or marker.group(1)[0])
            continue
        if not fence:
            match = _STATUS_LINE.match(line)
            if match:
                return match.group(1).strip()
    return None


# -- date ------------------------------------------------------------------------------------


def _day(year: int, month: int, day: int) -> str | None:
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def normalize_date(raw: str) -> str | None:
    """``YYYY-MM-DD`` for a day-only date, a canonical BeadLog timestamp for a time with a zone,
    else ``None``. A day-only input is never padded to a timestamp."""
    text = _plain(raw).strip(" .")
    moment = _MOMENT.match(text)
    if moment:
        day, clock, zone = moment.groups()
        zone = "+00:00" if zone.upper() in ("Z", "UTC") else zone
        if re.fullmatch(r"[+-]\d{2}", zone):
            zone += ":00"
        elif re.fullmatch(r"[+-]\d{4}", zone):
            zone = zone[:3] + ":" + zone[3:]
        try:
            return format_timestamp(datetime.fromisoformat(f"{day}T{clock}{zone}"))
        except ValueError:
            return None
    numeric = _DAY.match(text)
    if numeric:
        return _day(*map(int, numeric.groups()))
    long = _DAY_LONG.match(text)
    if long and long.group(1).lower() in _MONTHS:
        return _day(int(long.group(3)), _MONTHS[long.group(1).lower()], int(long.group(2)))
    dmy = _DAY_LONG_DMY.match(text)
    if dmy and dmy.group(2).lower() in _MONTHS:
        return _day(int(dmy.group(3)), _MONTHS[dmy.group(2).lower()], int(dmy.group(1)))
    return None


# -- the document ----------------------------------------------------------------------------


def normalize_doc_metadata(
    parsed: ParsedDoc, pattern: re.Pattern[str] | None = None
) -> DocMetadata:
    """Normalize ``parsed``'s status, date, owner and supersedes (see the module docstring)."""
    pattern = pattern or GENERIC_BEAD_TOKEN
    metadata = parsed.metadata
    raw: dict[str, Any] = {}
    supersedes: list[str] = []
    superseded_by: list[str] = []

    status_raw, source = _first(metadata, STATUS_KEYS), "frontmatter"
    if status_raw is None:
        status_raw, source = _section_status(parsed), "section"
    if status_raw is None:
        status_raw, source = _line_status(parsed), "line"
    status = None
    if status_raw is not None:
        raw["status"] = {"raw": status_raw, "source": source}
        status, by = normalize_status(status_raw)
        superseded_by.extend(by)

    date_raw = _first(metadata, DATE_KEYS)
    date = None
    if date_raw is not None:
        raw["date"] = date_raw
        date = normalize_date(date_raw)

    owner_raw = _first(metadata, OWNER_KEYS)
    owner = None
    if owner_raw is not None:
        raw["owner"] = owner_raw
        owner = _unwrap(owner_raw) or None

    declared = _first(metadata, SUPERSEDES_KEYS)
    if declared is not None:
        raw["supersedes"] = declared
        supersedes.extend(_split_refs(declared))

    unique = tuple(dict.fromkeys(supersedes))
    by = tuple(dict.fromkeys(superseded_by))
    refs = tuple(
        Ref("bead-token", token, where)
        for where, items in (("supersedes", unique), ("superseded-by", by))
        for token in find_bead_tokens("\n".join(items), pattern)
    )
    return DocMetadata(status, date, owner, unique, by, refs, MappingProxyType(raw))


__all__ = [
    "DATE_KEYS",
    "OWNER_KEYS",
    "STATUS_KEYS",
    "STATUS_SYNONYMS",
    "SUPERSEDES_KEYS",
    "DocMetadata",
    "normalize_date",
    "normalize_doc_metadata",
    "normalize_status",
]

assert set(STATUS_SYNONYMS.values()) <= set(DECLARED_STATUSES)
