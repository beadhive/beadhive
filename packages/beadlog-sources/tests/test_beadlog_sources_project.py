"""beadlog-sources: Phase C, projecting collated work units to proposed BeadLog events.

A golden per record category (the events, their provenance and evidence), the idempotency round
trip (project, reconcile, apply to an in-memory store, collate and project again: zero events),
matched units that never overwrite, the explicit prefix, and determinism.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import pytest

import beadlog
from beadlog.testing import InMemoryStore
from beadlog_sources import (
    PROJECT_EVENT_KINDS,
    ClusterOptions,
    Collation,
    EvidenceCorpus,
    IdentifyOptions,
    ProjectError,
    Projection,
    ProjectOptions,
    collate,
    dumps_collation,
    loads_collation,
    project,
    reconcile,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000aa"
PREFIX = "bh"
EXTRACTED = "2026-11-12T00:00:00Z"
DIRECTORY = {"hq": ["hq-9"]}


def sha(n: int) -> str:
    return f"{n:040x}"


def ckey(n: int) -> str:
    return f"git:commit/{sha(n)}"


def commit(n: int, when: str, *, cc_type="feat", paths=("src/a.py",), parents=(), **kw):
    return ev.change(
        sha(n),
        occurred_at=when,
        committed_at=when,
        parents=list(parents),
        cc_type=cc_type,
        paths=list(paths),
        **kw,
    )


def existing(ident: str, title: str, **fields: Any) -> beadlog.BeadEvent:
    return beadlog.mint_event(
        "create",
        beadlog.BeadRef(GROUP, ident),
        {"type": "task", "fields": {"title": title, **fields}},
        occurred_at="2025-06-01T00:00:00Z",
        recorded_at="2025-06-01T00:00:00Z",
        extracted_at=EXTRACTED,
        source=("bd", f"bd:create/{ident}"),
        provenance="observed",
    )


def run_collate(corpus: EvidenceCorpus, baseline: Sequence[beadlog.BeadEvent]) -> Collation:
    return collate(
        corpus,
        baseline,
        InMemoryLedgerDirectory(DIRECTORY),
        local="bh",
        options=ClusterOptions(),
        identify_options=IdentifyOptions(new_threshold=100),
    )


def run(
    *artifacts, baseline: Sequence[beadlog.BeadEvent] = (), options: ProjectOptions | None = None
) -> tuple[EvidenceCorpus, Collation, Projection]:
    corpus = ev.corpus(*artifacts)
    col = run_collate(corpus, baseline)
    return (
        corpus,
        col,
        project(
            col,
            corpus,
            baseline,
            prefix=PREFIX,
            group=GROUP,
            extracted_at=EXTRACTED,
            options=options,
        ),
    )


_SHA = re.compile(r"\b0{30,}([0-9a-f]+)\b")


def render(projection: Projection) -> list[tuple[Any, ...]]:
    """Events as golden rows ``(bead, kind, data, occurred_at, precision, provenance, confidence,
    evidence)``, sorted; a minted bead id is shown as its unit key and a sha as ``c<n>``."""
    alias = {u.bead: u.unit for u in projection.units if u.bead and u.decision == "NEW"}

    def name(ident: str) -> str:
        return alias.get(ident, ident)

    rows = []
    for e in projection.events:
        data = dict(e.to_json()["data"])
        if "target" in data:
            data["target"] = name(data["target"]["id"])
        if e.kind == "create":
            data = {"type": data["type"], **data["fields"]}
        if isinstance(data.get("value"), str) and data["value"].startswith("["):
            data["value"] = json.loads(data["value"])  # a JSON-array string, shown parsed
        row = (
            name(e.bead.id),
            str(e.kind),
            data,
            e.occurred_at,
            None if e.occurred_precision is None else str(e.occurred_precision),
            str(e.provenance),
            e.confidence,
            [x.ref for x in e.source.evidence],
        )
        rows.append(tuple(json.loads(_SHA.sub(r"c\1", json.dumps(list(row), sort_keys=True)))))
    return sorted(rows, key=lambda r: (r[0], r[1], json.dumps(r[2], sort_keys=True)))


def show(projection: Projection) -> None:
    for row in render(projection):
        print(repr(row) + ",")


# -- the scenarios, one per record category ---------------------------------------------------


def change_scenario() -> dict[str, Any]:
    """A feat commit (NEW) and a fix naming local bead bh-1 (PRESENT, attach)."""
    return {
        "artifacts": (
            commit(1, "2026-01-05T10:00:00Z", title="feat: archive writer"),
            commit(
                2,
                "2026-01-06T09:00:00Z",
                cc_type="fix",
                title="fix: ledger port (bh-1)",
                refs=[ev.ref("bh-1")],
            ),
        ),
        "baseline": (existing("bh-1", "Ledger directory port"),),
    }


def review_scenario() -> dict[str, Any]:
    """A merged PR with two commits."""
    return {
        "artifacts": (
            ev.review(
                5,
                title="Add parser",
                occurred_at="2026-01-02T08:00:00Z",
                opened_at="2026-01-02T08:00:00Z",
                merged_at="2026-01-04T12:00:00Z",
                commits=[sha(1), sha(2)],
            ),
            commit(1, "2026-01-02T09:00:00Z", title="feat(parser): tokens"),
            commit(2, "2026-01-03T09:00:00Z", title="feat(parser): grammar"),
        ),
        "baseline": (),
    }


def tracker_scenario() -> dict[str, Any]:
    """A commit naming a foreign-ledger bead (hq-9) and closing tracker issue #7."""
    return {
        "artifacts": (
            commit(
                1,
                "2026-01-05T10:00:00Z",
                title="feat: sync importer",
                refs=[ev.ref("hq-9", where="body"), ev.ref("#7", kind="issue", where="body")],
            ),
            ev.tracker_item(
                "7",
                title="Importer crashes",
                state="closed",
                opened_at="2026-01-01T00:00:00Z",
                closed_at="2026-01-06T00:00:00Z",
            ),
        ),
        "baseline": (),
    }


