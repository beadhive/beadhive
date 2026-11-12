"""The neutral ``planning`` strategy: a GSD ``.planning`` tree -> epics, issues and edges.

BeadHive-free (``docs/design/backfill-platform-adr.md`` section 2). It ports the planning mode of
the ``bh:backfill`` skill's ``reconcile.sh`` and builds the bulk emitter that skill's apply step
lacked, so a GSD-tracked repository imports in one reviewed plan instead of dozens of hand
creates. The mapping:

- ``.planning/phases/NN-<name>/`` -> an **epic** ``create``;
- ``NN-MM-PLAN.md`` -> an **issue** ``create`` with a ``parent-child`` edge to its phase epic;
- the plan's frontmatter ``depends_on`` (inline ``[..]`` and block YAML list) -> ``blocks`` edges
  to the plans it names (``deps_of`` is the awk function of the script, ported with its self-test);

The tree is read through the ``planning`` reader's GSD adapter
(:meth:`beadlog_sources.planning_reader.GsdFormat.phases`), which owns the scanning and the
``depends_on`` parsing; this module keeps the strategy: bead identity, events and the report.
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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beadlog import BeadEvent, BeadRef, Edge, Evidence, fold, instant, mint_event

from ._sources import (
    AddRecord,
    SourceError,
    csv,
    external_refs,
    first_adds,
    follow_add,
    is_git_repo,
    live_beads,
    mint_bead_id,
    truthy,
)
from .commits import require_full_history
from .contracts import ExtractRequest
from .planning_reader import (
    DEFAULT_TREE,
    GsdFormat,
    Phase,
    Plan,
    dep_key,
    deps_of,
    parse_depends,
    scan_tree,
)

STRATEGY_NAME = "planning"
STRATEGY_VERSION = "0.1.0"
SOURCE_SYSTEM = "planning"
ANNOTATION_KEY = "bdlg.planning"
ORIGIN_LABELS = ("origin:backfill", "source:planning")
CLOSED = "closed"


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
    phases = GsdFormat().phases(root, {"planning": tree}) if phases is None else list(phases)
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
    key = dep_key(raw)
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
