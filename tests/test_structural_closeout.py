"""Durability contracts for the bh-1jhk4 structural closeout decision."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "design" / "structural-quality-baseline.md"


def test_structural_closeout_names_exact_comparable_evidence_and_no_global_gate():
    text = " ".join(LEDGER.read_text().split())

    assert "c0baa210e6da5cd7d592f3ff2505107ee38ae544" in text
    assert "cf0aeb417f03104f69a27a8bc805a5baea78ec4a" in text
    assert "24,681 / 27,482 (89.8079%)" in text
    assert "Observed coverage floors, not global thresholds" in text
    assert "There is no unexplained coverage regression" in text


def test_structural_closeout_preserves_stewardship_and_second_maintainer_trigger():
    text = " ".join(LEDGER.read_text().split())

    for family in ("`work.py` or `work_*.py`", "`config.py` or `config_*.py`", "`worktree.py` or"):
        assert family in text
    assert "`.github/CODEOWNERS`" in text
    assert "when a second active maintainer is added" in text
    assert "must add path-specific CODEOWNERS entries" in text
    assert "No new evidence invalidates those existing bead boundaries" in text