def design_scenario() -> dict[str, Any]:
    """A standalone doc dated by day only, and a draft doc added with code (a conflict)."""
    return {
        "artifacts": (
            ev.design_record(
                "docs/design/queue.md",
                title="Queue design",
                declared_status="accepted",
                declared_date="2026-02-03",
            ),
            ev.design_record(
                "docs/design/cache.md",
                title="Cache design",
                declared_status="draft",
                raw_attrs={"declared_status": "Draft"},
            ),
            commit(
                1,
                "2026-01-05T10:00:00Z",
                title="feat(cache): cache layer",
                paths=("src/cache.py", "docs/design/cache.md"),
            ),
        ),
        "baseline": (),
    }


def plan_scenario() -> dict[str, Any]:
    """A phase epic with a done item and an open item that depends on it."""
    return {
        "artifacts": (
            ev.plan_item("phase-1", title="Phase 1: storage", occurred_at="2026-03-01T00:00:00Z"),
            ev.plan_item(
                "phase-1/a",
                title="Write the log",
                parent="phase-1",
                order=1,
                done_marker=True,
                occurred_at="2026-03-02T00:00:00Z",
            ),
            ev.plan_item(
                "phase-1/b",
                title="Read the log",
                parent="phase-1",
                depends_on=["phase-1/a"],
                order=2,
                occurred_at="2026-03-03T00:00:00Z",
            ),
        ),
        "baseline": (),
    }


def release_scenario() -> dict[str, Any]:
    """Two unscoped top-level feat commits grouped into their release window's epic."""
    return {
        "artifacts": (
            commit(1, "2026-04-01T10:00:00Z", title="feat: first", paths=("README",)),
            commit(
                2, "2026-04-02T10:00:00Z", title="feat: second", paths=("NOTES",), parents=[sha(1)]
            ),
            ev.release_marker(
                "v1.0",
                occurred_at="2026-04-03T00:00:00Z",
                released_at="2026-04-03T00:00:00Z",
                commit_range=sha(2),
            ),
        ),
        "baseline": (),
    }


SCENARIOS = {
    "change": change_scenario,
    "review": review_scenario,
    "tracker-item": tracker_scenario,
    "design-record": design_scenario,
    "plan-item": plan_scenario,
    "release-marker": release_scenario,
}


def scenario(name: str, options: ProjectOptions | None = None):
    built = SCENARIOS[name]()
    return run(*built["artifacts"], baseline=built["baseline"], options=options), built["baseline"]


