from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/capability_closeout.py"


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
    module = _module()
    assert module.build_proof() == module.build_proof()


def test_capability_closeout_proves_every_acceptance_boundary() -> None:
    data = _module().build_proof()
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
