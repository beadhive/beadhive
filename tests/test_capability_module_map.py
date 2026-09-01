from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/capability_module_map.py"


def _module():
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("capability_module_map", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_capability_module_map_is_reproducible_from_the_measured_revision() -> None:
    capability_module_map = _module()
    artifact = json.loads(capability_module_map.DEFAULT_OUTPUT.read_text(encoding="utf-8"))

    assert artifact == capability_module_map.build_map()


def test_capability_module_map_freezes_every_required_evidence_family() -> None:
    capability_module_map = _module()
    artifact = json.loads(capability_module_map.DEFAULT_OUTPUT.read_text(encoding="utf-8"))

    assert set(artifact["slices"]) == {"hives", "worktrees", "work", "planning", "state"}
    assert artifact["repository"]["owned_cyclic_sccs"] == 6
    assert artifact["repository"]["cyclic_edges"] == 278
    for slice_evidence in artifact["slices"].values():
        assert slice_evidence["source_paths"]
        assert slice_evidence["fan_in"]["distinct_external_importers"] >= 1
        assert slice_evidence["churn"]["commits"] >= 1
        assert slice_evidence["current_test_files"]
        assert "inbound" in slice_evidence["call_coupling"]
        assert "outbound" in slice_evidence["call_coupling"]


def test_dynamic_seams_cover_every_exact_revision_python_test_caller() -> None:
    capability_module_map = _module()
    artifact = json.loads(capability_module_map.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    exact_test_paths = capability_module_map._revision_files("tests")

    assert len(exact_test_paths) == 387
    assert artifact["repository"]["test_inventory"] == {
        "python_files": len(exact_test_paths),
        "python_paths": list(exact_test_paths),
    }
    expected_seam_counts = {
        "hives": 197,
        "worktrees": 39,
        "work": 57,
        "planning": 8,
        "state": 20,
    }
    assert len(artifact["slices"]["hives"]["dynamic_test_seams"]) == 197
    assert len(artifact["slices"]["hives"]["dynamic_test_seams"]) != 77
    for name, slice_evidence in artifact["slices"].items():
        assert slice_evidence["dynamic_test_scope"] == {
            "python_files": len(exact_test_paths),
            "paths_ref": "repository.test_inventory.python_paths",
        }
        assert len(slice_evidence["dynamic_test_seams"]) == expected_seam_counts[name]
        assert {row["path"] for row in slice_evidence["dynamic_test_seams"]} <= set(
            exact_test_paths
        )
