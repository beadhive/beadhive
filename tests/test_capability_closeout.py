from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

from scripts import check_import_boundaries as live_boundaries

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/capability_closeout.py"
OUTPUT = ROOT / "docs/proof/bh-bptze.7-capability-closeout.json"


def _module():
    spec = importlib.util.spec_from_file_location("capability_closeout", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_capability_closeout_is_reproducible() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_capability_closeout_uses_only_its_pinned_historical_checker() -> None:
    module = _module()
    historical = module._historical_checker()

    assert module.HISTORICAL_CHECKER_PATH == "scripts/check_import_boundaries.py"
    assert module.HISTORICAL_CHECKER_SHA256 == (
        "09e6dfde247ab68692119417bf64390544cce8173a2197cab2b191f3f7614837"
    )
    assert tuple(inspect.signature(historical.check).parameters) == (
        "source_root",
        "ledger_path",
    )
    assert tuple(inspect.signature(live_boundaries.check).parameters) == (
        "source_root",
        "ledger_path",
        "root_ownership_path",
    )


def test_capability_closeout_proves_every_acceptance_boundary() -> None:
    data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    assert data["measured_source_revision"] == _module().SOURCE_REVISION
    current = data["repository_history"][-1]
    assert (
        current["python_modules"],
        current["import_edges"],
        current["scc_count"],
        current["cyclic_modules"],
        current["scc_sizes"],
        current["cyclic_edges"],
        current["cyclic_symbols"],
    ) == (281, 2460, 8, 59, [35, 8, 6, 2, 2, 2, 2, 2], 155, 172)
    assert data["material_reduction"]["historical_largest_scc"]["delta"] == -30
    assert data["closure_registry"] == {
        "absent": 0,
        "full_gate": "just check",
        "present": 23,
        "release_gate": "just check-all",
    }
    assert set(data["modules"]) == {
        "agents",
        "config",
        "hives",
        "planning",
        "state",
        "work",
        "worktrees",
    }
    for row in data["modules"].values():
        assert row["after"]["status"] == "present"
        assert row["after"]["tests"]
        assert row["after"]["reverse_dependencies"]
        assert row["after"]["execution"]["passed"] > 0
        assert row["after"]["coverage"]["covered"] > 0
    assert all(
        cycle["owner"] and cycle["followup"] and cycle["rationale"] for cycle in current["cycles"]
    )
    assert sum(len(cycle["active_exception_ids"]) for cycle in current["cycles"]) == 38
    ledger = data["exception_ledger"]
    assert ledger["active_cycle_exceptions"] == 38
    assert ledger["removed_cycle_audit_records"] == 6
    assert ledger["architecture_check_errors"] == []
    assert data["repository_execution"]["after"]["passed"] == 7645