# -- goldens: (bead, kind, data, occurred_at, precision, provenance, confidence, evidence) ----

GOLDEN: dict[str, list[tuple[Any, ...]]] = {
    "change": [
        (
            "bh-1",
            "set",
            {"field": "metadata.git.commits", "value": ["c2"]},
            "2026-01-06T09:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c2"],
        ),
        (
            "git:commit/c1",
            "create",
            {
                "description": "Reconstructed from git:commit/c1.",
                "external_ref": "git:commit/c1",
                "title": "feat: archive writer",
                "type": "feature",
            },
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "label_add",
            {"label": "source:commit"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "phase",
            {"phase": "closed", "reason": "committed: git:commit/c1"},
            "2026-01-05T10:00:00.000001Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.git.commits", "value": ["c1"]},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.lifecycle.closed_at", "value": "2026-01-05T10:00:00Z"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-01-05T10:00:00Z"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
    ],
    "review": [
        (
            "gh:pr/5",
            "create",
            {
                "description": "Reconstructed from gh:pr/5.",
                "external_ref": "gh:pr/5",
                "title": "Add parser",
                "type": "feature",
            },
            "2026-01-02T08:00:00Z",
            None,
            "derived",
            None,
            ["gh:pr/5", "git:commit/c1", "git:commit/c2"],
        ),
        (
            "gh:pr/5",
            "label_add",
            {"label": "source:github"},
            "2026-01-02T08:00:00Z",
            None,
            "derived",
            None,
            ["gh:pr/5"],
        ),
        (
            "gh:pr/5",
            "phase",
            {"phase": "closed", "reason": "merged: gh:pr/5"},
            "2026-01-04T12:00:00Z",
            None,
            "observed",
            None,
            ["gh:pr/5"],
        ),
        (
            "gh:pr/5",
            "set",
            {"field": "metadata.git.commits", "value": ["c1", "c2"]},
            "2026-01-03T09:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1", "git:commit/c2"],
        ),
        (
            "gh:pr/5",
            "set",
            {"field": "metadata.lifecycle.closed_at", "value": "2026-01-04T12:00:00Z"},
            "2026-01-04T12:00:00Z",
            None,
            "derived",
            None,
            ["gh:pr/5"],
        ),
        (
            "gh:pr/5",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-01-02T08:00:00Z"},
            "2026-01-02T08:00:00Z",
            None,
            "derived",
            None,
            ["gh:pr/5"],
        ),
    ],
    "tracker-item": [
        (
            "git:commit/c1",
            "create",
            {
                "description": "Reconstructed from git:commit/c1.",
                "external_ref": "git:commit/c1",
                "title": "feat: sync importer",
                "type": "feature",
            },
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "label_add",
            {"label": "source:commit"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "phase",
            {"phase": "closed", "reason": "committed: git:commit/c1"},
            "2026-01-05T10:00:00.000001Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.git.commits", "value": ["c1"]},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.lifecycle.closed_at", "value": "2026-01-05T10:00:00Z"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-01-05T10:00:00Z"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.xref.hq", "value": ["hq-9"]},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.xref.tracker", "value": ["gh:issue/7"]},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1", "gh:issue/7"],
        ),
    ],
    "design-record": [
        (
            "doc:docs/design/queue.md",
            "create",
            {
                "description": "Reconstructed from doc:docs/design/queue.md.",
                "external_ref": "doc:docs/design/queue.md",
                "title": "Queue design",
                "type": "decision",
            },
            "2026-02-03T00:00:00Z",
            "day",
            "derived",
            None,
            ["doc:docs/design/queue.md"],
        ),
        (
            "doc:docs/design/queue.md",
            "label_add",
            {"label": "source:docs"},
            "2026-02-03T00:00:00Z",
            "day",
            "derived",
            None,
            ["doc:docs/design/queue.md"],
        ),
        (
            "doc:docs/design/queue.md",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-02-03"},
            "2026-02-03T00:00:00Z",
            "day",
            "derived",
            None,
            ["doc:docs/design/queue.md"],
        ),
        (
            "git:commit/c1",
            "create",
            {
                "description": "Reconstructed from git:commit/c1.",
                "external_ref": "git:commit/c1",
                "title": "feat(cache): cache layer",
                "type": "feature",
            },
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1", "doc:docs/design/cache.md"],
        ),
        (
            "git:commit/c1",
            "label_add",
            {"label": "source:commit"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "phase",
            {"phase": "closed", "reason": "committed: git:commit/c1"},
            "2026-01-05T10:00:00.000001Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {
                "field": "metadata.backfill.conflict",
                "value": [
                    {
                        "detail": "doc says draft but code landed in the same change or PR",
                        "rule": "doc-draft-code-landed",
                        "sources": [
                            {
                                "artifact": "doc:docs/design/cache.md",
                                "field": "declared_status",
                                "value": "draft",
                            },
                            {"artifact": "git:commit/c1", "field": "landed", "value": "landed"},
                        ],
                    }
                ],
            },
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["doc:docs/design/cache.md", "git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.git.commits", "value": ["c1"]},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.lifecycle.closed_at", "value": "2026-01-05T10:00:00Z"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-01-05T10:00:00Z"},
            "2026-01-05T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
    ],
    "plan-item": [
        (
            "plan:phase-1",
            "create",
            {
                "description": "Reconstructed from plan:phase-1.",
                "external_ref": "plan:phase-1",
                "title": "Phase 1: storage",
                "type": "epic",
            },
            "2026-03-01T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1"],
        ),
        (
            "plan:phase-1",
            "label_add",
            {"label": "source:planning"},
            "2026-03-01T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1"],
        ),
        (
            "plan:phase-1",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-03-01T00:00:00Z"},
            "2026-03-01T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1"],
        ),
        (
            "plan:phase-1/a",
            "create",
            {
                "description": "Reconstructed from plan:phase-1/a.",
                "external_ref": "plan:phase-1/a",
                "title": "Write the log",
                "type": "task",
            },
            "2026-03-02T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1/a"],
        ),
        (
            "plan:phase-1/a",
            "edge_add",
            {"target": "plan:phase-1", "type": "parent-child"},
            "2026-03-02T00:00:00Z",
            None,
            "observed",
            None,
            ["plan:phase-1/a"],
        ),
        (
            "plan:phase-1/a",
            "label_add",
            {"label": "source:planning"},
            "2026-03-02T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1/a"],
        ),
        (
            "plan:phase-1/a",
            "phase",
            {"phase": "closed", "reason": "plan-item: plan:phase-1/a"},
            "2026-03-02T00:00:00.000001Z",
            None,
            "observed",
            None,
            ["plan:phase-1/a"],
        ),
        (
            "plan:phase-1/a",
            "set",
            {"field": "metadata.lifecycle.closed_at", "value": "2026-03-02T00:00:00Z"},
            "2026-03-02T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1/a"],
        ),
        (
            "plan:phase-1/a",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-03-02T00:00:00Z"},
            "2026-03-02T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1/a"],
        ),
        (
            "plan:phase-1/b",
            "create",
            {
                "description": "Reconstructed from plan:phase-1/b.",
                "external_ref": "plan:phase-1/b",
                "title": "Read the log",
                "type": "task",
            },
            "2026-03-03T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1/b"],
        ),
        (
            "plan:phase-1/b",
            "edge_add",
            {"target": "plan:phase-1", "type": "parent-child"},
            "2026-03-03T00:00:00Z",
            None,
            "observed",
            None,
            ["plan:phase-1/b"],
        ),
        (
            "plan:phase-1/b",
            "edge_add",
            {"target": "plan:phase-1/a", "type": "blocks"},
            "2026-03-03T00:00:00Z",
            None,
            "observed",
            None,
            ["plan:phase-1/b", "plan:phase-1/a"],
        ),
        (
            "plan:phase-1/b",
            "label_add",
            {"label": "source:planning"},
            "2026-03-03T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1/b"],
        ),
        (
            "plan:phase-1/b",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-03-03T00:00:00Z"},
            "2026-03-03T00:00:00Z",
            None,
            "derived",
            None,
            ["plan:phase-1/b"],
        ),
    ],
    "release-marker": [
        (
            "area:release/v1.0",
            "create",
            {
                "description": "Reconstructed from area:release/v1.0.",
                "external_ref": "area:release/v1.0",
                "title": "release/v1.0",
                "type": "epic",
            },
            "2026-04-03T00:00:00Z",
            None,
            "derived",
            None,
            ["git:tag/v1.0"],
        ),
        (
            "area:release/v1.0",
            "label_add",
            {"label": "source:area"},
            "2026-04-03T00:00:00Z",
            None,
            "derived",
            None,
            ["git:tag/v1.0"],
        ),
        (
            "area:release/v1.0",
            "phase",
            {"phase": "closed", "reason": "released: git:tag/v1.0"},
            "2026-04-03T00:00:00.000001Z",
            None,
            "derived",
            None,
            ["git:tag/v1.0"],
        ),
        (
            "area:release/v1.0",
            "set",
            {"field": "metadata.lifecycle.closed_at", "value": "2026-04-03T00:00:00Z"},
            "2026-04-03T00:00:00Z",
            None,
            "derived",
            None,
            ["git:tag/v1.0"],
        ),
        (
            "area:release/v1.0",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-04-03T00:00:00Z"},
            "2026-04-03T00:00:00Z",
            None,
            "derived",
            None,
            ["git:tag/v1.0"],
        ),
        (
            "area:release/v1.0",
            "set",
            {"field": "metadata.release", "value": "v1.0"},
            "2026-04-03T00:00:00Z",
            None,
            "derived",
            None,
            ["git:tag/v1.0"],
        ),
        (
            "git:commit/c1",
            "create",
            {
                "description": "Reconstructed from git:commit/c1.",
                "external_ref": "git:commit/c1",
                "title": "feat: first",
                "type": "feature",
            },
            "2026-04-01T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "edge_add",
            {"target": "area:release/v1.0", "type": "parent-child"},
            "2026-04-01T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "label_add",
            {"label": "source:commit"},
            "2026-04-01T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "phase",
            {"phase": "closed", "reason": "committed: git:commit/c1"},
            "2026-04-01T10:00:00.000001Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.git.commits", "value": ["c1"]},
            "2026-04-01T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.lifecycle.closed_at", "value": "2026-04-01T10:00:00Z"},
            "2026-04-01T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-04-01T10:00:00Z"},
            "2026-04-01T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c1"],
        ),
        (
            "git:commit/c1",
            "set",
            {"field": "metadata.release", "value": "v1.0"},
            "2026-04-03T00:00:00Z",
            None,
            "derived",
            None,
            ["git:tag/v1.0"],
        ),
        (
            "git:commit/c2",
            "create",
            {
                "description": "Reconstructed from git:commit/c2.",
                "external_ref": "git:commit/c2",
                "title": "feat: second",
                "type": "feature",
            },
            "2026-04-02T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c2"],
        ),
        (
            "git:commit/c2",
            "edge_add",
            {"target": "area:release/v1.0", "type": "parent-child"},
            "2026-04-02T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c2"],
        ),
        (
            "git:commit/c2",
            "label_add",
            {"label": "source:commit"},
            "2026-04-02T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c2"],
        ),
        (
            "git:commit/c2",
            "phase",
            {"phase": "closed", "reason": "committed: git:commit/c2"},
            "2026-04-02T10:00:00.000001Z",
            None,
            "derived",
            None,
            ["git:commit/c2"],
        ),
        (
            "git:commit/c2",
            "set",
            {"field": "metadata.git.commits", "value": ["c2"]},
            "2026-04-02T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c2"],
        ),
        (
            "git:commit/c2",
            "set",
            {"field": "metadata.lifecycle.closed_at", "value": "2026-04-02T10:00:00Z"},
            "2026-04-02T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c2"],
        ),
        (
            "git:commit/c2",
            "set",
            {"field": "metadata.lifecycle.created_at", "value": "2026-04-02T10:00:00Z"},
            "2026-04-02T10:00:00Z",
            None,
            "derived",
            None,
            ["git:commit/c2"],
        ),
        (
            "git:commit/c2",
            "set",
            {"field": "metadata.release", "value": "v1.0"},
            "2026-04-03T00:00:00Z",
            None,
            "derived",
            None,
            ["git:tag/v1.0"],
        ),
    ],
}


