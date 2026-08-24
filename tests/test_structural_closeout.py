"""Durability contracts for the bh-1jhk4 structural closeout decision."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "design" / "structural-quality-baseline.md"


def test_structural_closeout_names_exact_comparable_evidence_and_no_global_gate():
    text = " ".join(LEDGER.read_text().split())

    assert "c0baa210e6da5cd7d592f3ff2505107ee38ae544" in text
    assert "cf0aeb417f03104f69a27a8bc805a5baea78ec4a" in text
    assert "712ac29580d1f1873380bdfad80c35e067c33721" in text
    assert "24,709 / 27,511 (89.8150%)" in text
    assert "Observed coverage floors, not global thresholds" in text
    assert "There is no unexplained coverage regression" in text


def test_structural_closeout_records_complexity_fanin_churn_and_range_risk():
    text = " ".join(LEDGER.read_text().split())

    assert "22 -> 9" in text and "18 -> 12" in text and "14 -> 10" in text
    assert "22 -> 26" in text and "204 -> 213" in text and "44 -> 53" in text
    assert "scores the program 9.9 in the `high` fallback band" in text
    assert "entropy 4.2749" in text
    assert "facade churn/fix pressure is work 2,883/38.25" in text
    assert "RepoWise has no stored before/after scalar risk snapshot" in text


def test_structural_closeout_proves_cycle_regression_was_removed_and_names_limit():
    text = " ".join(LEDGER.read_text().split())

    assert "| Cyclic SCCs | 6 | 6 |" in LEDGER.read_text()
    assert "| Files in cyclic SCCs | 59 | 59 |" in LEDGER.read_text()
    assert "| Largest cyclic core | 44 | 44 |" in LEDGER.read_text()
    assert "The first exact assembled-tip index exposed a real regression" in text
    assert "66 cyclic files and a largest core of 51" in text
    assert "restores the baseline 6 SCCs / 59 cyclic files / core 44" in text
    assert "FacadeBinding" in text
    assert "RepoWise 0.45.0 has no `graph`/cycle command" in text
    assert "exposes no conformance score or field" in text
    assert "4-layer, 8-module, 12-step hierarchy" in text


def test_structural_closeout_preserves_stewardship_and_second_maintainer_trigger():
    text = " ".join(LEDGER.read_text().split())

    for family in ("`work.py` or `work_*.py`", "`config.py` or `config_*.py`", "`worktree.py` or"):
        assert family in text
    assert "`.github/CODEOWNERS`" in text
    assert "when a second active maintainer is added" in text
    assert "must add path-specific CODEOWNERS entries" in text
    assert "No new evidence invalidates those existing bead boundaries" in text
