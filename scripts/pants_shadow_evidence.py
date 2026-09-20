#!/usr/bin/env python3
"""Validate checked Pants/native shadow evidence and fail closed on drift or escapes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
EVIDENCE = ROOT / "docs/proof/bh-70ewe.5-pants-shadow.json"
REQUIRED_MUTATIONS = {
    "source",
    "test",
    "dependency-lock",
    "shared-contract",
    "generated-resource",
    "build-config",
    "fixture-plugin",
    "multi-module",
    "unrelated-test",
}


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate(payload: dict, root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("unsupported schema")
    status = payload.get("status", "active")
    if status not in {"active", "superseded"}:
        errors.append("unsupported evidence status")
    if status == "superseded" and not payload.get("superseded_by"):
        errors.append("superseded evidence must name its replacement")
    inputs = payload.get("inputs", {})
    for relative, expected in inputs.items():
        path = root / relative
        # A superseded record preserves the exact inputs its historical measurements used.
        # Its hashes are intentionally not rewritten to bless a new routing policy.
        if not path.is_file() or (status == "active" and digest(path) != expected):
            errors.append(f"stale or missing input: {relative}")
    observations = payload.get("observations", [])
    kinds = {row.get("mutation") for row in observations}
    if kinds != REQUIRED_MUTATIONS:
        errors.append("mutation matrix is incomplete")
    for row in observations:
        if row.get("pants_result") == "green" and row.get("native_result") == "red":
            errors.append(f"selected-green/native-red escape: {row.get('mutation')}")
        required = {
            "selector_reason",
            "fallback_reason",
            "executed_count",
            "avoided_count",
            "cache_served_count",
            "edit_to_result_seconds",
            "pants_result",
            "native_result",
        }
        if not required <= row.keys():
            errors.append(f"incomplete observation: {row.get('mutation')}")
    metrics = payload.get("metrics", {})
    if not metrics.get("warm_repeatable") or not metrics.get("unrelated_test_avoided"):
        errors.append("warm improvement and unrelated-test avoidance are required")
    if metrics.get("warm_seconds", 10**9) >= metrics.get("cold_seconds", 0):
        errors.append("warm observation is not an improvement")
    promotion = payload.get("promotion", {})
    if status == "superseded" and (
        promotion.get("activation_eligible")
        or promotion.get("production_activation")
        or promotion.get("promoted_pants_routes") != 0
    ):
        errors.append("superseded evidence cannot promote or activate a route")
    if errors or promotion.get("selected_green_native_red_escapes") != 0:
        if promotion.get("activation_eligible"):
            errors.append("unsafe evidence cannot be activation eligible")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    options = parser.parse_args()
    payload = json.loads(options.evidence.read_text(encoding="utf-8"))
    errors = validate(payload)
    if errors:
        print("pants-shadow-evidence: FAILED")
        for error in errors:
            print(f"- {error}")
        print("route: native/full")
        return 1
    status = payload.get("status", "active")
    print(
        "pants-shadow-evidence: OK "
        f"({status}; {len(payload['observations'])} mutations; "
        f"{payload['promotion']['promoted_pants_routes']} promoted Pants route)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