# -- goldens ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_golden_per_category(name: str) -> None:
    (_, _, projection), _ = scenario(name)
    assert render(projection) == [tuple(row) for row in GOLDEN[name]]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_event_cites_its_unit_and_evidence_and_has_a_provenance(name: str) -> None:
    (corpus, _, projection), _ = scenario(name)
    keys = {a.key for a in corpus.artifacts}
    by_bead = {u.bead: u.unit for u in projection.units if u.bead}
    assert projection.events
    for event in projection.events:
        assert event.source.system == "collate"
        assert event.source.ref == by_bead[event.bead.id]  # the unit key
        assert event.source.evidence and {x.ref for x in event.source.evidence} <= keys
        assert str(event.provenance) in ("observed", "derived", "inferred")
        assert (event.confidence is not None) == (str(event.provenance) == "inferred")
        assert str(event.kind) in PROJECT_EVENT_KINDS
        assert event.recorded_at is None and event.extracted_at == EXTRACTED


def test_new_creates_carry_the_b5_verdict() -> None:
    (_, col, projection), _ = scenario("review")
    (create,) = [e for e in projection.events if e.kind == "create"]
    verdict = create.annotations["bdlg.collate"]["verdict"]
    decided = next(v for v in col.verdicts if v["unit"] == "gh:pr/5" and v["class"])
    # the review has no baseline candidate: B5 decides it by rule (derived, no confidence)
    assert verdict["step"] == decided["step"] == "no-candidate"
    assert verdict["engine"] == decided["engine"] and verdict["score"] == decided["score"]
    assert decided["confidence_permille"] is None and create.confidence is None
    assert create.provenance == "derived"


