"""The ``docs`` source reader: design and decision documents -> ``design-record`` artifacts.

One artifact per document, keyed ``doc:<repo-relative path>``. The reader is pure (it reads files
and runs ONE ``git log`` for every document's first-add commit) and does no resolution: every
bead-looking token is a ``Ref(kind="bead-token")``. Parsing goes through the shared
:class:`~beadlog_sources.doc_formats.DocFormat` port.

Options (``ReadRequest.options``; every value may be a string, lists are comma-separated):

- ``layouts``: layout presets to read (default: all of :data:`LAYOUT_PRESETS`); ``none`` reads no
  preset directory. Every file below a preset directory whose name a format detects is read.
  ``docs-html`` is the non-recursive glob ``docs/*.html``; ``reviews`` is ``docs/reviews``
  (review-tool HTML exports).
- ``globs``: extra glob patterns relative to the root (``docs/**/*.md``), routed by detection.
- ``files``: explicit files of any extension, routed by ``DocFormat.detect`` (no format detects
  one: an error).
- ``formats``: enabled format names (``markdown``, ``html``; default ``markdown``).
- ``namespaces``: bead id prefixes that make a token bead-looking (``bh,hq``), see
  :mod:`beadlog_sources.tokens`; without them a generic shape is used (it misses digit-free ids).
  The run header records ``token_mode`` (``namespaces`` or ``generic``).
- ``first_add``: record the first-add commit time (default true); ``git``: the git executable.

``occurred_at`` is the committer time of the commit that first added the file, ``None`` when git
cannot say. ``declared_status`` / ``declared_date`` / ``owner`` / ``supersedes`` come from
:func:`beadlog_sources.doc_metadata.normalize_doc_metadata` (frontmatter, HTML meta, ``## Status``
sections, ``Status:`` lines); the raw frontmatter and every raw declared value stay in
``raw_attrs`` (``frontmatter``, ``declared``), and bead-looking supersedes entries add
``bead-token`` refs with ``where="supersedes"``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ._sources import SourceError, csv, first_adds, is_git_repo, truthy
from .doc_formats import BUILTIN_FORMATS, DocFormat, ParsedDoc
from .doc_metadata import normalize_doc_metadata
from .evidence import Artifact, Category, Ref
from .readers import ReadRequest
from .tokens import bead_token_pattern, find_bead_tokens, token_mode

READER_NAME = "docs"
READER_VERSION = "0.2.0"
KIND = "doc"
DEFAULT_FORMATS = ("markdown",)

# Layout preset -> directory (relative to the root), read recursively.
LAYOUT_PRESETS: Mapping[str, str] = MappingProxyType(
    {
        "decisions": "docs/decisions",
        "design": "docs/design",
        "adr": "docs/adr",
        "architecture": "docs/architecture",
        "docs-html": "docs/*.html",
        "reviews": "docs/reviews",
    }
)

_BEADS_KEY = re.compile(r"^\s*-?\s*beads?:", re.IGNORECASE)
_GLOB_CHARS = re.compile(r"[*?\[]")


def _formats(names: Iterable[str]) -> tuple[DocFormat, ...]:
    out: list[DocFormat] = []
    for name in names:
        factory = BUILTIN_FORMATS.get(name)
        if factory is None:
            raise SourceError(f"unknown doc format {name!r} (known: {', '.join(BUILTIN_FORMATS)})")
        out.append(factory())
    return tuple(out)


def _detect(formats: Iterable[DocFormat], path: str) -> DocFormat | None:
    for doc_format in formats:
        if doc_format.detect(path):
            return doc_format
    return None


def _prefix(pattern: str) -> str:
    """The literal leading directories of a glob, usable as a git pathspec."""
    parts = pattern.split("/")
    keep: list[str] = []
    for part in parts[:-1]:
        if _GLOB_CHARS.search(part):
            break
        keep.append(part)
    return "/".join(keep) or "."


class DocsReader:
    """Reads design/decision documents into ``design-record`` artifacts."""

    name = READER_NAME
    version = READER_VERSION
    categories = (Category.DESIGN_RECORD,)

    def read(self, request: ReadRequest) -> Iterable[Artifact]:
        options = request.options
        root = Path(request.root)
        formats = _formats(csv(options.get("formats", ",".join(DEFAULT_FORMATS))))
        pattern = self._pattern(options)
        found = self._discover(root, options, formats)
        adds: dict[str, Any] = {}
        git = str(options.get("git", "git"))
        if found and truthy(options.get("first_add", "true")) and is_git_repo(root, git):
            specs = sorted({spec for _, spec in found.values()})
            adds = first_adds(root, specs, git)
        for path in sorted(found):
            doc_format, _ = found[path]
            data = (root / path).read_bytes()
            decode = getattr(doc_format, "decode", None)
            text = decode(data) if decode else data.decode("utf-8", errors="replace")
            yield self.artifact(path, doc_format, text, pattern, adds.get(path))

    # -- discovery -------------------------------------------------------------------------

    @staticmethod
    def _pattern(options: Mapping[str, Any]) -> re.Pattern[str]:
        return bead_token_pattern(csv(options.get("namespaces", "")))

    @staticmethod
    def header_options(options: Mapping[str, Any]) -> dict[str, Any]:
        """The options recorded in the run's ReaderHeader: the given ones plus ``token_mode``."""
        return {**options, "token_mode": token_mode(csv(options.get("namespaces", "")))}

    @staticmethod
    def _discover(
        root: Path, options: Mapping[str, Any], formats: tuple[DocFormat, ...]
    ) -> dict[str, tuple[DocFormat, str]]:
        """Repo-relative posix path -> (format, git pathspec that covers it)."""
        found: dict[str, tuple[DocFormat, str]] = {}

        def add(path: Path, spec: str) -> None:
            if not path.is_file():
                return
            relative = path.relative_to(root).as_posix()
            doc_format = _detect(formats, relative)
            if doc_format is not None:
                found.setdefault(relative, (doc_format, spec))

        raw_layouts = str(options.get("layouts", ",".join(LAYOUT_PRESETS)))
        layouts = [] if raw_layouts.strip().lower() == "none" else csv(raw_layouts)
        for layout in layouts:
            directory = LAYOUT_PRESETS.get(layout)
            if directory is None:
                raise SourceError(
                    f"unknown docs layout {layout!r} (known: {', '.join(LAYOUT_PRESETS)})"
                )
            if _GLOB_CHARS.search(directory):  # a glob preset (not recursive)
                for path in sorted(root.glob(directory)):
                    add(path, _prefix(directory))
                continue
            for path in sorted((root / directory).rglob("*")):
                add(path, directory)
        for glob in csv(options.get("globs", "")):
            for path in sorted(root.glob(glob)):
                add(path, _prefix(glob))
        for name in csv(options.get("files", "")):
            path = root / name
            if not path.is_file():
                raise SourceError(f"docs file {name!r} does not exist under {root}")
            relative = path.relative_to(root).as_posix()
            doc_format = _detect(formats, relative)
            if doc_format is None:
                raise SourceError(f"no enabled doc format detects {name!r}")
            found.setdefault(relative, (doc_format, name))
        return found

    # -- one document ----------------------------------------------------------------------

    def artifact(
        self,
        path: str,
        doc_format: DocFormat,
        text: str,
        pattern: re.Pattern[str],
        add: Any = None,
    ) -> Artifact:
        parsed = doc_format.parse(text)
        raw_attrs: dict[str, Any] = {"format": doc_format.name}
        if parsed.metadata:
            raw_attrs["frontmatter"] = dict(parsed.metadata)
        refs = self._refs(parsed, pattern)
        declared = normalize_doc_metadata(parsed, pattern)
        if declared.raw:
            raw_attrs["declared"] = dict(declared.raw)
        refs.extend(declared.refs)
        occurred_at = None
        if add is not None:
            raw_attrs["first_add"] = {"sha": add.sha, "date": add.date}
            occurred_at = add.when
            refs.append(Ref("sha", add.sha, "git-add"))
        return Artifact(
            category=Category.DESIGN_RECORD,
            kind=KIND,
            key=f"doc:{path}",
            title=parsed.title or Path(path).stem,
            occurred_at=occurred_at,
            attrs=declared.attrs(),
            raw_attrs=raw_attrs,
            refs=tuple(refs),
            reader=self.name,
            reader_version=self.version,
        )

    @staticmethod
    def _refs(parsed: ParsedDoc, pattern: re.Pattern[str]) -> list[Ref]:
        """Bead-looking tokens, in order, de-duplicated per ``where``: those on ``Beads:`` lines
        (``back-ref``), in the frontmatter values, and in the body."""
        refs: list[Ref] = []
        seen: set[tuple[str, str]] = set()

        def collect(text: str, where: str) -> None:
            for token in find_bead_tokens(text, pattern):
                if (where, token) not in seen:
                    seen.add((where, token))
                    refs.append(Ref("bead-token", token, where))

        for line in parsed.backref_lines:
            collect(line, "back-ref")
        collect("\n".join(parsed.metadata.values()), "frontmatter")
        collect(parsed.body, "body")
        return refs


__all__ = ["DEFAULT_FORMATS", "LAYOUT_PRESETS", "DocsReader"]
