"""The neutral ``planning`` strategy: a GSD ``.planning`` tree -> epics, issues and edges.

BeadHive-free (``docs/design/backfill-platform-adr.md`` section 2). It ports the planning mode of
the ``bh:backfill`` skill's ``reconcile.sh`` and builds the bulk emitter that skill's apply step
lacked, so a GSD-tracked repository imports in one reviewed plan instead of dozens of hand
creates. The mapping:

- ``.planning/phases/NN-<name>/`` -> an **epic** ``create``;
- ``NN-MM-PLAN.md`` -> an **issue** ``create`` with a ``parent-child`` edge to its phase epic;
- the plan's frontmatter ``depends_on`` (inline ``[..]`` and block YAML list) -> ``blocks`` edges
  to the plans it names (``deps_of`` is the awk function of the script, ported with its self-test);
- a plan is **closed** (a ``phase`` event) when its sibling ``NN-MM-SUMMARY.md`` exists, and a
  phase epic is closed when it has plans and every one of them is;
- ``occurred_at`` is the committer date of the commit that first added the file (the plan for a
  create, the summary for a close), from ONE ``git log``; undated when the tree is not in git;
- ``external_ref`` is the doc path (``.planning/phases/01-x/01-01-PLAN.md``; the epic's is its
  directory), which is the idempotency key.

**Created beads** carry ``origin:backfill`` and ``source:planning`` (and any option ``labels``,
e.g. the hive identity triplet). Their ids are minted deterministically from ``(group, path)``
under the explicit option ``id_prefix`` (required whenever a doc would mint a bead; never derived).

**Idempotent.** A doc whose path is already some baseline bead's ``external_ref`` is PRESENT and
proposes nothing, except one thing: when it is not closed yet and its summary now exists, a
closing ``phase`` event. Applying the events and extracting again proposes zero events. Edges and
labels of a bead that already exists are never touched (it may be hand-made); a ``depends_on``
entry naming no plan, or several, is reported (:attr:`PlanningReport.unresolved`) and skipped.

Options (``ExtractRequest.options``): ``planning`` (the tree, default ``.planning``),
``id_prefix``, ``labels`` (extra labels for created beads), ``issue_type`` (default ``task``),
``allow_shallow`` and ``git``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beadlog import BeadEvent, BeadRef, Edge, Evidence, fold, instant, mint_event

from ._sources import (
    AddRecord,
    SourceError,
    csv,
    doc_title,
    external_refs,
    first_adds,
    follow_add,
    is_git_repo,
    live_beads,
    mint_bead_id,
    split_frontmatter,
    truthy,
)
from .commits import require_full_history
from .contracts import ExtractRequest

STRATEGY_NAME = "planning"
STRATEGY_VERSION = "0.1.0"
SOURCE_SYSTEM = "planning"
ANNOTATION_KEY = "bdlg.planning"
DEFAULT_TREE = ".planning"
ORIGIN_LABELS = ("origin:backfill", "source:planning")
PLAN_SUFFIX = "-PLAN.md"
SUMMARY_SUFFIX = "-SUMMARY.md"
CLOSED = "closed"
_DEPENDS = re.compile(r"^depends_on:[ ]*(.*)$")
_ITEM = re.compile(r"^[ \t]+-[ \t]")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_PHASE_NAME = re.compile(r"^(\d+)[-_](.*)$")


# -- depends_on (the deps_of port) ---------------------------------------------------------


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
            match = _DEPENDS.match(line)
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


# -- the tree ------------------------------------------------------------------------------


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


# -- the report ----------------------------------------------------------------------------


@dataclass
class PlanningEntry:
    """What happened to one phase or plan."""

    path: str
    kind: str  # epic | issue
    status: str  # NEW | PRESENT
    bead: str
    events: int = 0
    closed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "class": self.status,
            "bead": self.bead,
            "events": self.events,
            "closed": self.closed,
        }


@dataclass
class PlanningReport:
    """What one extraction saw, for the operator and the archive annotation."""

    tree: str
    entries: list[PlanningEntry] = field(default_factory=list)
    unresolved: list[dict[str, str]] = field(default_factory=list)
    edges: int = 0

    def counts(self) -> dict[str, int]:
        out = {"phases": 0, "plans": 0, "new": 0, "present": 0, "closed": 0}
        for entry in self.entries:
            out["phases" if entry.kind == "epic" else "plans"] += 1
            out["new" if entry.status == "NEW" else "present"] += 1
            out["closed"] += entry.closed
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "tree": self.tree,
            "counts": self.counts(),
            "proposed": sum(entry.events for entry in self.entries),
            "edges": self.edges,
            "unresolved": list(self.unresolved),
            "drift": [
                {"bead": item["plan"], "reason": f"depends_on {item['dep']!r}: {item['reason']}"}
                for item in self.unresolved
            ],
            "entries": [entry.to_json() for entry in self.entries],
        }


@dataclass(frozen=True)
class PlanningProposal:
    events: tuple[BeadEvent, ...]
    report: PlanningReport


# -- the strategy --------------------------------------------------------------------------


def _dep_key(raw: str) -> str:
    key = raw
    for suffix in (".md", PLAN_SUFFIX[: -len(".md")]):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return key


def propose(
    request: ExtractRequest,
    phases: Sequence[Phase] | None = None,
    *,
    tree: str = DEFAULT_TREE,
    git: str = "git",
) -> PlanningProposal:
    """The events creating the beads of ``phases`` (default: scanned from ``request.root``) that
    the baseline does not hold yet (module doc)."""
    root = Path(request.root)
    phases = scan_tree(root, tree) if phases is None else list(phases)
    live = live_beads(fold(request.baseline), request.group)
    refs = external_refs(live)
    repo = is_git_repo(root, git)
    adds = first_adds(root, [tree], git) if repo else {}
    prefix = str(request.options.get("id_prefix") or "") or None
    plan_type = str(request.options.get("issue_type", "task"))
    labels = tuple(dict.fromkeys([*ORIGIN_LABELS, *csv(request.options.get("labels", ""))]))
    report = PlanningReport(tree)
    taken = set(live)
    events: list[BeadEvent] = []

    def record(path: str) -> AddRecord | None:
        found = adds.get(path)
        if found is None and repo:
            found = follow_add(root, path, git)  # a renamed file: only these cost a git call
            if found is not None:
                adds[path] = found
        return found

    def add_when(path: str) -> str | None:
        found = record(path)
        return found.when if found else None

    def ref_of(path: str) -> tuple[BeadRef, bool]:
        """The bead for a doc path and whether it already exists in the baseline."""
        known = refs.get(path)
        if known:
            return BeadRef(request.group, known[0]), True
        if prefix is None:
            raise SourceError(
                "cannot mint a bead id without an explicit id prefix (prefixes are never derived "
                "or guessed); pass option id_prefix"
            )
        ident = mint_bead_id(prefix, request.group, f"planning:{path}", taken)
        taken.add(ident)
        return BeadRef(request.group, ident), False

    def mint(
        kind: str, bead: BeadRef, data: Mapping[str, Any], path: str, ref: str, when: str | None
    ) -> BeadEvent:
        return mint_event(
            kind,
            bead,
            data,
            occurred_at=when,
            recorded_at=None,
            extracted_at=request.extracted_at,
            source=(SOURCE_SYSTEM, f"{path}#{ref}"),
            provenance="derived",
            evidence=[Evidence("file", path)],
        )

    def closure(
        bead: BeadRef, exists: bool, path: str, why: str, when: str | None
    ) -> list[BeadEvent]:
        if exists and live[bead.id].phase in (CLOSED, "retired"):
            return []
        return [mint("phase", bead, {"phase": CLOSED, "reason": why}, path, "phase", when)]

    # first pass: a bead for every phase and plan, so depends_on can point at any of them
    plan_ref: dict[str, tuple[BeadRef, bool]] = {}
    epic_ref: dict[str, tuple[BeadRef, bool]] = {}
    by_key: dict[str, list[tuple[Phase, Plan]]] = {}
    for phase in phases:
        epic_ref[phase.path] = ref_of(phase.path)
        for plan in phase.plans:
            plan_ref[plan.path] = ref_of(plan.path)
            by_key.setdefault(plan.key, []).append((phase, plan))

    for phase in phases:
        bead, exists = epic_ref[phase.path]
        made: list[BeadEvent] = []
        dates = [w for p in phase.plans if (w := add_when(p.path)) is not None]
        if not dates:  # a phase without plans: its earliest tracked file
            dates = [
                w
                for name, found in adds.items()
                if name.startswith(phase.path + "/") and (w := found.when) is not None
            ]
        created = min(dates, key=instant) if dates else None
        if not exists:
            made.append(
                mint(
                    "create",
                    bead,
                    {
                        "type": "epic",
                        "fields": {
                            "title": phase.title,
                            "external_ref": phase.path,
                            "description": f"Reconstructed from {phase.path}.",
                        },
                    },
                    phase.path,
                    "create",
                    created,
                )
            )
            made.extend(
                mint("label_add", bead, {"label": label}, phase.path, f"label/{label}", created)
                for label in labels
            )
        summaries = [w for p in phase.plans if p.summary and (w := add_when(p.summary)) is not None]
        done = max(summaries, key=instant) if summaries else None
        if phase.closed:
            made.extend(closure(bead, exists, phase.path, "every plan has a summary", done))
        events.extend(made)
        report.entries.append(
            PlanningEntry(
                phase.path, "epic", "PRESENT" if exists else "NEW", bead.id, len(made), phase.closed
            )
        )

        for plan in phase.plans:
            bead_, exists_ = plan_ref[plan.path]
            plan_events: list[BeadEvent] = []
            when = add_when(plan.path)
            if not exists_:
                plan_events.append(
                    mint(
                        "create",
                        bead_,
                        {
                            "type": plan_type,
                            "fields": {
                                "title": plan.title,
                                "external_ref": plan.path,
                                "description": f"Reconstructed from {plan.path}.",
                            },
                        },
                        plan.path,
                        "create",
                        when,
                    )
                )
                plan_events.extend(
                    mint("label_add", bead_, {"label": label}, plan.path, f"label/{label}", when)
                    for label in labels
                )
                parent = Edge("parent-child", epic_ref[phase.path][0])
                plan_events.append(
                    mint("edge_add", bead_, parent.to_json(), plan.path, "edge/parent-child", when)
                )
                report.edges += 1
                for dep in plan.deps:
                    target = _resolve_dep(dep, phase, plan, by_key, report)
                    if target is None:
                        continue
                    blocks = Edge("blocks", plan_ref[target.path][0])
                    plan_events.append(
                        mint(
                            "edge_add",
                            bead_,
                            blocks.to_json(),
                            plan.path,
                            f"edge/blocks/{target.path}",
                            when,
                        )
                    )
                    report.edges += 1
            if plan.summary:
                plan_events.extend(
                    closure(
                        bead_,
                        exists_,
                        plan.path,
                        f"{plan.summary} exists",
                        add_when(plan.summary),
                    )
                )
            events.extend(plan_events)
            report.entries.append(
                PlanningEntry(
                    plan.path,
                    "issue",
                    "PRESENT" if exists_ else "NEW",
                    bead_.id,
                    len(plan_events),
                    plan.summary is not None,
                )
            )
    return PlanningProposal(tuple(events), report)


def _resolve_dep(
    raw: str,
    phase: Phase,
    plan: Plan,
    by_key: Mapping[str, list[tuple[Phase, Plan]]],
    report: PlanningReport,
) -> Plan | None:
    """The plan a ``depends_on`` entry names: its key (``01-01``), preferring the same phase."""
    key = _dep_key(raw)
    found = by_key.get(key, [])
    if not found:
        report.unresolved.append(
            {"plan": plan.path, "dep": raw, "reason": "names no plan of the tree"}
        )
        return None
    same = [candidate for candidate in found if candidate[0].path == phase.path]
    chosen = same or found
    if len(chosen) > 1:
        report.unresolved.append({"plan": plan.path, "dep": raw, "reason": "names several plans"})
        return None
    target = chosen[0][1]
    if target.path == plan.path:
        report.unresolved.append({"plan": plan.path, "dep": raw, "reason": "names itself"})
        return None
    return target


class PlanningStrategy:
    """Source strategy ``planning``: the GSD ``.planning`` tree of ``request.root`` as epics,
    issues and edges (module doc). ``report()`` describes the last extraction (the ``beadlog
    extract`` CLI stores it as the archive annotation)."""

    name = STRATEGY_NAME
    version = STRATEGY_VERSION

    def __init__(self) -> None:
        self._report: PlanningReport | None = None

    def propose(self, request: ExtractRequest) -> PlanningProposal:
        options = request.options
        binary = str(options.get("git", "git"))
        if is_git_repo(request.root, binary) and not truthy(options.get("allow_shallow", "false")):
            require_full_history(request.root, binary)
        tree = str(options.get("planning", DEFAULT_TREE))
        proposal = propose(request, tree=tree, git=binary)
        self._report = proposal.report
        return proposal

    def extract(self, request: ExtractRequest) -> tuple[BeadEvent, ...]:
        return self.propose(request).events

    def report(self) -> Mapping[str, Any] | None:
        """The last extraction's report as JSON (``None`` before the first extraction)."""
        return None if self._report is None else self._report.to_json()


__all__ = [
    "ANNOTATION_KEY",
    "DEFAULT_TREE",
    "ORIGIN_LABELS",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "Phase",
    "Plan",
    "PlanningEntry",
    "PlanningProposal",
    "PlanningReport",
    "PlanningStrategy",
    "deps_of",
    "parse_depends",
    "propose",
    "scan_tree",
]