def test_day_precision_is_the_start_of_the_day_never_a_midnight_instant() -> None:
    (_, _, projection), _ = scenario("design-record")
    record = projection.unit("doc:docs/design/queue.md")
    assert record is not None
    dated = [e for e in projection.events if e.bead.id == record.bead]
    assert {e.occurred_at for e in dated} == {"2026-02-03T00:00:00Z"}
    assert {str(e.occurred_precision) for e in dated} == {"day"}
    state = beadlog.fold(projection.events)[beadlog.BeadRef(GROUP, record.bead)]
    assert state.fields["metadata.lifecycle.created_at"] == "2026-02-03"  # precision kept
    assert state.phase == "open"  # declared, never added: no closing clock


def test_a_close_at_the_create_instant_is_raised_so_the_bead_folds_closed() -> None:
    (_, _, projection), _ = scenario("change")
    record = projection.unit(ckey(1))
    assert record is not None and "phase-raised" in record.notes
    state = beadlog.fold(projection.events)[beadlog.BeadRef(GROUP, record.bead)]
    assert state.phase == "closed"
    assert state.fields["metadata.lifecycle.closed_at"] == "2026-01-05T10:00:00Z"


# -- idempotency ------------------------------------------------------------------------------


def apply(baseline: Sequence[beadlog.BeadEvent], events: Sequence[beadlog.BeadEvent]):
    store = InMemoryStore()
    store.load(tuple(baseline), dry_run=False)
    store.load(tuple(events), dry_run=False)
    return store.export()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_project_apply_and_project_again_proposes_zero_events(name: str) -> None:
    (corpus, _, first), baseline = scenario(name)
    plan = reconcile(first.events, baseline)
    assert {e.id for e in plan.events} == {e.id for e in first.events}
    applied = apply(baseline, plan.events)
    assert reconcile(first.events, applied).empty

    again = project(
        run_collate(corpus, applied),
        corpus,
        applied,
        prefix=PREFIX,
        group=GROUP,
        extracted_at="2026-11-13T00:00:00Z",
    )
    assert again.events == ()
    projected = {u.unit: u.bead for u in first.units if u.events}
    assert {u.unit: u.bead for u in again.units if u.unit in projected} == projected
    assert {u.decision for u in again.units if u.unit in projected} == {"PRESENT"}


