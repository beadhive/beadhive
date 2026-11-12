"""``bh backfill verdicts``: the read-only judge verdict review.

Inputs: a ``.bdlg`` reconcile plan with recorded verdicts and a ``bdlg.collation/1`` JSON whose
``verdicts`` member (the B5 shape, ``tests/fixtures/backfill/collation-verdicts.json``).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from tests.test_backfill_bd import cfg_path as cfg_path  # noqa: F401 — fixture resolved by name
from typer.testing import CliRunner

import beadlog
from beadhive import backfill_cli, cli
from beadlog_sources import Fallback, Judgment, write_plan

reconcile = importlib.import_module("beadlog_sources.reconcile")

ENTRY = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "bh"}
ENV = {"COLUMNS": "200", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
FIXTURE = Path(__file__).parent / "fixtures" / "backfill" / "collation-verdicts.json"


def _invoke(*args: str):
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == 0, result.output
    return result


def _verdict(outcome, confidence, engine="haiku", model="m1", fallback=None):
    return reconcile.Verdict(
        "reconcile.match",
        f"sha256:{outcome}{confidence}",
        Judgment(
            outcome=outcome,
            probabilities={outcome or "abstain": 1.0},
            confidence=confidence,
            option_count=3,
            rationale="r",
            engine=engine,
            engine_version="1",
            model=model,
            fallback=fallback,
        ),
    )


def _entry(bead, cls, verdict, **kw):
    return reconcile.PlanEntry(
        beadlog.BeadRef("bh", bead), cls, reconcile.Review.REQUIRED, verdict=verdict, **kw
    )


@pytest.fixture
def plan_path(tmp_path, monkeypatch) -> Path:
    entries = [
        _entry("a-1", reconcile.ReconcileClass.NEW, _verdict("new", 0.9)),
        _entry(
            "a-2",
            reconcile.ReconcileClass.PRESENT,
            _verdict(
                "present:w-1",
                0.4,
                engine="deterministic-lexical",
                model=None,
                fallback=Fallback("deterministic-lexical", "timeout", "slow"),
            ),
            match=beadlog.BeadRef("bh", "w-1"),
        ),
        _entry("a-3", reconcile.ReconcileClass.NEW, _verdict(None, 0.0)),
        reconcile.PlanEntry(
            beadlog.BeadRef("bh", "a-4"), reconcile.ReconcileClass.NEW, reconcile.Review.NONE
        ),
    ]
    plan = reconcile.ReconcilePlan(
        events=(), entries=tuple(entries), corpus_hash="sha256:c", proposed_hash="sha256:p"
    )
    path = tmp_path / "plan.bdlg"
    write_plan(
        path,
        plan,
        groups=[backfill_cli.hive_group(ENTRY, tmp_path)],
        generator=beadlog.Generator("beadlog", "0.1.0"),
    )
    return path


def test_plan_view_groups_by_class_and_reports_fallback(plan_path, cfg_path) -> None:  # noqa: F811
    view = json.loads(_invoke("backfill", "verdicts", str(plan_path), "--json").stdout)
    assert view["schema"] == "beadhive.backfill.verdicts/1"
    assert view["input"] == "archive"
    assert view["totals"]["recorded"] == 3
    assert view["totals"]["without_verdict"] == 1
    assert list(view["groups"]) == ["NEW", "PRESENT"]
    new = view["groups"]["NEW"]
    assert [i["confidence"] for i in new] == [0.0, 0.9]
    present = view["groups"]["PRESENT"][0]
    assert present["fallback"]["reason"] == "timeout"
    assert present["match"] == {"group": "bh", "id": "w-1"}
    assert view["cost"]["cost_per_call_usd"] is None
    assert view["cost"]["estimated_usd"] is None


def test_filters_and_cost(plan_path, cfg_path, monkeypatch) -> None:  # noqa: F811
    from beadhive import backfill_verdicts as bv

    view = bv.build_view(plan_path, classes=["new"], min_confidence=0.5, cost_per_call_usd=0.01)
    assert view["totals"]["shown"] == 1
    assert view["cost"] == {"calls": 1, "cost_per_call_usd": 0.01, "estimated_usd": 0.01}
    by_engine = bv.build_view(plan_path, engine="deterministic-lexical")
    assert by_engine["totals"]["shown"] == 1
    assert bv.build_view(plan_path, max_confidence=0.1)["totals"]["shown"] == 1
    # text output is produced
    out = _invoke("backfill", "verdicts", str(plan_path), "--class", "PRESENT").stdout
    assert "PRESENT (1)" in out and "fallback=timeout" in out and "NEW" not in out


def test_collation_fixture_uses_the_b5_shape(cfg_path) -> None:  # noqa: F811
    view = json.loads(_invoke("backfill", "verdicts", str(FIXTURE), "--json").stdout)
    assert view["input"] == "collation"
    assert view["totals"]["recorded"] == 4
    assert set(view["groups"]) == {"NEW", "PRESENT", "abstain", "residual"}
    new = view["groups"]["NEW"][0]
    assert new["confidence"] == 0.8 and new["step"] == "judge"
    assert new["provenance"] == "inferred" and new["score"] == 41.0
    assert view["groups"]["PRESENT"][0]["bead"] == {"group": "", "id": "wd-1"}
    abstain = view["groups"]["abstain"][0]
    assert abstain["fallback"]["reason"] == "timeout"
    assert view["groups"]["residual"][0]["reason"] == "judge-budget"
    # one judge record without fallback is a call; the fallback and the budget residual are not
    from beadhive import backfill_verdicts as bv

    priced = bv.build_view(FIXTURE, cost_per_call_usd=0.5)
    assert priced["cost"]["calls"] == 1 and priced["cost"]["estimated_usd"] == 0.5


@pytest.mark.parametrize(
    "body", ['{"schema": "bdlg.collation/1"}', '{"schema": "bdlg.collation/1", "verdicts": []}']
)
def test_empty_collation_is_an_empty_view(tmp_path, cfg_path, body) -> None:  # noqa: F811
    path = tmp_path / "c.json"
    path.write_text(body)
    view = json.loads(_invoke("backfill", "verdicts", str(path), "--json").stdout)
    assert view["groups"] == {} and view["totals"]["recorded"] == 0
    assert "no verdicts recorded" in _invoke("backfill", "verdicts", str(path)).stdout


def test_non_collation_input_is_refused(tmp_path, cfg_path) -> None:  # noqa: F811
    path = tmp_path / "x.json"
    path.write_text("[]")
    result = CliRunner().invoke(cli.app, ["backfill", "verdicts", str(path)], env=ENV)
    assert result.exit_code != 0


def test_view_is_read_only(plan_path, cfg_path, tmp_path) -> None:  # noqa: F811
    before = plan_path.read_bytes()
    _invoke("backfill", "verdicts", str(plan_path))
    assert plan_path.read_bytes() == before
