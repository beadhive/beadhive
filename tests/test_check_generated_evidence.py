"""bh-2kodj: the fast generated-evidence pre-check names the generator to re-run."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_generated_evidence.py"
SPEC = importlib.util.spec_from_file_location("check_generated_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _documents(*, test_importers=158, revision="base"):
    evidence = {
        "baseline_revision": "base",
        "fan_in": {
            "production_config_importers_after": 40,
            "production_config_schema_importers_after": 7,
        },
        "static_test_blast_radius": {"exact_ast_files_after": 158},
    }
    ledger = {
        "current": {
            "production_config_importer_count": 40,
            "production_config_schema_importer_count": 7,
            "test_config_importer_count": test_importers,
        }
    }
    metrics = {"before": {"revision": revision}}
    return evidence, ledger, metrics


def test_module_evidence_matching_the_checked_ledger_is_current() -> None:
    assert MODULE.module_evidence_errors(*_documents()) == []


def test_module_evidence_drift_names_the_exact_field_and_value() -> None:
    errors = MODULE.module_evidence_errors(*_documents(test_importers=159, revision="other"))

    assert errors == [
        "docs/design/config-module-evidence.json: static_test_blast_radius."
        "exact_ast_files_after is 158; the checked ledger says 159",
        "docs/design/config-module-structural-metrics.json: before.revision must equal "
        "docs/design/config-module-evidence.json baseline_revision",
    ]


def _check(name, outcome):
    return MODULE.Check(name, f"{name}.json", f"regen-{name}", lambda _root: outcome)


def test_checks_run_concurrently_but_report_in_declared_order_and_crashes_are_red() -> None:
    def crash(_root):
        raise RuntimeError("boom")

    checks = (
        _check("a", MODULE.Outcome(True)),
        MODULE.Check("b", "b.json", "regen-b", crash),
        _check("c", MODULE.Outcome(False, "drifted")),
    )

    results = MODULE.run_checks(checks, ROOT, workers=3)

    assert [check.name for check, _ in results] == ["a", "b", "c"]
    assert [outcome.ok for _, outcome in results] == [True, False, False]
    assert results[1][1].detail == "RuntimeError: boom"


def test_stale_report_names_each_artifact_and_its_generator(capsys) -> None:
    results = [
        (_check("ledger", MODULE.Outcome(False, "ledger drifted")), MODULE.Outcome(False, "x")),
        (_check("metrics", MODULE.Outcome(True)), MODULE.Outcome(True)),
    ]

    assert MODULE.report(results, 1.25) == 1

    out = capsys.readouterr().out
    assert "generated-evidence: STALE (1 of 2 checks, 1.2s)" in out
    assert "✗ ledger is not current: ledger.json" in out
    assert "→ re-run: regen-ledger" in out
    assert "metrics is not current" not in out


def test_green_report_is_one_summary_line(capsys) -> None:
    results = [(_check("ledger", MODULE.Outcome(True)), MODULE.Outcome(True))]

    assert MODULE.report(results, 0.5) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "generated-evidence: OK (1 checks, 0.5s)"


def test_every_shipped_check_names_a_generator_and_an_artifact() -> None:
    names = [check.name for check in MODULE.CHECKS]

    assert len(names) == len(set(names))
    assert {"config dependency ledger", "config module evidence", "native impact map"} <= set(names)
    assert all(check.artifact and check.regenerate for check in MODULE.CHECKS)
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    assert "generated-evidence-check:\n    uv run python scripts/check_generated_evidence.py" in (
        justfile
    )