def test_projected_beads_fold_to_closed_history() -> None:
    (_, _, projection), _ = scenario("plan-item")
    states = beadlog.fold(projection.events)
    by_unit = {u.unit: states[beadlog.BeadRef(GROUP, u.bead)] for u in projection.units}
    epic, done, todo = by_unit["plan:phase-1"], by_unit["plan:phase-1/a"], by_unit["plan:phase-1/b"]
    assert epic.type == "epic" and epic.phase == "open"
    assert done.phase == "closed" and todo.phase == "open"
    assert beadlog.Edge("parent-child", epic.bead) in done.edges
    assert beadlog.Edge("blocks", done.bead) in todo.edges
    assert set(done.labels) == {"source:planning"}


# -- matched units never overwrite ------------------------------------------------------------


def test_a_matched_unit_only_appends_and_stamps_and_never_overwrites() -> None:
    hand_made = existing(
        "bh-7",
        "Hand made",
        external_ref="gh:pr/5",
        **{
            "metadata.git.commits": json.dumps([sha(1)]),
            "metadata.lifecycle.created_at": "2025-12-31T00:00:00Z",
            "metadata.release": "v0.9",
        },
    )
    artifacts = review_scenario()["artifacts"] + (
        ev.release_marker(
            "v1.0",
            occurred_at="2026-01-05T00:00:00Z",
            released_at="2026-01-05T00:00:00Z",
            commit_range=sha(2),
        ),
    )
    _, _, projection = run(*artifacts, baseline=[hand_made])
    (record,) = [u for u in projection.units if u.unit == "gh:pr/5"]
    assert record.decision == "PRESENT" and record.bead == "bh-7"
    events = [e for e in projection.events if e.bead.id == "bh-7"]
    assert {str(e.kind) for e in events} == {"set"}
    assert {e.data["field"] for e in events} == {
        "metadata.git.commits",
        "metadata.lifecycle.closed_at",
    }
    assert {(k.field, k.existing) for k in record.kept} == {
        ("metadata.lifecycle.created_at", "2025-12-31T00:00:00Z"),
        ("metadata.release", "v0.9"),
    }
    state = beadlog.fold([hand_made, *projection.events])[beadlog.BeadRef(GROUP, "bh-7")]
    assert state.fields["title"] == "Hand made" and state.fields["external_ref"] == "gh:pr/5"
    assert json.loads(state.fields["metadata.git.commits"]) == [sha(1), sha(2)]
    assert state.fields["metadata.lifecycle.created_at"] == "2025-12-31T00:00:00Z"
    assert state.fields["metadata.release"] == "v0.9"
    assert state.phase == "open" and not state.labels and not state.edges


