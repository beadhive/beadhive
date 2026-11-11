"""The ``DocFormat`` port and its markdown adapter: the ONE shared docs parser.

A **doc format** recognises a file by its path (:meth:`DocFormat.detect`) and parses its text into
a :class:`ParsedDoc` (:meth:`DocFormat.parse`): the title, the raw frontmatter-style metadata, the
body, the heading sections and the lines that carry bead back-references. Parsing is pure and does
no resolution and no normalization (status/date/owner normalization is a later layer); the docs
reader (:mod:`beadlog_sources.docs_reader`) turns a :class:`ParsedDoc` into an evidence artifact.

The :class:`MarkdownFormat` adapter ports the frontmatter/heading logic of the ``docs`` strategy
(:func:`beadlog_sources._sources.doc_title` and the first ``Beads:`` line bridge) and adds heading
sections. Headings come from markdown-it-py's token stream when the optional ``beadlog-sources[md]``
extra is installed, and from a regex scan otherwise; both paths are fence-aware and agree on the
corpus in the tests. (A setext heading is found only with markdown-it-py.)
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ._sources import split_frontmatter

_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_ -]*?)[ \t]*:[ \t]*(.*?)[ \t]*$")
_BEADS_LINE = re.compile(r"^\s*-?\s*beads?:", re.IGNORECASE)
_EMPTY: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class Section:
    """One heading and the text under it, up to the next heading of any level."""

    level: int
    heading: str
    text: str


@dataclass(frozen=True)
class ParsedDoc:
    """What a :class:`DocFormat` extracts from one document.

    ``title`` is the frontmatter ``title`` else the first heading (``None``: neither). ``metadata``
    is the raw ``key: value`` frontmatter (keys lowercased, quotes stripped, scalars only).
    ``body`` is the text after the frontmatter; ``sections`` its headings in order;
    ``backref_lines`` the lines of the WHOLE document that open with ``Beads:`` / ``- Bead:``.
    """

    title: str | None
    metadata: Mapping[str, str]
    body: str
    sections: tuple[Section, ...] = ()
    backref_lines: tuple[str, ...] = ()


@runtime_checkable
class DocFormat(Protocol):
    """Port: one document format. Pure."""

    name: str
    extensions: tuple[str, ...]

    def detect(self, path: str) -> bool:
        """Whether this format parses the file at ``path`` (by its name; nothing is read)."""
        ...

    def parse(self, text: str) -> ParsedDoc:
        """Parse a document's text."""
        ...


def parse_frontmatter(lines: Sequence[str]) -> dict[str, str]:
    """The ``key: value`` lines of a frontmatter block (hand-parsed subset: scalars only;
    indented, list and comment lines are skipped; a repeated key keeps its first value)."""
    found: dict[str, str] = {}
    for line in lines:
        if not line or line[0] in " \t-#":
            continue
        match = _KEY.match(line)
        if match is None:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip("'\" ")
        found.setdefault(key, value)
    return found


def _regex_headings(lines: Sequence[str]) -> list[tuple[int, int, str]]:
    """``(line index, level, text)`` of the ATX headings outside code fences."""
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        opening = _FENCE.match(line)
        if opening:
            mark = opening.group(1)
            if fence is None:
                fence = mark[0]
            elif mark[0] == fence and not line.strip().strip(fence):
                fence = None
            continue
        if fence is not None:
            continue
        match = _ATX.match(line)
        if match:
            found.append((index, len(match.group(1)), match.group(2).strip()))
    return found


def _token_headings(lines: Sequence[str]) -> list[tuple[int, int, str]]:
    """The same list from markdown-it-py's tokens (raises ``ImportError`` without the extra)."""
    import markdown_it  # noqa: PLC0415 - optional extra, imported lazily

    tokens = markdown_it.MarkdownIt("commonmark").parse("\n".join(lines))
    found: list[tuple[int, int, str]] = []
    for position, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        text = tokens[position + 1].content.strip()
        if text:
            found.append((token.map[0], int(token.tag[1:]), text))
    return found


def markdown_it_available() -> bool:
    """Whether the optional ``markdown-it-py`` extra is importable."""
    try:
        import markdown_it  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


class MarkdownFormat:
    """The ``markdown`` adapter (``.md`` / ``.markdown``).

    ``use_markdown_it``: ``None`` (default) uses markdown-it-py when installed, ``True`` requires
    it, ``False`` forces the regex path.
    """

    name = "markdown"
    extensions: tuple[str, ...] = (".md", ".markdown")

    def __init__(self, use_markdown_it: bool | None = None) -> None:
        available = markdown_it_available()
        if use_markdown_it and not available:
            raise ImportError("markdown-it-py is not installed (pip install beadlog-sources[md])")
        self.tokenized = available if use_markdown_it is None else use_markdown_it

    def detect(self, path: str) -> bool:
        return path.lower().endswith(self.extensions)

    def parse(self, text: str) -> ParsedDoc:
        front, body_lines = split_frontmatter(text)
        metadata = parse_frontmatter(front)
        headings = (_token_headings if self.tokenized else _regex_headings)(body_lines)
        sections = []
        for number, (index, level, heading) in enumerate(headings):
            stop = headings[number + 1][0] if number + 1 < len(headings) else len(body_lines)
            sections.append(Section(level, heading, "\n".join(body_lines[index + 1 : stop])))
        title = metadata.get("title") or (headings[0][2] if headings else None)
        backrefs = tuple(line for line in text.splitlines() if _BEADS_LINE.match(line))
        return ParsedDoc(
            title=title or None,
            metadata=MappingProxyType(metadata) if metadata else _EMPTY,
            body="\n".join(body_lines),
            sections=tuple(sections),
            backref_lines=backrefs,
        )


# Formats shipped with the library, by name; the html adapter registers here when it lands.
BUILTIN_FORMATS: Mapping[str, Any] = MappingProxyType({"markdown": MarkdownFormat})

__all__ = [
    "BUILTIN_FORMATS",
    "DocFormat",
    "MarkdownFormat",
    "ParsedDoc",
    "Section",
    "markdown_it_available",
    "parse_frontmatter",
]
