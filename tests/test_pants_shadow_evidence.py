"""Fail-closed checks for Pants/native shadow qualification evidence."""

from __future__ import annotations

import copy
import importlib.util
import json
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
PAYLOAD = json.loads(shadow.EVIDENCE.read_text(encoding="utf-8"))


def test_checked_shadow_evidence_is_current_and_complete() -> None:
    assert shadow.validate(PAYLOAD) == []


def test_selected_green_native_red_escape_blocks_activation() -> None:
    payload = copy.deepcopy(PAYLOAD)
    payload["observations"][0]["native_result"] = "red"
    assert any("selected-green/native-red" in error for error in shadow.validate(payload))


def test_missing_stale_or_incompatible_evidence_fails_closed(tmp_path) -> None:
    payload = copy.deepcopy(PAYLOAD)
    payload["inputs"] = {"missing": "sha256:nope"}
    payload["schema_version"] = 2
    errors = shadow.validate(payload, tmp_path)
    assert "unsupported schema" in errors
    assert "stale or missing input: missing" in errors


def test_operational_volume_and_attest_handoff_are_explicit() -> None:
    assert PAYLOAD["promotion"]["promoted_pants_routes"] == 1
    assert PAYLOAD["promotion"]["fallback_observations"] == 7
    assert PAYLOAD["promotion"]["production_activation"] is False
    docs = (ROOT / "docs/PANTS-SHADOW-QUALIFICATION.md").read_text(encoding="utf-8")
    assert "just check-all" in docs
    assert "without replacing" in docs
