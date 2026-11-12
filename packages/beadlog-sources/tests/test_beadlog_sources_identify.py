"""beadlog-sources: collate B5, identifying work units against the baseline.

Bridges, the lexical engine, the opt-in judge (a fake command runner: no subprocess, no
network), the opt-in NEW threshold and the residual; resume by query hash across capped runs;
the cost preview before any judge call; the record schemas and strict loading.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest

import beadlog
from beadlog_sources import (
    CollationFormatError,
    CommandJudge,
    CommandResult,
    CostPreview,
    IdentifyConfirmationRequired,
    IdentifyOptions,
    IdentifyOptionsError,
    IdentifyRun,
    JudgeSettings,
    collate,
    collation_schema,
    dumps_collation,
    identify_prior_verdicts,
    loads_collation,
)
from beadlog_sources.identify import (
    FAMILY,
    RESIDUAL_MEMBERS,
    THRESHOLD_LABEL,
    VERDICT_MEMBERS,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000aa", "bh")


def bead(
    ident: str, title: str, *, type_: str = "task", external_ref: str | None = None
) -> beadlog.BeadEvent:
    fields: dict[str, Any] = {"title": title}
    if external_ref is not None:
        fields["external_ref"] = external_ref
    return beadlog.mint_event(
        "create",
        beadlog.BeadRef(GROUP.id, ident),
        {"type": type_, "fields": fields},
        occurred_at="2026-07-01T00:00:00Z",
        recorded_at="2026-07-01T00:00:00Z",
        extracted_at="2026-10-31T00:00:00Z",
        source=("bd", f"bd:create/{ident}"),
        provenance="observed",
    )


BASELINE = (
    bead("bh-1", "Archive writer"),
    bead("bh-2", "Ledger directory port"),
    bead("bh-3", "Old design", external_ref="docs/linked.md"),
)


def doc(path: str, title: str, **kw: Any):
    return ev.design_record(path, title=title, **kw)


def run(
    *artifacts,
    baseline: Sequence[beadlog.BeadEvent] = BASELINE,
    options: IdentifyOptions | None = None,
    identify_run: IdentifyRun | None = None,
):
    return collate(
        ev.corpus(*artifacts),
        baseline,
        InMemoryLedgerDirectory({"hq": ["hq-9"]}),
        local="bh",
        identify_options=options,
        identify_run=identify_run,
    )


class FakeRunner:
    """A ``bh-judge/1`` command runner: answers by a script, records every request."""

    def __init__(
        self,
        answer: Callable[[dict[str, Any]], str | None] = lambda _: "new",
        *,
        log: list[str] | None = None,
        returncode: int = 0,
    ) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []
        self.log = log if log is not None else []
        self.returncode = returncode

    def __call__(self, argv: Sequence[str], stdin: bytes, timeout: float) -> CommandResult:
        request = json.loads(stdin)
        self.requests.append(request)
        self.log.append("call")
        outcome = self.answer(request)
        probabilities = {outcome: 0.9, "abstain": 0.1} if outcome else {"abstain": 1.0}
        body = {
            "schema": "bh-judge/1",
            "type": "response",
            "outcome": outcome,
            "probabilities": probabilities,
            "rationale": "fake answer",
            "engine": "fake-judge",
            "engine_version": "7",
            "model": "fake-model",
        }
        return CommandResult(self.returncode, json.dumps(body).encode())


def judged(runner: FakeRunner, **kw: Any) -> IdentifyRun:
    kw.setdefault("preview", lambda _: None)
    cap = kw.pop("cap", 100)
    judge = CommandJudge("fake-judge", runner=runner, model="fake-model", max_calls_per_run=cap)
    return IdentifyRun(judge=judge, max_calls_per_run=cap, **kw)


def decisions(col) -> dict[str, dict[str, Any]]:
    return {v["unit"]: v for v in col.verdicts if v["class"] is not None}


def residual(col) -> dict[str, dict[str, Any]]:
    return {r["unit"]: r for r in col.residual}


# -- the steps -------------------------------------------------------------------------------


def test_judge_off_and_no_threshold_is_residual_only() -> None:
    arts = [
        doc("docs/linked.md", "Linked doc"),  # external_ref bridge
        doc("docs/archive.md", "Archive writer"),  # lexical PRESENT
        doc("docs/near.md", "Archive writer redesign notes"),  # candidates, lexical abstains
        doc("docs/zebra.md", "Zebra census"),  # no candidate at all
    ]
    col = run(*arts)
    assert col == run(*arts, identify_run=IdentifyRun())  # default run is the judge-off run
    assert col.options["identify"] == {"new_threshold": None, "max_candidates": 5}
    got = decisions(col)
    assert got["doc:docs/linked.md"]["step"] == "external-ref"
    assert got["doc:docs/linked.md"]["bead"] == "bh-3"
    assert got["doc:docs/archive.md"]["step"] == "lexical"
    assert got["doc:docs/archive.md"]["class"] == "PRESENT"
    assert got["doc:docs/archive.md"]["bead"] == "bh-1"
    assert all(v["class"] != "NEW" and v["step"] != "judge" for v in col.verdicts)
    left = residual(col)
    assert set(left) == {"doc:docs/near.md", "doc:docs/zebra.md"}
    assert {r["reason"] for r in left.values()} == {"no-match"}
    assert left["doc:docs/near.md"]["candidates"] == ["bh-1"]
    assert left["doc:docs/zebra.md"] == {
        "unit": "doc:docs/zebra.md",
        "reason": "no-match",
        "score": 0,
        "query_hash": None,
        "candidates": [],
    }


def test_explicit_id_attach_unit_is_present() -> None:
    change = ev.change(f"{1:040x}", title="feat: tidy", refs=[ev.ref("bh-2")], paths=["src/a.py"])
    col = run(change)
    (verdict,) = col.verdicts
    assert verdict["unit"] == "bead:bh-2" and verdict["step"] == "explicit-id"
    assert verdict["class"] == "PRESENT" and verdict["bead"] == "bh-2"
    assert verdict["provenance"] == "derived" and verdict["review"] == "none"
    assert verdict["confidence_permille"] == 1000 and col.residual == ()


def test_external_ref_matches_unit_key() -> None:
    plan = ev.plan_item("p-1", title="Something else entirely")
    base = (*BASELINE, bead("bh-4", "Planned", external_ref="plan:p-1"))
    (verdict,) = run(plan, baseline=base).verdicts
    assert verdict["step"] == "external-ref" and verdict["bead"] == "bh-4"


def test_threshold_over_197_docs_yields_inferred_new() -> None:
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
    arts = [
        doc(f"docs/design/d{i:03d}.md", f"{words[i % 12]} {words[(i // 12) % 12]} topic{i}")
        for i in range(190)
    ]
    arts += [doc(f"docs/design/w{i}.md", f"Archive notes on extension point {i}") for i in range(4)]
    arts += [doc(f"docs/design/x{i}.md", f"Archive writer {i}") for i in range(3)]
    assert len(arts) == 197
    col = run(*arts, options=IdentifyOptions(new_threshold=40))
    assert len(col.units) == 197
    new = [v for v in col.verdicts if v["class"] == "NEW"]
    assert len(new) > 0
    for verdict in new:
        assert verdict["step"] == "threshold" and verdict["provenance"] == "inferred"
        assert 0 < verdict["confidence_permille"] <= 1000
        assert verdict["labels"] == [THRESHOLD_LABEL] and verdict["review"] == "required"
        assert verdict["score"] < 40
    # "Archive writer N" scores 67% against bh-1: above the threshold, left for a human.
    assert {r["unit"] for r in col.residual} == {f"doc:docs/design/x{i}.md" for i in range(3)}
    assert len(new) + len(col.residual) == 197
    # Off by default: the same corpus without a threshold has no NEW at all.
    assert not [v for v in run(*arts).verdicts if v["class"] == "NEW"]


def test_judge_decides_and_records_engine_model_confidence() -> None:
    runner = FakeRunner(lambda r: "present:1" if "redesign" in r["context"] else None)
    arts = [
        doc("docs/near.md", "Archive writer redesign notes"),
        doc("docs/other.md", "Ledger port thoughts"),
        doc("docs/zebra.md", "Zebra census"),
    ]
    col = run(*arts, identify_run=judged(runner, auto_accept={FAMILY: 0.5}))
    assert len(runner.requests) == 2  # the zebra doc has no candidate: never sent
    got = decisions(col)["doc:docs/near.md"]
    assert got["step"] == "judge" and got["class"] == "PRESENT" and got["bead"] == "bh-1"
    assert got["engine"] == "fake-judge" and got["model"] == "fake-model"
    assert got["provenance"] == "inferred" and got["review"] == "auto"
    assert got["confidence_permille"] == 850  # Jev confidence of 0.9 over 3 options
    assert got["probabilities_permille"] == {"abstain": 100, "present:1": 900}
    assert got["family"] == FAMILY and got["query_hash"].startswith("sha256:")
    abstained = [v for v in col.verdicts if v["unit"] == "doc:docs/other.md"]
    assert [v["class"] for v in abstained] == [None]
    assert residual(col)["doc:docs/other.md"]["reason"] == "judge-abstained"
    assert residual(col)["doc:docs/zebra.md"]["reason"] == "no-match"


def test_judge_abstention_then_threshold_keeps_both_records() -> None:
    runner = FakeRunner(lambda _: None)
    col = run(
        doc("docs/near.md", "Archive writer redesign notes"),
        options=IdentifyOptions(new_threshold=60),  # its score is 50%
        identify_run=judged(runner),
    )
    steps = [(v["step"], v["class"]) for v in col.verdicts]
    assert steps == [("judge", None), ("threshold", "NEW")]
    assert col.residual == ()
    assert list(identify_prior_verdicts(col)) == [col.verdicts[0]["query_hash"]]


def test_judge_failure_is_recorded_retried_and_not_reused() -> None:
    runner = FakeRunner(returncode=3)
    col = run(doc("docs/near.md", "Archive writer redesign notes"), identify_run=judged(runner))
    (verdict,) = col.verdicts
    assert verdict["fallback"]["reason"] == "exit" and verdict["class"] is None
    assert residual(col)["doc:docs/near.md"]["reason"] == "judge-failed"
    assert identify_prior_verdicts(col) == {}


# -- resume and the cost preview -------------------------------------------------------------

CAP = 3


def _near_docs(count: int) -> list[Any]:
    return [doc(f"docs/n{i}.md", f"Archive writer redesign part{i}") for i in range(count)]


def test_two_capped_runs_finish_a_residual_of_twice_the_cap() -> None:
    arts = _near_docs(2 * CAP)
    first_runner = FakeRunner(lambda _: "new")
    with pytest.raises(IdentifyConfirmationRequired) as refused:
        run(*arts, identify_run=judged(first_runner, cap=CAP))
    assert refused.value.preview.calls == 2 * CAP and first_runner.requests == []

    first = run(*arts, identify_run=judged(first_runner, cap=CAP, confirm=True))
    assert len(first_runner.requests) == CAP
    assert [r["reason"] for r in first.residual] == ["judge-budget"] * CAP
    assert len(decisions(first)) == CAP

    loaded = loads_collation(dumps_collation(first))  # resume from the persisted collation
    prior = identify_prior_verdicts(loaded)
    assert len(prior) == CAP
    second_runner = FakeRunner(lambda _: "new")
    second = run(*arts, identify_run=judged(second_runner, cap=CAP, prior=prior))
    assert len(second_runner.requests) == CAP  # no confirmation needed: N == cap
    asked_first = {json.dumps(r, sort_keys=True) for r in first_runner.requests}
    asked_second = {json.dumps(r, sort_keys=True) for r in second_runner.requests}
    assert not asked_first & asked_second
    assert second.residual == ()
    assert set(decisions(second)) == {f"doc:docs/n{i}.md" for i in range(2 * CAP)}
    # every recorded verdict carries on: a third run asks nothing at all
    third_runner = FakeRunner()
    third = run(
        *arts,
        identify_run=judged(third_runner, cap=CAP, prior=identify_prior_verdicts(second)),
    )
    assert third_runner.requests == [] and third.verdicts == second.verdicts


def test_cost_preview_comes_before_any_judge_call(capsys) -> None:
    log: list[str] = []
    previews: list[CostPreview] = []

    def sink(preview: CostPreview) -> None:
        previews.append(preview)
        log.append("preview")

    runner = FakeRunner(log=log)
    run(*_near_docs(4), identify_run=judged(runner, preview=sink, cost_per_call_usd=0.0025))
    assert log == ["preview", "call", "call", "call", "call"]
    (preview,) = previews
    assert preview.calls == 4 and preview.estimated_usd == pytest.approx(0.01)
    assert str(preview).startswith("4 judge calls ≈ $0.01")

    # the default sink prints the line on stderr, still before the first call
    runner = FakeRunner()
    judge = CommandJudge("fake-judge", runner=runner, max_calls_per_run=10)
    run(*_near_docs(2), identify_run=IdentifyRun(judge=judge, cost_per_call_usd=0.5))
    assert "identify: 2 judge calls ≈ $1.00 (max 10 per run)" in capsys.readouterr().err

    # judge off: no preview, no call
    run(*_near_docs(2), identify_run=IdentifyRun(preview=sink))
    assert len(previews) == 1


def test_preview_counts_prior_reuse_and_unknown_cost() -> None:
    first = run(*_near_docs(2), identify_run=judged(FakeRunner()))
    previews: list[CostPreview] = []
    run(
        *_near_docs(3),
        identify_run=judged(
            FakeRunner(), preview=previews.append, prior=identify_prior_verdicts(first)
        ),
    )
    (preview,) = previews
    assert (preview.calls, preview.reused, preview.estimated_usd) == (1, 2, None)
    assert "2 reused" in str(preview) and "$?" in str(preview)


def test_from_settings_builds_the_configured_judge() -> None:
    runner = FakeRunner()
    settings = JudgeSettings(enabled=True, max_calls_per_run=1, auto_accept={FAMILY: 0.1})
    built = IdentifyRun.from_settings(settings, runner=runner, confirm=True, preview=lambda _: None)
    assert built.judging and built.cap() == 1 and built.auto_accept == {FAMILY: 0.1}
    col = run(*_near_docs(2), identify_run=built)
    assert len(runner.requests) == 1
    assert [r["reason"] for r in col.residual] == ["judge-budget"]
    assert not IdentifyRun.from_settings(JudgeSettings()).judging


# -- determinism, schema, strict loading -----------------------------------------------------


def test_deterministic_with_a_fake_judge() -> None:
    arts = [*_near_docs(4), doc("docs/zebra.md", "Zebra census")]

    def once() -> bytes:
        runner = FakeRunner(lambda r: "present:1" if "part1" in r["context"] else None)
        col = run(*arts, options=IdentifyOptions(new_threshold=60), identify_run=judged(runner))
        return dumps_collation(col)

    data = once()
    assert data == once()
    loaded = loads_collation(data)
    assert dumps_collation(loaded) == data
    # verdicts sorted by unit then step, residual by unit
    units = [v["unit"] for v in loaded.verdicts]
    assert units == sorted(units)


def test_options_validation() -> None:
    assert IdentifyOptions.from_mapping({"new_threshold": 30, "other": 1}).new_threshold == 30
    for bad in (
        {"new_threshold": 0},
        {"new_threshold": 101},
        {"new_threshold": 0.5},
        {"new_threshold": True},
        {"max_candidates": 0},
    ):
        with pytest.raises(IdentifyOptionsError):
            IdentifyOptions(**bad)
    with pytest.raises(IdentifyOptionsError):
        IdentifyOptions.from_json({"new_threshold": None})
    with pytest.raises(ValueError):
        IdentifyRun(max_calls_per_run=-1)


def test_schema_and_strict_loading() -> None:
    runner = FakeRunner(lambda r: "present:1" if "part1" in r["context"] else None)
    col = run(
        *_near_docs(3),
        doc("docs/zebra.md", "Zebra census"),
        doc("docs/linked.md", "Linked"),
        options=IdentifyOptions(new_threshold=20),
        identify_run=judged(runner),
    )
    obj = json.loads(dumps_collation(col))
    assert {v["step"] for v in obj["verdicts"]} == {"external-ref", "judge", "threshold"}
    assert obj["residual"]
    assert all(sorted(v) == sorted(VERDICT_MEMBERS) for v in obj["verdicts"])
    assert all(sorted(r) == sorted(RESIDUAL_MEMBERS) for r in obj["residual"])
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(collation_schema()).validate(obj)

    def load(**change: Any):
        return loads_collation(beadlog.canonical_json({**obj, **change}).encode() + b"\n")

    assert load() == col
    verdict, item = obj["verdicts"][0], obj["residual"][0]
    for bad in (
        {"verdicts": [{**verdict, "extra": 1}]},
        {"verdicts": [{**verdict, "step": "guess"}]},
        {"verdicts": [{**verdict, "class": "MAYBE"}]},
        {"verdicts": [{**verdict, "confidence_permille": -1}]},
        {"verdicts": [{**verdict, "probabilities_permille": {"new": 2000}}]},
        {"verdicts": [{**verdict, "fallback": {"engine": "x"}}]},
        {"residual": [{**item, "reason": "guess"}]},
        {"residual": [{k: v for k, v in item.items() if k != "score"}]},
        {"options": {**obj["options"], "identify": {"new_threshold": 0, "max_candidates": 5}}},
        {"options": {k: v for k, v in obj["options"].items() if k != "identify"}},
    ):
        with pytest.raises(CollationFormatError):
            load(**bad)
