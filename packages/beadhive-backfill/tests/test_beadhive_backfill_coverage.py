"""Link-coverage ratios, inputs hash and cache over a hand-computed fixture hive.

No clock, no subprocess, no store: the hive is rows folded to events, the commit list and doc set
are literals, readers are counting fakes and the clock is a constant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import beadlog
from beadhive_backfill.coverage import (
    CACHE_VERSION,
    CacheState,
    CoverageError,
    CoverageReport,
    CoverageSettings,
    Ratio,
    check_cache,
    compute_coverage,
    coverage_schema,
    measure,
    normalize_doc_ref,
    read_coverage_cache,
    write_coverage_cache,
)
from beadlog_sources import baseline_events

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000dd", "wd")
STAMP = "2026-11-01T00:00:00Z"
SHA = {n: f"{n}" * 40 for n in "1234567"}


def row(iid: str, issue_type: str = "task", status: str = "closed", **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": issue_type,
        "status": status,
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [],
        "created_at": "2026-08-10T09:00:00Z",
        "updated_at": "2026-08-20T00:00:00Z",
        **({"closed_at": "2026-08-12T10:00:00Z"} if status == "closed" else {}),
        **extra,
    }


def commits_meta(*shas: str) -> dict[str, Any]:
    return {"metadata": {"git.commits": json.dumps(list(shas))}}


# Closed and code-bearing under the default types (bug chore feature task):
#   wd-1 task, commits           linked
#   wd-2 bug, no metadata        unlinked
#   wd-3 feature, malformed      unlinked (not a JSON array of strings)
#   wd-9 chore, closed, commits  linked
#   wd-10 task, empty list       unlinked
# => (i) 2/5. Not counted: wd-4 (open), wd-5 (epic), wd-6 gate, wd-7 event, wd-8 molecule (the
# last four closed).
ROWS = [
    row("wd-1", **commits_meta(SHA["1"])),
    row("wd-2", "bug"),
    row("wd-3", "feature", metadata={"git.commits": "not json"}),
    row("wd-4", status="open"),
    row("wd-5", "epic", **commits_meta(SHA["2"])),
    row("wd-6", "gate"),
    row("wd-7", "event"),
    row("wd-8", "molecule"),
    row("wd-9", "chore", **commits_meta(SHA["3"], SHA["4"])),
    row("wd-10", metadata={"git.commits": "[]"}, external_ref="docs/design/b.md#top"),
]


@dataclass(frozen=True)
class FakeCommit:
    sha: str
    subject: str
    message: str


def commit(n: str, subject: str, body: str = "") -> FakeCommit:
    return FakeCommit(SHA[n], subject, f"{subject}\n{body}")


# 7 commits, newest first. Resolving: 1 (subject), 2 (body only), 3 (two beads once), 4 (case
# and a segment suffix). Not resolving: 5 (no id), 6 (bh-infra style over-match), 7 (a bead
# of another namespace).
COMMITS = [
    commit("1", "feat: widget (wd-1)"),
    commit("2", "fix: edge case", "Fixes wd-2 and wd-2 again"),
    commit("3", "chore(merge): bead wd-9", "also wd-1"),
    commit("4", "docs: note WD-10.1 follow-up"),
    commit("5", "docs: typo"),
    commit("6", "build: bump wd-version"),
    commit("7", "chore: other hive xx-1"),
]
DOCS = [
    "docs/design/a.md",  # unlinked
    "docs/design/b.md",  # linked by wd-10's external_ref (fragment ignored)
    "docs/decisions/c.md",  # unlinked
    "docs/d.md",  # unlinked
    "docs/e.md",  # linked by wd-1's ref below
]


def events(rows: list[dict[str, Any]] | None = None) -> tuple[beadlog.BeadEvent, ...]:
    return baseline_events(rows if rows is not None else ROWS, group=GROUP.id, extracted_at=STAMP)


def report(**kw: Any) -> CoverageReport:
    return compute_coverage(
        events(kw.pop("rows", None)),
        kw.pop("commits", COMMITS),
        kw.pop("docs", DOCS),
        computed_at=STAMP,
        group=GROUP.id,
        **kw,
    )


def with_ref() -> list[dict[str, Any]]:
    rows = [dict(r) for r in ROWS]
    rows[0] = {**rows[0], "external_ref": "./docs/e.md"}
    return rows


# -- the ratios ------------------------------------------------------------------------------


def test_bead_ratio_counts_closed_code_beads_with_recorded_commits() -> None:
    result = report()
    # wd-1, wd-2, wd-3, wd-9, wd-10 are closed code-bearing; wd-1 and wd-9 record commits.
    assert result.beads == Ratio(2, 5)
    assert result.beads.to_json() == {"linked": 2, "total": 5, "ratio": 0.4}
    assert [e["id"] for e in result.examples.beads] == ["wd-10", "wd-2", "wd-3"]
    assert result.examples.beads[1] == {"id": "wd-2", "title": "Bead wd-2"}


def test_infrastructure_types_never_enter_the_denominator_even_when_configured() -> None:
    every = CoverageSettings(
        code_types=("bug", "chore", "feature", "task", "epic", "gate", "event", "molecule")
    )
    result = report(settings=every)
    # only the closed epic wd-5 (which records a commit) joins; gate/event/molecule never do
    assert result.beads == Ratio(3, 6)
    assert {e["id"] for e in result.examples.beads} == {"wd-10", "wd-2", "wd-3"}
    narrow = report(settings=CoverageSettings(code_types=("task",)))
    assert narrow.beads == Ratio(1, 2)  # wd-1 linked, wd-10 not
    none = report(settings=CoverageSettings(code_types=("gate", "event", "molecule")))
    assert none.beads == Ratio(0, 0) and none.beads.value is None
    assert none.beads.to_json()["ratio"] is None


def test_open_beads_are_not_in_the_denominator() -> None:
    rows = [
        row("wd-1", status="open", **commits_meta(SHA["1"])),
        row("wd-2", status="in_progress"),
    ]
    assert report(rows=rows).beads == Ratio(0, 0)  # nothing closed


def test_commit_ratio_resolves_ids_in_subject_and_body_once_per_commit() -> None:
    result = report()
    assert result.commits == Ratio(4, 7)
    assert [e["sha"] for e in result.examples.commits] == [SHA["5"], SHA["6"], SHA["7"]]
    assert result.examples.commits[0] == {"sha": SHA["5"], "subject": "docs: typo"}


def test_doc_ratio_matches_external_refs_after_normalizing() -> None:
    result = report(rows=with_ref())
    assert result.docs == Ratio(2, 5)  # b.md via "docs/design/b.md#top", e.md via "./docs/e.md"
    assert [e["path"] for e in result.examples.docs] == [
        "docs/d.md",
        "docs/decisions/c.md",
        "docs/design/a.md",
    ]
    assert report().docs == Ratio(1, 5)
    assert normalize_doc_ref(" ./docs//x.md#frag ") == "docs/x.md"
    assert normalize_doc_ref("#only") == ""


def test_example_limit_and_recency_order() -> None:
    rows = [
        row("wd-1", closed_at="2026-08-01T00:00:00Z"),
        row("wd-2", closed_at="2026-09-01T00:00:00Z"),
        row("wd-3", closed_at="2026-08-15T00:00:00Z"),
    ]
    result = report(rows=rows, settings=CoverageSettings(example_limit=2))
    assert [e["id"] for e in result.examples.beads] == ["wd-2", "wd-3"]
    assert len(result.examples.commits) == 2 and len(result.examples.docs) == 2
    assert report(settings=CoverageSettings(example_limit=0)).examples.beads == ()
    with pytest.raises(CoverageError):
        CoverageSettings(example_limit=-1)


def test_empty_inputs_have_null_ratios_and_no_namespace_does_not_crash() -> None:
    result = compute_coverage([], [], [], computed_at=STAMP)
    assert (result.beads.total, result.commits.total, result.docs.total) == (0, 0, 0)
    assert result.beads.value is None
    # commits but no beads: nothing can resolve
    only_commits = compute_coverage([], COMMITS, [], computed_at=STAMP)
    assert only_commits.commits == Ratio(0, len(COMMITS))


def test_ratio_rejects_impossible_counts() -> None:
    with pytest.raises(CoverageError):
        Ratio(3, 2)


# -- the inputs hash -------------------------------------------------------------------------


def test_inputs_hash_is_order_independent_and_tracks_every_input() -> None:
    base = report().inputs_hash
    assert base.startswith("sha256:") and len(base) == len("sha256:") + 64
    assert report(commits=list(reversed(COMMITS)), docs=list(reversed(DOCS))).inputs_hash == base
    assert report(rows=list(reversed(ROWS))).inputs_hash == base
    # computed_at and the example limit are not inputs
    assert report(settings=CoverageSettings(example_limit=1)).inputs_hash == base

    changed = {
        "a bead gains a commit": report(
            rows=[*ROWS[:1], {**ROWS[1], **commits_meta(SHA["5"])}, *ROWS[2:]]
        ),
        "a doc appears": report(docs=[*DOCS, "docs/f.md"]),
        "a commit appears": report(commits=[*COMMITS, commit("5", "other", "x")]),
        "a commit message is amended": report(
            commits=[commit("1", "feat: different"), *COMMITS[1:]]
        ),
        "a bead is linked to a doc": report(rows=with_ref()),
        "the types change": report(settings=CoverageSettings(code_types=("task",))),
        "the roots change": report(settings=CoverageSettings(doc_roots=("docs", "notes"))),
    }
    hashes = {name: r.inputs_hash for name, r in changed.items()}
    assert base not in hashes.values()
    assert len(set(hashes.values())) == len(hashes)


# -- the cache -------------------------------------------------------------------------------


def test_cache_round_trips_and_validates_against_the_schema(tmp_path: Path) -> None:
    result = report(rows=with_ref())
    path = write_coverage_cache(tmp_path / "state" / "coverage.json", result)
    assert read_coverage_cache(path) == result
    data = json.loads(path.read_text())
    assert data["version"] == CACHE_VERSION == 1 and data["computed_at"] == STAMP
    schema = coverage_schema()
    cache_schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/cache"}
    jsonschema.Draft202012Validator(cache_schema).validate(data)
    assert not list(tmp_path.joinpath("state").glob("*.tmp"))  # the atomic temp file is gone


def test_write_replaces_the_previous_cache(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    write_coverage_cache(path, report())
    newer = report(docs=[*DOCS, "docs/f.md"])
    write_coverage_cache(path, newer)
    assert read_coverage_cache(path) == newer


def test_check_cache_reports_fresh_stale_missing_invalid_and_unverified(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    current = report()

    assert check_cache(path, current.inputs_hash).state is CacheState.MISSING

    write_coverage_cache(path, current)
    fresh = check_cache(path, current.inputs_hash)
    assert fresh.state is CacheState.FRESH and fresh.usable and fresh.report == current
    assert check_cache(path).state is CacheState.UNVERIFIED  # a reader with no inputs at hand

    newer = report(docs=[*DOCS, "docs/f.md"])
    stale = check_cache(path, newer.inputs_hash)
    assert stale.state is CacheState.STALE and not stale.usable
    assert stale.report == current  # shown for context, never presented as current
    assert "inputs changed" in stale.detail

    path.write_text("{not json")
    assert check_cache(path, current.inputs_hash).state is CacheState.INVALID
    path.write_text(json.dumps({**current.to_json(), "version": 99}))
    unsupported = check_cache(path, current.inputs_hash)
    assert unsupported.state is CacheState.INVALID and "version" in unsupported.detail
    path.write_text(json.dumps({"schema": "other"}))
    assert check_cache(path).state is CacheState.INVALID
    broken = current.to_json()
    del broken["ratios"]["docs_linked_to_bead"]
    path.write_text(json.dumps(broken))
    assert check_cache(path).state is CacheState.INVALID


# -- the readers -----------------------------------------------------------------------------


class CountingReaders:
    def __init__(self) -> None:
        self.calls = {"beads": 0, "commits": 0, "docs": 0}

    def beads(self):
        self.calls["beads"] += 1
        return events()

    def commits(self):
        self.calls["commits"] += 1
        return COMMITS

    def docs(self):
        self.calls["docs"] += 1
        return DOCS


def test_measure_reads_each_input_exactly_once_and_stamps_the_injected_clock() -> None:
    readers = CountingReaders()
    ticks = iter(["2026-11-02T00:00:00Z", "never"])
    result = measure(readers, clock=lambda: next(ticks), group=GROUP.id)
    assert readers.calls == {"beads": 1, "commits": 1, "docs": 1}
    assert result.computed_at == "2026-11-02T00:00:00Z"
    assert result == compute_coverage(
        events(), COMMITS, DOCS, computed_at="2026-11-02T00:00:00Z", group=GROUP.id
    )


def test_group_scoping_ignores_other_groups() -> None:
    other = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000ee", "zz")
    foreign = baseline_events(
        [row("wd-77", **commits_meta(SHA["1"]))], group=other.id, extracted_at=STAMP
    )
    mixed = (*events(), *foreign)
    scoped = compute_coverage(mixed, COMMITS, DOCS, computed_at=STAMP, group=GROUP.id)
    assert scoped.beads == report().beads and scoped.inputs_hash == report().inputs_hash
    unscoped = compute_coverage(mixed, COMMITS, DOCS, computed_at=STAMP)
    assert unscoped.beads == Ratio(3, 6)


def test_schema_accepts_every_status_envelope() -> None:
    validator = jsonschema.Draft202012Validator(coverage_schema())
    current = report()
    for status, cache in (
        ("fresh", current.to_json()),
        ("stale", current.to_json()),
        ("missing", None),
        ("invalid", None),
    ):
        validator.validate(
            {
                "status": status,
                "cache_path": "/state/coverage.json",
                "detail": "",
                "refreshed": False,
                "current_inputs_hash": current.inputs_hash,
                "cache": cache,
            }
        )
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"status": "maybe", "cache_path": "x", "detail": "", "refreshed": 0})
