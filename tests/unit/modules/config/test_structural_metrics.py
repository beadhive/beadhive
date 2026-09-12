from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/config_module_metrics.py"


def _metrics_module():
    spec = importlib.util.spec_from_file_location("config_module_metrics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_checked_structural_metrics_are_current(runtime_test_scope):
    del runtime_test_scope
    module = _metrics_module()

    assert module.DEFAULT_OUTPUT.read_text(encoding="utf-8") == module._render(
        module.build_metrics()
    )


def test_structural_metrics_name_revision_and_complexity_definition(runtime_test_scope):
    del runtime_test_scope
    module = _metrics_module()
    metrics = module.build_metrics()

    assert metrics["before"]["revision"] == module.BASE_REVISION
    assert metrics["before"]["python_files"] > 0
    assert metrics["after"]["python_files"] >= metrics["before"]["python_files"]
    assert metrics["after"]["cyclomatic_max"] >= 1
    assert "AST decision count" in metrics["metric_definition"]["cyclomatic"]
