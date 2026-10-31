"""reconcile(proposed, baseline, judge): NEW / PRESENT / NOISE / DRIFT on fixture pairs, verdicts
recorded in the plan and never re-queried, and the plan archive round trip."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from typing import Any

import pytest

import beadlog
from beadlog_sources import (
    ABSTAIN,
    ChoiceQuery,
    Fallback,
    Judgment,
    ReconcileClass,
    Review,
    ScoreQuery,
    plan_entries,
    prior_verdicts,
    read_plan,
    reconcile,
    write_plan,
)
from beadlog_sources.reconcile import MATCH_FAMILY, PLAN_ANNOTATION

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000aa", "bh")
GENERATOR = beadlog.Generator("beadlog-sources-tests", "0")
EXTRACTED = "2026-10-31T00:00:00Z"


def _ref(bead: str) -> beadlog.BeadRef:
    return beadlog.BeadRef(GROUP.id, bead)


def _event(
    kind: str,
    bead: str,
    data: Mapping[str, Any],
    *,
    at: str,
    source: tuple[str, str],
    recorded: str | None = None,
) -> beadlog.BeadEvent:
    return beadlog.mint_event(
        kind,
        _ref(bead),
        data,
        occurred_at=at,
        recorded_at=recorded,
        extracted_at=EXTRACTED,
        source=source,
        provenance="observed" if source[0] == "bd" else "inferred",
    )


def _create(
    bead: str,
    title: str,
    *,
    kind: str = "task",
    source: str = "bd",
    at: str = "2026-07-01T00:00:00Z",
    **fields: Any,
) -> beadlog.BeadEvent:
    return _event(
        "create",
        bead,
        {"type": kind, "fields": {"title": title, **fields}},
        at=at,
        source=(source, f"{source}:create/{bead}"),
        recorded=at if source == "bd" else None,
    )


def _baseline() -> tuple[beadlog.BeadEvent, ...]:
    return (
        _create("bh-1", "Add the archive writer"),
        _create("bh-2", "BeadLog v0 format and engine", kind="epic"),
        _create("bh-3", "Streaming reader for untrusted archives"),
    )


def _baseline_without_epic() -> tuple[beadlog.BeadEvent, ...]:
    return tuple(event for event in _baseline() if event.bead.id != "bh-2")


class _Scripted:
    """A JudgmentPort answering every choice with a fixed key (counts its calls)."""

    engine = "scripted"
    engine_version = "1"
    model = "fake-model"

    def __init__(self, outcome: str | None, confidence: float = 0.9) -> None:
        self.outcome = outcome
        self.confidence = confidence
        self.queries: list[ChoiceQuery] = []

    def choose(self, query: ChoiceQuery) -> Judgment:
        self.queries.append(query)
        probabilities = {self.outcome or ABSTAIN: 1.0}
        return Judgment(
            self.outcome,
            probabilities,
            self.confidence,
            len(query.options),
            "scripted answer",
            self.engine,
            self.engine_version,
            self.model,
        )

    def score(self, query: ScoreQuery) -> Judgment:  # pragma: no cover - unused here
        raise AssertionError("reconcile asks choices only")


def _entry(plan: Any, bead: str) -> Any:
    (entry,) = [entry for entry in plan.entries if entry.bead == _ref(bead)]
    return entry


def test_reconcile_of_a_log_with_itself_proposes_nothing() -> None:
    baseline = _baseline()
    plan = reconcile(baseline, baseline, _Scripted("new"))
    assert plan.empty and plan.events == () and plan.entries == ()
    assert plan.counts() == {"NEW": 0, "PRESENT": 0, "NOISE": 0, "DRIFT": 0}
    assert plan.calls == 0


def test_fixture_pair_classifies_new_present_and_drift_exactly() -> None:
    baseline = _baseline()
    proposed = (
        *baseline,  # already present: never classified again
        # same bead, same facets, another source: PRESENT, nothing to apply
        _create("bh-3", "Streaming reader for untrusted archives", source="docs"),
        # same bead gains a description: DRIFT
        _event(
            "set",
            "bh-1",
            {"field": "description", "value": "Reproducible ZIP writer."},
            at="2026-07-02T00:00:00Z",
            source=("git", "git:commit/abc"),
        ),
        # unknown id, no lexical overlap with any baseline title: NEW without a judge call
        _create("doc-9", "Quarterly zebra census", source="docs"),
    )
    judge = _Scripted("present:1")
    plan = reconcile(proposed, baseline, judge)

    assert {str(e.bead.id): str(e.cls) for e in plan.entries} == {
        "bh-1": "DRIFT",
        "bh-3": "PRESENT",
        "doc-9": "NEW",
    }
    drift = _entry(plan, "bh-1")
    assert drift.changed == ("fields.description",) and drift.review is Review.NONE
    assert _entry(plan, "bh-3").events == () and _entry(plan, "bh-3").verdict is None
    new = _entry(plan, "doc-9")
    assert new.review is Review.NONE and new.verdict is None and new.note == "no baseline candidate"
    planned = {event.id for event in plan.events}
    assert planned == set(drift.events) | set(new.events)
    assert judge.queries == [] and plan.calls == 0


def test_unknown_id_with_an_exact_title_is_matched_deterministically() -> None:
    proposed = (_create("doc-1", "Add the archive writer", source="docs"),)
    judge = _Scripted("new")
    plan = reconcile(proposed, _baseline(), judge)
    entry = _entry(plan, "doc-1")
    assert entry.cls is ReconcileClass.PRESENT and entry.match == _ref("bh-1")
    assert entry.verdict is not None
    assert entry.verdict.judgment.engine == "deterministic-lexical"
    assert entry.verdict.judgment.confidence == 1.0
    assert entry.review is Review.REQUIRED  # every default off: no auto-accept threshold
    assert judge.queries == [] and plan.events == ()

    accepted = reconcile(proposed, _baseline(), judge, auto_accept={MATCH_FAMILY: 0.95})
    assert _entry(accepted, "doc-1").review is Review.AUTO


def test_residual_goes_to_the_judge_and_the_verdict_is_recorded() -> None:
    proposed = (_create("doc-2", "Archive writer tests", source="docs"),)
    judge = _Scripted("new", confidence=0.8)
    plan = reconcile(proposed, _baseline(), judge, auto_accept={MATCH_FAMILY: 0.75})
    entry = _entry(plan, "doc-2")
    assert entry.cls is ReconcileClass.NEW and entry.review is Review.AUTO
    assert entry.note == "judged new" and len(plan.events) == 1
    (query,) = judge.queries
    assert query.family == MATCH_FAMILY and query.context.startswith("Archive writer tests")
    assert {"present:1", "new", ABSTAIN} <= set(query.options)
    recorded = entry.verdict.to_json()
    assert recorded["engine"] == "scripted" and recorded["model"] == "fake-model"
    assert recorded["confidence"] == 0.8 and recorded["option_count"] == len(query.options)
    assert recorded["rationale"] == "scripted answer" and recorded["fallback"] is None


def test_abstention_is_new_but_needs_review() -> None:
    plan = reconcile(
        (_create("doc-3", "Archive writer tests", source="docs"),),
        _baseline(),
        _Scripted(None),
        auto_accept={MATCH_FAMILY: 0.0},
    )
    entry = _entry(plan, "doc-3")
    assert entry.cls is ReconcileClass.NEW and entry.review is Review.REQUIRED
    assert entry.note == "judge abstained"


def test_noise_requires_a_covering_epic_event_in_the_baseline() -> None:
    proposal = (_create("doc-4", "BeadLog format and archive writer tidy-ups", source="docs"),)
    judge = _Scripted("noise:1")
    plan = reconcile(proposal, _baseline(), judge)
    entry = _entry(plan, "doc-4")
    assert entry.cls is ReconcileClass.NOISE and entry.covered_by == _ref("bh-2")
    assert plan.events == ()

    # Without the epic's event in the baseline there is no noise option, and a judge naming
    # one anyway is refused rather than trusted.
    without = reconcile(proposal, _baseline_without_epic(), _Scripted("noise:1"))
    assert all(entry.cls is not ReconcileClass.NOISE for entry in without.entries)
    refused = _entry(without, "doc-4")
    assert refused.cls is ReconcileClass.NEW and refused.review is Review.REQUIRED
    assert "not an offered option" in refused.verdict.judgment.rationale


def test_a_baseline_epic_named_as_parent_is_always_a_noise_candidate() -> None:
    proposal = (
        _create("doc-5", "Zebra census", source="docs"),
        _event(
            "edge_add",
            "doc-5",
            {"type": "parent-child", "target": {"group": GROUP.id, "id": "bh-2"}},
            at="2026-07-01T00:00:00Z",
            source=("docs", "docs:edge/doc-5"),
        ),
    )
    judge = _Scripted("noise:1")
    plan = reconcile(proposal, _baseline(), judge)
    assert _entry(plan, "doc-5").covered_by == _ref("bh-2")
    assert judge.queries[0].options["noise:1"] == "BeadLog v0 format and engine"


def test_recorded_verdicts_are_never_re_queried() -> None:
    proposal = (_create("doc-6", "Archive writer tests", source="docs"),)
    first_judge = _Scripted("new")
    first = reconcile(proposal, _baseline(), first_judge)
    assert first.calls == 1

    buffer = io.BytesIO()
    write_plan(buffer, first, groups=[GROUP], generator=GENERATOR)
    _, entries = read_plan(io.BytesIO(buffer.getvalue()))
    second_judge = _Scripted("present:1")
    second = reconcile(proposal, _baseline(), second_judge, prior=prior_verdicts(entries))
    assert second_judge.queries == [] and second.calls == 0
    assert _entry(second, "doc-6").cls is ReconcileClass.NEW
    assert second.plan_section()["plan_hash"] == first.plan_section()["plan_hash"]


def test_fallback_provenance_is_recorded_and_forces_review() -> None:
    class _FallingBack(_Scripted):
        def choose(self, query: ChoiceQuery) -> Judgment:
            verdict = super().choose(query)
            return Judgment(
                None,
                {},
                0.0,
                verdict.option_count,
                "lexical abstain",
                "deterministic-lexical",
                "1",
                None,
                Fallback("deterministic-lexical", "timeout", "no answer within 30s"),
            )

    plan = reconcile(
        (_create("doc-7", "Archive writer tests", source="docs"),),
        _baseline(),
        _FallingBack("new"),
        auto_accept={MATCH_FAMILY: 0.0},
    )
    entry = _entry(plan, "doc-7")
    assert entry.review is Review.REQUIRED
    assert entry.verdict.to_json()["fallback"] == {
        "engine": "deterministic-lexical",
        "reason": "timeout",
        "detail": "no answer within 30s",
    }


def _validate_manifest(manifest: beadlog.Manifest) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(beadlog.archive.packaged_schemas()["schema/manifest.schema.json"])
    jsonschema.Draft202012Validator(schema).validate(manifest.to_json())


def test_plan_archive_round_trips_with_a_schema_valid_plan_section() -> None:
    baseline = _baseline()
    proposed = (
        _create("doc-8", "Archive writer tests", source="docs"),
        _create("bh-3", "Streaming reader for untrusted archives", source="docs"),
    )
    plan = reconcile(proposed, baseline, _Scripted("new", confidence=0.625))
    buffer = io.BytesIO()
    manifest = write_plan(buffer, plan, groups=[GROUP], generator=GENERATOR)
    _validate_manifest(manifest)
    assert "plan" in manifest.capabilities.optional
    section = manifest.plan
    assert section["pass"] == "reconcile" and section["corpus_hash"] == plan.corpus_hash
    assert dict(section["judge"]) == {"engine": "scripted", "version": "1", "model": "fake-model"}

    with beadlog.ArchiveReader(io.BytesIO(buffer.getvalue())) as reader:
        reader.verify(ids=True)
        assert [event.id for event in reader.events()] == [
            event.id for event in beadlog.sort_events(plan.events)
        ]
        assert plan_entries(reader.manifest) == plan.entries
        assert reader.manifest.annotations[PLAN_ANNOTATION]["counts"] == plan.counts()


def test_empty_plan_writes_a_valid_archive() -> None:
    baseline = _baseline()
    buffer = io.BytesIO()
    manifest = write_plan(
        buffer, reconcile(baseline, baseline), groups=[GROUP], generator=GENERATOR
    )
    _validate_manifest(manifest)
    manifest_read, events = beadlog.read_archive(io.BytesIO(buffer.getvalue()))
    assert events == () and plan_entries(manifest_read) == ()


@pytest.mark.parametrize("candidates", [1, 3])
def test_candidate_lists_are_bounded(candidates: int) -> None:
    baseline = tuple(_create(f"bh-{n}", f"Archive task {n}") for n in range(10))
    judge = _Scripted(None)
    reconcile(
        (_create("doc-x", "Archive work", source="docs"),),
        baseline,
        judge,
        max_candidates=candidates,
    )
    (query,) = judge.queries
    assert sum(key.startswith("present:") for key in query.options) == candidates