def test_a_matched_bead_without_external_ref_gets_the_unit_key_stamped() -> None:
    plain = existing("bh-8", "Add parser")  # lexical: the same title
    _, _, projection = run(*review_scenario()["artifacts"], baseline=[plain])
    (record,) = [u for u in projection.units if u.unit == "gh:pr/5"]
    assert record.decision == "PRESENT" and record.bead == "bh-8"
    stamp = next(e for e in projection.events if e.data.get("field") == "external_ref")
    assert stamp.data["value"] == "gh:pr/5" and str(stamp.provenance) == "derived"
    # the set postdates the baseline create, so it wins the fold
    assert beadlog.fold([plain, *projection.events])[stamp.bead].fields["external_ref"] == (
        "gh:pr/5"
    )


def test_a_differently_linked_or_unparsable_bead_is_kept_and_reported() -> None:
    odd = existing(
        "bh-1",
        "Ledger directory port",
        external_ref="gh:pr/99",
        **{"metadata.git.commits": "not json"},
    )
    commit_ = commit(2, "2026-01-06T09:00:00Z", cc_type="fix", refs=[ev.ref("bh-1")])
    _, _, projection = run(commit_, baseline=[odd])
    (record,) = projection.units
    assert record.unit == "bead:bh-1" and record.decision == "PRESENT"
    assert projection.events == ()
    assert [k.field for k in record.kept] == ["metadata.git.commits"]


# -- residual, labels, prefix, determinism ----------------------------------------------------


def test_residual_units_project_nothing_but_are_reported() -> None:
    built = review_scenario()
    corpus = ev.corpus(*built["artifacts"])
    # A baseline bead sharing a title token is a candidate; with no judge and no threshold the
    # unit is then undecided (a unit with no candidate at all would be NEW by rule).
    baseline = [existing("bh-9", "Add parser lexer and grammar notes")]
    col = collate(corpus, baseline, InMemoryLedgerDirectory(DIRECTORY), local="bh")
    projection = project(col, corpus, baseline, prefix=PREFIX, group=GROUP, extracted_at=EXTRACTED)
    assert projection.events == ()
    assert projection.residual == ("gh:pr/5",)
    assert projection.counts()["residual"] == 1


