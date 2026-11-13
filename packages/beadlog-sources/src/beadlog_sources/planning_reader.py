"""The ``planning`` source reader: plan trees and checklists -> ``plan-item`` artifacts.

One artifact per plan item, keyed ``plan:<path>#<item-id>`` (``path`` is the plan file, or the
phase directory for a GSD phase; ``item-id`` is stable across runs: the GSD plan key ``01-01`` / the
literal ``phase``, or a roadmap item's ancestor-slug path such as ``phase-1/setup-repo``, see
:func:`roadmap_items`). The reader is pure (files plus ONE ``git log`` for first-add commit times)
and does no resolution:
every bead-looking token is a ``Ref(kind="bead-token")`` (:mod:`beadlog_sources.tokens`).
Provenance is ``observed``: the structure is stated by the files, not inferred.

Parsing goes through the :class:`PlanningFormat` port, one adapter per layout:

- ``gsd``: a ``.planning/phases/NN-<name>/NN-MM-PLAN.md`` tree (:func:`scan_tree`; the neutral
  ``planning`` strategy reads its phases through :meth:`GsdFormat.phases`). A phase is an item
  with no parent, closed when it has plans and every one has a ``-SUMMARY.md``; a plan's parent
  is its phase, its ``depends_on`` are the resolved plan keys, and it is done when its summary
  exists.
- ``roadmap``: headed checklists and implementation-plan markdown.
  Headings nest by level (a single ``#`` is the document title, not an item); a
  ``- [ ]`` / ``- [x]`` item nests under the nearest heading, or under the checkbox item of lesser
  indentation. A checkbox is done when ticked; a heading is done when it has checkbox descendants
  and all are done. ``(depends: <item text or id>, ...)`` at the end of an item's text names
  dependencies by slug within the same file.

Attrs: ``parent`` (the parent artifact key or ``None``), ``depends_on`` (artifact keys),
``done_marker`` and ``order`` (0-based position among the siblings of one parent). Dependencies
that name nothing are kept in ``raw_attrs.unresolved_depends_on``.

Options (``ReadRequest.options``; lists are comma-separated):

- ``formats``: enabled formats (default ``gsd,roadmap``).
- ``planning``: the GSD tree (default ``.planning``).
- ``files``: explicit roadmap files; ``globs``: extra roadmap globs relative to the root. Without
  either, the conventional names of :data:`ROADMAP_FILES` are read when present.
- ``namespaces``: bead id prefixes anchoring tokens; ``header_options`` records ``token_mode``.
- ``first_add``: record the first-add commit time (default true); ``git``: the git executable.

``occurred_at`` is the committer time of the commit that first added the item's file (the
earliest of its plans for a GSD phase), ``None`` when git cannot say.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from beadlog import instant

from ._sources import (
    SourceError,
    csv,
    doc_title,
    first_adds,
    is_git_repo,
    split_frontmatter,
    truthy,
)
from .evidence import Artifact, Category, Ref
from .readers import ReadRequest
from .tokens import GENERIC_BEAD_TOKEN, bead_token_pattern, find_bead_tokens, token_mode

READER_NAME = "planning"
READER_VERSION = "0.1.0"
KIND = "plan"
DEFAULT_FORMATS = ("gsd", "roadmap")
ROADMAP_FILES = (
    "ROADMAP.md",
    "roadmap.md",
    "docs/ROADMAP.md",
    "docs/roadmap.md",
    "IMPLEMENTATION_PLAN.md",
    "implementation-plan.md",
    "docs/implementation-plan.md",
    "docs/IMPLEMENTATION_PLAN.md",
)

DEFAULT_TREE = ".planning"
PLAN_SUFFIX = "-PLAN.md"
SUMMARY_SUFFIX = "-SUMMARY.md"

_DEPENDS_ON = re.compile(r"^depends_on:[ ]*(.*)$")
_ITEM = re.compile(r"^[ \t]+-[ \t]")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_PHASE_NAME = re.compile(r"^(\d+)[-_](.*)$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_CHECKBOX = re.compile(r"^([ \t]*)[-*+][ \t]+\[([ xX])\][ \t]+(.*?)[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_DEPENDS = re.compile(r"[ \t]*\(depends(?: on)?:[ \t]*([^)]*)\)")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


@dataclass
class PlanItem:
    """One plan item as a format states it (before it becomes an artifact).

    ``parent`` and ``depends_on`` are item keys (``plan:<path>#<id>``); ``date_paths`` the files
    whose first-add commits date it; ``text`` the text searched for bead tokens.
    """

    path: str
    item_id: str
    title: str
    parent: str | None
    depends_on: list[str]
    done: bool
    order: int
    text: str
    date_paths: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"plan:{self.path}#{self.item_id}"


class PlanningFormat(Protocol):
    """Port: one planning layout. ``items`` reads every item of that layout under ``root``, in
    a deterministic order (parents before their children). Pure: files only."""

    name: str

    def items(self, root: Path, options: Mapping[str, Any]) -> list[PlanItem]: ...

    def pathspecs(self, root: Path, options: Mapping[str, Any]) -> list[str]:
        """Git pathspecs covering the files ``items`` reads (for the first-add lookup)."""
        ...


# -- gsd: depends_on (the deps_of port) -----------------------------------------------------


def deps_of(text: str) -> str:
    """The first ``depends_on:`` frontmatter value, normalized to a bracketed list; ``""`` when
    there is none. Exactly the awk ``deps_of`` of ``reconcile.sh``:

    - inline (``depends_on: ["10-01"]``, ``[11-01]``, ``[]``) is returned as written;
    - block style (``depends_on:`` then ``  - "01-01"`` lines) becomes ``[01-01,01-02]``, each
      item stripped to ``[A-Za-z0-9._-]``; the block ends at the first unindented line;
    - frontmatter only: the file must open with ``---`` and the scan stops at the closing one.
    """
    front, _ = split_frontmatter(text)
    items: list[str] = []
    in_block = False
    for line in front:
        line = line.rstrip("\r")
        if not in_block:
            match = _DEPENDS_ON.match(line)
            if match is None:
                continue
            value = match.group(1)
            if value != "":
                return value
            in_block = True
            continue
        if _ITEM.match(line):
            item = _UNSAFE.sub("", re.sub(r"^[ \t]+-[ \t]*", "", line))
            if item:
                items.append(item)
        elif line and not line[0].isspace():
            in_block = False
    return "[" + ",".join(items) + "]" if items else ""


def parse_depends(text: str) -> list[str]:
    """The ``depends_on`` entries of a plan as plain keys (``[]`` when it has none)."""
    value = deps_of(text).strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    keys = (_UNSAFE.sub("", part) for part in value.split(","))
    return [key for key in keys if key]


# -- gsd: the tree --------------------------------------------------------------------------


@dataclass
class Plan:
    """One ``NN-MM-PLAN.md``."""

    key: str
    path: str
    title: str
    deps: list[str]
    summary: str | None  # the sibling summary's path, when it exists


@dataclass
class Phase:
    """One ``phases/NN-<name>/`` directory and its plans."""

    name: str
    path: str
    plans: list[Plan] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return bool(self.plans) and all(plan.summary for plan in self.plans)

    @property
    def title(self) -> str:
        match = _PHASE_NAME.match(self.name)
        if not match:
            return self.name
        words = match.group(2).replace("-", " ").replace("_", " ").strip()
        return f"Phase {match.group(1)}: {words[:1].upper()}{words[1:]}" if words else self.name


def scan_tree(root: Path | str, tree: str = DEFAULT_TREE) -> list[Phase]:
    """The phases of ``<root>/<tree>/phases`` (sorted), each with its sorted plans."""
    base = Path(root)
    phases_dir = base / tree / "phases"
    if not phases_dir.is_dir():
        return []
    phases: list[Phase] = []
    for folder in sorted(p for p in phases_dir.iterdir() if p.is_dir()):
        phase = Phase(folder.name, folder.relative_to(base).as_posix())
        for plan in sorted(folder.glob(f"*{PLAN_SUFFIX}")):
            key = plan.name[: -len(PLAN_SUFFIX)]
            text = plan.read_text(encoding="utf-8", errors="replace")
            summary = folder / f"{key}{SUMMARY_SUFFIX}"
            phase.plans.append(
                Plan(
                    key,
                    plan.relative_to(base).as_posix(),
                    doc_title(text, key),
                    parse_depends(text),
                    summary.relative_to(base).as_posix() if summary.is_file() else None,
                )
            )
        phases.append(phase)
    return phases


def dep_key(raw: str) -> str:
    """The plan key a ``depends_on`` entry names (``01-01-PLAN.md``, ``01-01-PLAN``: ``01-01``)."""
    key = raw
    for suffix in (".md", PLAN_SUFFIX[: -len(".md")]):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return key


# -- gsd -------------------------------------------------------------------------------------


def _gsd_items(phases: Sequence[Phase]) -> list[PlanItem]:
    by_key: dict[str, list[tuple[Phase, Plan]]] = {}
    for phase in phases:
        for plan in phase.plans:
            by_key.setdefault(plan.key, []).append((phase, plan))

    def resolve(raw: str, phase: Phase, plan: Plan) -> Plan | None:
        found = by_key.get(dep_key(raw), [])
        chosen = [c for c in found if c[0].path == phase.path] or found
        if len(chosen) != 1 or chosen[0][1].path == plan.path:
            return None  # names nothing, several plans, or itself
        return chosen[0][1]

    out: list[PlanItem] = []
    for phase_order, phase in enumerate(phases):
        phase_key = f"plan:{phase.path}#phase"
        out.append(
            PlanItem(
                phase.path,
                "phase",
                phase.title,
                None,
                [],
                phase.closed,
                phase_order,
                phase.name,
                [plan.path for plan in phase.plans],
            )
        )
        for order, plan in enumerate(phase.plans):
            deps: list[str] = []
            unresolved: list[str] = []
            for raw in plan.deps:
                target = resolve(raw, phase, plan)
                if target is None:
                    unresolved.append(raw)
                else:
                    deps.append(f"plan:{target.path}#{target.key}")
            text = ""  # filled by GsdFormat.items, which owns the root
            out.append(
                PlanItem(
                    plan.path,
                    plan.key,
                    plan.title,
                    phase_key,
                    deps,
                    plan.summary is not None,
                    order,
                    text,
                    [plan.path],
                    unresolved,
                )
            )
    return out


class GsdFormat:
    """Adapter: a GSD ``.planning`` tree (module doc)."""

    name = "gsd"

    @staticmethod
    def _tree(options: Mapping[str, Any]) -> str:
        return str(options.get("planning", DEFAULT_TREE))

    def phases(self, root: Path, options: Mapping[str, Any]) -> list[Phase]:
        """The phases of the tree (option ``planning``), each with its plans."""
        return scan_tree(root, self._tree(options))

    def items(self, root: Path, options: Mapping[str, Any]) -> list[PlanItem]:
        items = _gsd_items(self.phases(root, options))
        for item in items:
            if item.item_id != "phase":
                item.text = (root / item.path).read_text(encoding="utf-8", errors="replace")
        return items

    def pathspecs(self, root: Path, options: Mapping[str, Any]) -> list[str]:
        return [self._tree(options)]


# -- roadmap ---------------------------------------------------------------------------------


def slugify(text: str) -> str:
    """A stable item id from text: lowercase alphanumerics joined by ``-`` (``item`` if empty)."""
    return _SLUG_STRIP.sub("-", text.lower()).strip("-") or "item"


@dataclass
class _Node:
    kind: str  # heading | checkbox
    level: int  # heading level, or checkbox indent width
    title: str
    done: bool
    deps: list[str]
    text: str
    children: list[_Node] = field(default_factory=list)
    item_id: str = ""
    slug: str = ""


def _parse_roadmap(text: str) -> list[_Node]:
    """The forest of headings and checkboxes of one markdown file, in document order."""
    roots: list[_Node] = []
    stack: list[_Node] = []
    in_fence = False

    def attach(node: _Node) -> None:
        while stack and not _encloses(stack[-1], node):
            stack.pop()
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING.match(line)
        if heading:
            title, deps = _split_deps(heading.group(2).strip())
            attach(_Node("heading", len(heading.group(1)), title, False, deps, line.strip()))
            continue
        box = _CHECKBOX.match(line)
        if box:
            title, deps = _split_deps(box.group(3))
            indent = len(box.group(1).expandtabs(4))
            attach(_Node("checkbox", indent, title, box.group(2) != " ", deps, line.strip()))
    return roots


def _encloses(outer: _Node, inner: _Node) -> bool:
    if outer.kind == "heading":
        return inner.kind == "checkbox" or inner.level > outer.level
    return inner.kind == "checkbox" and inner.level > outer.level


def _split_deps(text: str) -> tuple[str, list[str]]:
    deps: list[str] = []

    def take(match: re.Match[str]) -> str:
        deps.extend(slugify(part) for part in match.group(1).split(",") if part.strip())
        return ""

    return _DEPENDS.sub(take, text).strip(), deps


def _done(node: _Node) -> bool:
    if node.kind == "checkbox":
        return node.done
    boxes = list(_checkboxes(node))
    return bool(boxes) and all(box.done for box in boxes)


def _checkboxes(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        if child.kind == "checkbox":
            yield child
        yield from _checkboxes(child)


def _drop_document_title(roots: list[_Node]) -> list[_Node]:
    """A single ``#`` heading is the file's title, not an item: its children become roots."""
    titles = [n for n in roots if n.kind == "heading" and n.level == 1]
    if len(titles) != 1:
        return roots
    out: list[_Node] = []
    for node in roots:
        out.extend(node.children if node is titles[0] else [node])
    return out


def roadmap_items(
    path: str, text: str, pattern: re.Pattern[str] = GENERIC_BEAD_TOKEN
) -> list[PlanItem]:
    """The items of one roadmap file (``path`` is its repo-relative posix path).

    The item id is the chain of ancestor slugs and the item's own: ``phase-1/setup-repo``. A slug
    is made from the title without bead tokens, the ``(depends: ...)`` clause, the checkbox state
    and punctuation, so editing those never changes the key; ``-2``, ``-3`` only separate true
    duplicates under one parent.
    """
    roots = _drop_document_title(_parse_roadmap(text))
    flat: list[tuple[_Node, str | None, int]] = []

    def walk(nodes: Sequence[_Node], parent: _Node | None) -> None:
        seen: dict[str, int] = {}
        for order, node in enumerate(nodes):
            base = slugify(pattern.sub(" ", node.title))
            seen[base] = seen.get(base, 0) + 1
            leaf = base if seen[base] == 1 else f"{base}-{seen[base]}"
            node.slug = leaf
            node.item_id = f"{parent.item_id}/{leaf}" if parent else leaf
            flat.append((node, f"plan:{path}#{parent.item_id}" if parent else None, order))
            walk(node.children, node)

    walk(roots, None)
    ids = {node.item_id for node, _, _ in flat}
    by_slug: dict[str, list[str]] = {}
    for node, _, _ in flat:
        by_slug.setdefault(node.slug, []).append(node.item_id)
    out: list[PlanItem] = []
    for node, parent, order in flat:
        deps: list[str] = []
        unresolved: list[str] = []
        sibling = node.item_id.rpartition("/")[0]
        for raw in node.deps:
            found = by_slug.get(raw, [])
            near = [i for i in found if i.rpartition("/")[0] == sibling]
            chosen = near or found
            target = raw if raw in ids else (chosen[0] if len(chosen) == 1 else None)
            if target is None or target == node.item_id:
                unresolved.append(raw)
            else:
                deps.append(f"plan:{path}#{target}")
        out.append(
            PlanItem(
                path, node.item_id, node.title, parent, deps, _done(node), order,
                node.text, [path], unresolved,
            )
        )  # fmt: skip
    return out


def _distinct_files(root: Path, names: tuple[str, ...]) -> list[str]:
    """Existing ``names`` under ``root``, one per real file (first spelling wins).

    On a case-insensitive filesystem ``ROADMAP.md`` and ``roadmap.md`` are one file;
    de-duplicate by identity (samefile), not by spelling.
    """
    kept: list[str] = []
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        if any(path.samefile(root / seen) for seen in kept):
            continue
        kept.append(name)
    return kept


class RoadmapFormat:
    """Adapter: headed checklists and implementation-plan markdown (module doc)."""

    name = "roadmap"

    @staticmethod
    def discover(root: Path, options: Mapping[str, Any]) -> list[str]:
        found: set[str] = set()
        explicit = csv(options.get("files", ""))
        globs = csv(options.get("globs", ""))
        for name in explicit:
            if not (root / name).is_file():
                raise SourceError(f"planning file {name!r} does not exist under {root}")
            found.add((root / name).relative_to(root).as_posix())
        for pattern in globs:
            found.update(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file())
        if not explicit and not globs:
            found.update(_distinct_files(root, ROADMAP_FILES))
        return sorted(found)

    def items(self, root: Path, options: Mapping[str, Any]) -> list[PlanItem]:
        pattern = bead_token_pattern(csv(options.get("namespaces", "")))
        out: list[PlanItem] = []
        for path in self.discover(root, options):
            text = (root / path).read_text(encoding="utf-8", errors="replace")
            out.extend(roadmap_items(path, text, pattern))
        return out

    def pathspecs(self, root: Path, options: Mapping[str, Any]) -> list[str]:
        return self.discover(root, options)


BUILTIN_FORMATS: Mapping[str, Callable[[], PlanningFormat]] = {
    "gsd": GsdFormat,
    "roadmap": RoadmapFormat,
}


def _formats(names: Iterable[str]) -> tuple[PlanningFormat, ...]:
    out: list[PlanningFormat] = []
    for name in names:
        factory = BUILTIN_FORMATS.get(name)
        if factory is None:
            raise SourceError(
                f"unknown planning format {name!r} (known: {', '.join(BUILTIN_FORMATS)})"
            )
        out.append(factory())
    return tuple(out)


# -- the reader ------------------------------------------------------------------------------


class PlanningReader:
    """Reads plan trees and checklists into ``plan-item`` artifacts."""

    name = READER_NAME
    version = READER_VERSION
    categories = (Category.PLAN_ITEM,)

    @staticmethod
    def header_options(options: Mapping[str, Any]) -> dict[str, Any]:
        """The options recorded in the run's ReaderHeader: the given ones plus ``token_mode``."""
        return {**options, "token_mode": token_mode(csv(options.get("namespaces", "")))}

    def read(self, request: ReadRequest) -> Iterable[Artifact]:
        options = request.options
        root = Path(request.root)
        formats = _formats(csv(options.get("formats", ",".join(DEFAULT_FORMATS))))
        pattern = bead_token_pattern(csv(options.get("namespaces", "")))
        git = str(options.get("git", "git"))
        batches = [(fmt, fmt.items(root, options)) for fmt in formats]
        adds: dict[str, Any] = {}
        if any(items for _, items in batches) and truthy(options.get("first_add", "true")):
            if is_git_repo(root, git):
                specs = sorted(
                    {s for fmt, items in batches if items for s in fmt.pathspecs(root, options)}
                )
                adds = first_adds(root, specs, git)
        seen: set[str] = set()
        for fmt, items in batches:
            for item in items:
                if item.key in seen:
                    continue
                seen.add(item.key)
                yield self.artifact(fmt.name, item, pattern, adds)

    def artifact(
        self, format_name: str, item: PlanItem, pattern: re.Pattern[str], adds: Mapping[str, Any]
    ) -> Artifact:
        records = [adds[p] for p in item.date_paths if p in adds]
        occurred_at = min((r.when for r in records if r.when), key=instant, default=None)
        raw_attrs: dict[str, Any] = {"format": format_name, "item_id": item.item_id}
        if item.unresolved:
            raw_attrs["unresolved_depends_on"] = list(item.unresolved)
        if records:
            oldest = min(records, key=lambda r: instant(r.when) if r.when else 0)
            raw_attrs["first_add"] = {"sha": oldest.sha, "date": oldest.date}
        refs = [Ref("bead-token", token, "body") for token in find_bead_tokens(item.text, pattern)]
        return Artifact(
            category=Category.PLAN_ITEM,
            kind=KIND,
            key=item.key,
            title=item.title,
            occurred_at=occurred_at,
            attrs={
                "parent": item.parent,
                "depends_on": list(item.depends_on),
                "done_marker": item.done,
                "order": item.order,
            },
            raw_attrs=raw_attrs,
            refs=tuple(refs),
            reader=self.name,
            reader_version=self.version,
        )


__all__ = [
    "BUILTIN_FORMATS",
    "DEFAULT_TREE",
    "PLAN_SUFFIX",
    "ROADMAP_FILES",
    "SUMMARY_SUFFIX",
    "GsdFormat",
    "Phase",
    "Plan",
    "PlanItem",
    "PlanningFormat",
    "PlanningReader",
    "RoadmapFormat",
    "dep_key",
    "deps_of",
    "parse_depends",
    "roadmap_items",
    "scan_tree",
    "slugify",
]
