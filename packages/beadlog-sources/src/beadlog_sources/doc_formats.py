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
from html.parser import HTMLParser
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


_STATUS_LINE = re.compile(r"^status[ \t]*:[ \t]*(\S.*?)[ \t]*$", re.IGNORECASE)
_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)
_HTML_HEADING = re.compile(r"h[1-6]")
_HTML_SKIP = frozenset({"script", "style", "noscript", "template"})
_HTML_BLOCK = frozenset(
    "p div br li ul ol tr table section article header footer pre blockquote dd dt dl hr".split()
)


class _HtmlScan(HTMLParser):
    """Collects the title, ``<meta>``, headings and visible text (never script/style content)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.parts: list[str] = []
        self.headings: list[tuple[int, str, int, int]] = []  # level, text, start, end in parts
        self._skip = 0
        self._in_title = False
        self._level = 0
        self._heading: list[str] = []
        self._start = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HTML_SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            fields = dict(attrs)
            name, content = (fields.get("name") or "").strip().lower(), fields.get("content")
            if name and content is not None:
                self.meta.setdefault(name, content.strip())
        elif _HTML_HEADING.fullmatch(tag):
            self._close_heading()
            self._level, self._heading, self._start = int(tag[1]), [], len(self.parts)
        elif tag in _HTML_BLOCK:
            self._close_heading()  # a block element cannot sit inside a heading
        if tag in _HTML_BLOCK or _HTML_HEADING.fullmatch(tag):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif _HTML_HEADING.fullmatch(tag):
            self._close_heading()
        if tag in _HTML_BLOCK or _HTML_HEADING.fullmatch(tag):
            self.parts.append("\n")

    def _close_heading(self) -> None:
        text = " ".join("".join(self._heading).split())
        if self._level and text:
            self.headings.append((self._level, text, self._start, len(self.parts)))
        self._level, self._heading = 0, []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)
        if self._level:
            self._heading.append(data)

    def close(self) -> None:
        super().close()
        self._close_heading()


class HtmlFormat:
    """The ``html`` adapter (``.html`` / ``.htm``), on the stdlib ``html.parser`` only.

    Title: ``<title>`` else the first ``<h1>``. Metadata: ``<meta name=... content=...>`` with
    lowercased names (the frontmatter shape; values stay raw), plus a visible ``Status:`` line when
    no ``status`` meta exists. Sections are ``h1``-``h6`` headings with their text. Script, style,
    noscript and template content is ignored; nothing is fetched or executed.
    """

    name = "html"
    extensions: tuple[str, ...] = (".html", ".htm")

    def detect(self, path: str) -> bool:
        return path.lower().endswith(self.extensions)

    @staticmethod
    def decode(data: bytes) -> str:
        """Decode with a BOM or ``<meta charset>`` declaration, else UTF-8 (with replacement)."""
        if data.startswith(b"\xef\xbb\xbf"):
            return data[3:].decode("utf-8", errors="replace")
        match = _CHARSET.search(data[:2048])
        if match:
            try:
                return data.decode(match.group(1).decode("ascii"), errors="replace")
            except LookupError:
                pass
        return data.decode("utf-8", errors="replace")

    def parse(self, text: str) -> ParsedDoc:
        scan = _HtmlScan()
        scan.feed(text)
        scan.close()
        lines = "".join(scan.parts).split("\n")
        lines = [" ".join(line.split()) for line in lines]
        # Section text: the visible text between consecutive headings.
        ends = [h[2] for h in scan.headings[1:]] + [len(scan.parts)] * bool(scan.headings)
        pieces = [
            "".join(scan.parts[h[3] : end]) for h, end in zip(scan.headings, ends, strict=True)
        ]
        sections = tuple(
            Section(level, heading, "\n".join(x for x in map(str.strip, piece.split("\n")) if x))
            for (level, heading, _, _), piece in zip(scan.headings, pieces, strict=True)
        )
        metadata = dict(scan.meta)
        for line in lines:
            status = _STATUS_LINE.match(line)
            if status:
                metadata.setdefault("status", status.group(1))
                break
        h1 = next((heading for level, heading, _, _ in scan.headings if level == 1), None)
        title = " ".join(scan.title.split()) or h1
        return ParsedDoc(
            title=title or None,
            metadata=MappingProxyType(metadata) if metadata else _EMPTY,
            body="\n".join(line for line in lines if line),
            sections=sections,
            backref_lines=tuple(line for line in lines if _BEADS_LINE.match(line)),
        )


# Formats shipped with the library, by name (factories).
BUILTIN_FORMATS: Mapping[str, Any] = MappingProxyType(
    {"markdown": MarkdownFormat, "html": HtmlFormat}
)

__all__ = [
    "BUILTIN_FORMATS",
    "DocFormat",
    "HtmlFormat",
    "MarkdownFormat",
    "ParsedDoc",
    "Section",
    "markdown_it_available",
    "parse_frontmatter",
]