def test_creation_labels_are_an_option_and_off_by_default() -> None:
    (_, _, plain), _ = scenario("review")
    assert {e.data["label"] for e in plain.events if e.kind == "label_add"} == {"source:github"}
    options = ProjectOptions(creation_labels=("origin:backfill",))
    (_, _, labelled), _ = scenario("review", options)
    labels = {e.data["label"] for e in labelled.events if e.kind == "label_add"}
    assert labels == {"source:github", "origin:backfill"}
    assert labelled.to_json()["options"]["creation_labels"] == ["origin:backfill"]


def test_types_are_configurable_per_category() -> None:
    options = ProjectOptions(types={"review": "story"})
    (_, _, projection), _ = scenario("review", options)
    (create,) = [e for e in projection.events if e.kind == "create"]
    assert create.data["type"] == "story"
    with pytest.raises(ProjectError):
        ProjectOptions(types={"pull-request": "story"})
    with pytest.raises(ProjectError):
        ProjectOptions(source_labels={"review": ""})
    with pytest.raises(ProjectError):
        ProjectOptions(creation_labels="origin:backfill")  # type: ignore[arg-type]


def test_the_prefix_is_explicit_and_required() -> None:
    built = review_scenario()
    corpus = ev.corpus(*built["artifacts"])
    col = run_collate(corpus, [])
    with pytest.raises(TypeError):
        project(col, corpus, [], group=GROUP, extracted_at=EXTRACTED)  # type: ignore[call-arg]
    for bad in ("", None, " bh", "-bh"):
        with pytest.raises(ProjectError, match="never derived"):
            project(col, corpus, [], prefix=bad, group=GROUP, extracted_at=EXTRACTED)  # type: ignore[arg-type]
    projected = project(col, corpus, [], prefix="zz", group=GROUP, extracted_at=EXTRACTED)
    assert {e.bead.id.split("-")[0] for e in projected.events} == {"zz"}


def test_minted_ids_never_collide_with_the_baseline() -> None:
    built = review_scenario()
    corpus = ev.corpus(*built["artifacts"])
    first = project(
        run_collate(corpus, []), corpus, [], prefix=PREFIX, group=GROUP, extracted_at=EXTRACTED
    )
    taken = first.units[0].bead
    squatter = existing(taken, "Unrelated squatter")
    col = run_collate(corpus, [squatter])
    second = project(col, corpus, [squatter], prefix=PREFIX, group=GROUP, extracted_at=EXTRACTED)
    assert second.units[0].decision == "NEW" and second.units[0].bead not in (None, taken)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_projection_is_deterministic_and_reads_a_loaded_collation(name: str) -> None:
    (corpus, col, first), baseline = scenario(name)
    (_, _, again), _ = scenario(name)
    assert [e.to_json() for e in first.events] == [e.to_json() for e in again.events]
    assert first.to_json() == again.to_json()
    loaded = loads_collation(dumps_collation(col))  # no in-memory indexes
    reread = project(loaded, corpus, baseline, prefix=PREFIX, group=GROUP, extracted_at=EXTRACTED)
    assert [e.id for e in reread.events] == [e.id for e in first.events]
    later = project(
        col, corpus, baseline, prefix=PREFIX, group=GROUP, extracted_at="2027-01-01T00:00:00Z"
    )
    assert [e.id for e in later.events] == [e.id for e in first.events]  # not identity


def test_source_labels_match_the_existing_corpus_and_are_configurable() -> None:
    expected = {
        "change": {"source:commit"},
        "review": {"source:github"},
        "tracker-item": {"source:commit"},  # its unit is the commit that refs the issue
        "design-record": {"source:docs", "source:commit"},
        "plan-item": {"source:planning"},
        "release-marker": {"source:commit", "source:area"},
    }
    for name, labels in expected.items():
        (_, _, projection), _ = scenario(name)
        assert {e.data["label"] for e in projection.events if e.kind == "label_add"} == labels
    options = ProjectOptions(source_labels={"pr": "source:gh-pr"})  # a kind beats its category
    (_, _, projection), _ = scenario("review", options)
    assert {e.data["label"] for e in projection.events if e.kind == "label_add"} == {"source:gh-pr"}
    assert projection.to_json()["options"]["source_labels"]["review"] == "source:github"
