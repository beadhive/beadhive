"""Fail-closed checks for Pants/native shadow qualification evidence."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pants_shadow", ROOT / "scripts/pants_shadow_evidence.py"
)
assert SPEC and SPEC.loader
shadow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shadow
SPEC.loader.exec_module(shadow)
PAYLOAD = {
    "schema_version": 1,
    "status": "active",
    "inputs": {},
    "observations": [
        {
            "mutation": mutation,
            "selector_reason": "synthetic unit fixture",
            "fallback_reason": None,
            "executed_count": 1,
            "avoided_count": 1,
            "cache_served_count": 0,
            "edit_to_result_seconds": 1.0,
            "pants_result": "green",
            "native_result": "green",
        }
        for mutation in sorted(shadow.REQUIRED_MUTATIONS)
    ],
    "metrics": {
        "warm_repeatable": True,
        "unrelated_test_avoided": True,
        "warm_seconds": 1.0,
        "cold_seconds": 2.0,
    },
    "promotion": {
        "selected_green_native_red_escapes": 0,
        "activation_eligible": True,
        "promoted_pants_routes": 0,
        "fallback_observations": 7,
        "production_activation": False,
    },
}


def test_shadow_evidence_validator_accepts_complete_runtime_payload() -> None:
    assert shadow.validate(PAYLOAD) == []


def test_selected_green_native_red_escape_blocks_activation() -> None:
    payload = copy.deepcopy(PAYLOAD)
    payload["observations"][0]["native_result"] = "red"
    assert any("selected-green/native-red" in error for error in shadow.validate(payload))


def test_missing_stale_or_incompatible_evidence_fails_closed(tmp_path) -> None:
    payload = copy.deepcopy(PAYLOAD)
    payload["status"] = "active"
    payload["inputs"] = {"missing": "sha256:nope"}
    payload["schema_version"] = 2
    errors = shadow.validate(payload, tmp_path)
    assert "unsupported schema" in errors
    assert "stale or missing input: missing" in errors


def test_operational_volume_and_attest_handoff_are_explicit() -> None:
    assert PAYLOAD["promotion"]["promoted_pants_routes"] == 0
    assert PAYLOAD["promotion"]["fallback_observations"] == 7
    assert PAYLOAD["promotion"]["production_activation"] is False
    docs = (ROOT / "docs/PANTS-SHADOW-QUALIFICATION.md").read_text(encoding="utf-8")
    assert "just check-all" in docs
    assert "without replacing" in docs


def test_superseded_evidence_cannot_activate_a_route() -> None:
    payload = copy.deepcopy(PAYLOAD)
    payload["status"] = "superseded"
    payload["superseded_by"] = "synthetic replacement"
    payload["promotion"]["activation_eligible"] = True
    assert "superseded evidence cannot promote or activate a route" in shadow.validate(payload)
