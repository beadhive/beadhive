#!/usr/bin/env python3
"""Evaluate checked shadow evidence without executing or selecting tests.

This module is deliberately a pure policy boundary.  It can describe when a recorded selector
plan would be safe at a local leaf boundary, but it cannot run a command, change Beadhive
configuration, or activate a closure.  The checked repository evidence currently admits no
closure, so every real validation boundary continues to use ``just check``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CERTIFICATION_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.1-test-closure-certification.json"
EVIDENCE_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.3-shadow-activation.json"
SELECTOR_PATH = ROOT / "scripts" / "test_impact_selector.py"

SCHEMA_VERSION = 1
POLICY_VERSION = "bh-test-closure-shadow-policy-v1"
SELECTOR_VERSION = "bh-test-impact-selector-v1"
FULL_GATE = "just check"
MIN_QUALIFYING_CHANGES = 30
MIN_WINDOW_DAYS = 60
MAX_WALL_RATIO = 0.5
MIN_MEDIAN_SAVINGS_SECONDS = 30.0
REQUIRED_CHANGE_CLASSES = frozenset(
    {
        "owned-implementation",
        "public-port-or-contract",
        "reverse-dependent-or-multi-file",
    }
)
LOCAL_LEAF_BOUNDARIES = frozenset({"check", "submit", "pristine-review"})
FULL_ONLY_BOUNDARIES = frozenset(
    {
        "leaf-merge",
        "epic-finish",
        "workstream-submit",
        "workstream-review",
        "scheduled",
        "release",
    }
)


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _empty_measurements() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "sample_count": 0,
        "qualifying_count": 0,
        "window_days": None,
        "selected_wall_median_seconds": None,
        "full_wall_median_seconds": None,
        "wall_savings_median_seconds": None,
        "wall_ratio": None,
        "selected_compute_median_seconds": None,
        "full_compute_median_seconds": None,
        "queue_median_seconds": None,
        "flake_count": 0,
        "miss_count": 0,
        "fallback_count": 0,
        "fallback_rate": None,
    }


def _applicability_reasons(closure: Mapping[str, Any]) -> set[str]:
    applicability = closure.get("current_applicability")
    if not isinstance(applicability, Mapping):
        return {"current-applicability-not-proven"}
    reasons = applicability.get("fallback_reasons")
    if applicability.get("applicable") is not True or not isinstance(reasons, list) or reasons:
        return {"current-applicability-not-proven"}
    for recorded, observed in (
        ("recorded_input_digest", "observed_input_digest"),
        ("recorded_registry_digest", "observed_registry_digest"),
        ("recorded_material_digest", "observed_material_digest"),
    ):
        left = applicability.get(recorded)
        right = applicability.get(observed)
        if not isinstance(left, str) or not left or left != right:
            return {"current-applicability-not-proven"}
    return set()


def _prerequisite_reasons(closure: Mapping[str, Any]) -> set[str]:
    reasons = _applicability_reasons(closure)
    certification = closure.get("certification")
    if not isinstance(certification, Mapping) or certification.get("status") != "certified":
        reasons.add("closure-not-certified")
    if not isinstance(certification, Mapping) or not certification.get(
        "eligible_for_selective_activation"
    ):
        reasons.add("closure-not-activation-eligible")
    if closure.get("confidence") != "high":
        reasons.add("confidence-below-high")
    if closure.get("enforceable_port") is not True:
        reasons.add("unenforceable-public-port")
    if closure.get("input_digest") != closure.get("observed_input_digest"):
        reasons.add("closure-input-digest-mismatch")
    coverage = closure.get("coverage_mapping")
    if (
        not isinstance(coverage, Mapping)
        or coverage.get("mode") != "dynamic-per-test"
        or not coverage.get("per_test_contexts")
        or coverage.get("input_digest") != closure.get("input_digest")
        or not coverage.get("source_revision")
    ):
        reasons.add("missing-or-stale-dynamic-trace")
    return reasons


def evaluate_candidate(
    closure: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    *,
    selector_version: str,
    selector_digest: str,
) -> dict[str, Any]:
    """Return a deterministic eligibility decision from already-recorded observations."""
    reasons = _prerequisite_reasons(closure)
    closure_id = str(closure.get("id") or "")
    closure_digest = closure.get("input_digest")
    command = str(closure.get("command") or f"just test-closure {closure_id}")
    qualifying: list[Mapping[str, Any]] = []
    timestamps: list[datetime] = []
    selected_wall: list[float] = []
    full_wall: list[float] = []
    selected_compute: list[float] = []
    full_compute: list[float] = []
    queue: list[float] = []
    flake_count = 0
    miss_count = 0
    fallback_count = 0
    change_classes: set[str] = set()
    evidence_modes: set[str] = set()

    for sample in samples:
        sample_fallbacks = sample.get("fallback_reasons")
        if not isinstance(sample_fallbacks, list):
            reasons.add("invalid-shadow-sample")
            sample_fallbacks = ["invalid-shadow-sample"]
        if sample_fallbacks:
            fallback_count += 1
            reasons.add("shadow-sample-fell-back")
        if sample.get("qualifying_merged_change") is not True:
            continue
        qualifying.append(sample)
        provenance = sample.get("provenance")
        if provenance not in {"qualifying-merged-change", "simulation-only"}:
            reasons.add("invalid-shadow-provenance")
        else:
            evidence_modes.add(str(provenance))
        change_class = sample.get("change_class")
        if not isinstance(change_class, str):
            reasons.add("invalid-shadow-sample")
        else:
            change_classes.add(change_class)
        timestamp = _parse_timestamp(sample.get("observed_at"))
        if timestamp is None:
            reasons.add("invalid-shadow-sample")
        else:
            timestamps.append(timestamp)

        selected = sample.get("selected")
        full = sample.get("full")
        if not isinstance(selected, Mapping) or not isinstance(full, Mapping):
            reasons.add("invalid-shadow-sample")
            continue
        sample_tree = sample.get("tree")
        if (
            not sample_tree
            or selected.get("tree") != sample_tree
            or full.get("tree") != sample_tree
        ):
            reasons.add("selected-full-tree-mismatch")
        selector = sample.get("selector")
        if (
            not isinstance(selector, Mapping)
            or selector.get("version") != selector_version
            or selector.get("digest") != selector_digest
        ):
            reasons.add("selector-drift")
        sample_digest = sample.get("closure_input_digest")
        if not isinstance(sample_digest, str) or not sample_digest:
            reasons.add("closure-input-digest-mismatch")
        plan = sample.get("plan")
        if not isinstance(plan, Mapping):
            reasons.add("missing-selector-plan")
        else:
            if (
                not isinstance(plan.get("plan_digest"), str)
                or not plan.get("plan_digest")
                or plan.get("plan_digest") != plan.get("replay_plan_digest")
            ):
                reasons.add("selector-plan-not-deterministic")
            if plan.get("selector") != selector:
                reasons.add("selector-drift")
            if plan.get("decision") != "selective" or plan.get("fallback_reasons"):
                reasons.add("shadow-sample-fell-back")
            if plan.get("commands") != [command] or plan.get("tests") != selected.get("tests"):
                reasons.add("selector-plan-run-mismatch")
        if selected.get("command") != command or full.get("command") != FULL_GATE:
            reasons.add("invalid-shadow-command")
        for collection in (
            selected.get("tests"),
            full.get("tests"),
            sample.get("omitted_tests"),
        ):
            if not isinstance(collection, list) or any(
                not isinstance(item, str) for item in collection
            ):
                reasons.add("incomplete-test-inventory")
        selected_tests = selected.get("tests")
        full_tests = full.get("tests")
        omitted_tests = sample.get("omitted_tests")
        inventories = (selected_tests, full_tests, omitted_tests)
        if all(
            isinstance(value, list) and all(isinstance(item, str) for item in value)
            for value in inventories
        ):
            expected_omitted = sorted(set(full_tests) - set(selected_tests))
            if sorted(omitted_tests) != expected_omitted:
                reasons.add("omitted-test-inventory-mismatch")
        for result in (selected, full):
            if not isinstance(result.get("failures"), list):
                reasons.add("incomplete-failure-inventory")
        relevant_failures = full.get("relevant_failures")
        escapes = sample.get("escapes")
        if not isinstance(relevant_failures, list) or not isinstance(escapes, list):
            reasons.add("invalid-shadow-sample")
            relevant_failures = []
            escapes = []
        selected_green = selected.get("exit_code") == 0
        if selected_green and relevant_failures:
            miss_count += 1
            reasons.add("selected-green-full-red-escape")
            if not escapes or any(
                not isinstance(escape, Mapping)
                or not escape.get("root_cause")
                or not escape.get("correction")
                or not escape.get("replay")
                for escape in escapes
            ):
                reasons.add("unexplained-or-unreplayed-escape")
        elif escapes:
            reasons.add("escape-ledger-inconsistent")

        values = {
            "selected_wall": _number(selected.get("wall_seconds")),
            "full_wall": _number(full.get("wall_seconds")),
            "selected_compute": _number(selected.get("compute_seconds")),
            "full_compute": _number(full.get("compute_seconds")),
            "selected_queue": _number(selected.get("queue_seconds")),
            "full_queue": _number(full.get("queue_seconds")),
        }
        if any(value is None for value in values.values()):
            reasons.add("incomplete-timing-measurement")
        else:
            selected_wall.append(values["selected_wall"] or 0.0)
            full_wall.append(values["full_wall"] or 0.0)
            selected_compute.append(values["selected_compute"] or 0.0)
            full_compute.append(values["full_compute"] or 0.0)
            queue.append((values["selected_queue"] or 0.0) + (values["full_queue"] or 0.0))
        selected_flakes = selected.get("flake_count")
        full_flakes = full.get("flake_count")
        if (
            isinstance(selected_flakes, bool)
            or not isinstance(selected_flakes, int)
            or selected_flakes < 0
            or isinstance(full_flakes, bool)
            or not isinstance(full_flakes, int)
            or full_flakes < 0
        ):
            reasons.add("incomplete-flake-measurement")
        else:
            flake_count += selected_flakes + full_flakes

    if len(qualifying) < MIN_QUALIFYING_CHANGES:
        reasons.add("insufficient-qualifying-shadow-samples")
    if not REQUIRED_CHANGE_CLASSES.issubset(change_classes):
        reasons.add("representative-change-classes-missing")
    if "simulation-only" in evidence_modes and closure.get("simulation_control") is not True:
        reasons.add("simulation-evidence-not-authoritative")
    if len(evidence_modes) > 1:
        reasons.add("mixed-shadow-provenance")
    unique_trees = {sample.get("tree") for sample in qualifying if sample.get("tree")}
    if len(unique_trees) != len(qualifying):
        reasons.add("duplicate-shadow-tree")
    window_days: int | None = None
    if len(timestamps) >= 2:
        window_days = (max(timestamps) - min(timestamps)).days
    if window_days is None or window_days < MIN_WINDOW_DAYS:
        reasons.add("shadow-window-too-young")
    latest_sample = max(
        ((timestamp, sample) for timestamp, sample in zip(timestamps, qualifying, strict=False)),
        default=None,
        key=lambda item: item[0],
    )
    coverage = closure.get("coverage_mapping")
    if latest_sample is None or latest_sample[1].get("closure_input_digest") != closure_digest:
        reasons.add("closure-input-digest-mismatch")
    if (
        latest_sample is None
        or not isinstance(coverage, Mapping)
        or latest_sample[1].get("source_revision") != coverage.get("source_revision")
    ):
        reasons.add("dynamic-trace-revision-mismatch")

    selected_wall_median = _median(selected_wall)
    full_wall_median = _median(full_wall)
    savings = (
        full_wall_median - selected_wall_median
        if selected_wall_median is not None and full_wall_median is not None
        else None
    )
    ratio = (
        selected_wall_median / full_wall_median
        if selected_wall_median is not None and full_wall_median not in (None, 0.0)
        else None
    )
    if (
        ratio is None
        or savings is None
        or ratio > MAX_WALL_RATIO
        or savings < MIN_MEDIAN_SAVINGS_SECONDS
    ):
        reasons.add("timing-target-not-met")

    measurements = _empty_measurements()
    if samples:
        measurements = {
            "status": "measured",
            "sample_count": len(samples),
            "qualifying_count": len(qualifying),
            "window_days": window_days,
            "selected_wall_median_seconds": selected_wall_median,
            "full_wall_median_seconds": full_wall_median,
            "wall_savings_median_seconds": savings,
            "wall_ratio": ratio,
            "selected_compute_median_seconds": _median(selected_compute),
            "full_compute_median_seconds": _median(full_compute),
            "queue_median_seconds": _median(queue),
            "flake_count": flake_count,
            "miss_count": miss_count,
            "fallback_count": fallback_count,
            "fallback_rate": fallback_count / len(samples),
        }
    return {
        "id": closure_id,
        "command": command,
        "eligible": not reasons,
        "reasons": sorted(reasons),
        "selector": {"version": selector_version, "digest": selector_digest},
        "input_digest": closure_digest,
        "evidence_mode": (
            next(iter(evidence_modes), "none") if len(evidence_modes) <= 1 else "mixed"
        ),
        "measurements": measurements,
    }


def route_validation(
    plan: Mapping[str, Any],
    candidate_decisions: Mapping[str, Mapping[str, Any]],
    *,
    boundary: str,
    is_leaf: bool,
    local_enabled: bool,
    allow_simulation: bool = False,
) -> dict[str, Any]:
    """Resolve one already-built plan; uncertainty always returns the ordinary full gate."""

    def full(*reasons: str) -> dict[str, Any]:
        return {
            "command": FULL_GATE,
            "selective": False,
            "closure": None,
            "reasons": sorted(set(reasons)),
        }

    if not local_enabled:
        return full("local-activation-disabled")
    if boundary not in LOCAL_LEAF_BOUNDARIES:
        return full("boundary-requires-full-gate")
    if not is_leaf:
        return full("non-leaf-work-item")
    fallback_reasons = plan.get("fallback_reasons")
    if plan.get("decision") != "selective" or fallback_reasons:
        reasons = ["selector-required-full-gate"]
        if isinstance(fallback_reasons, list):
            reasons.extend(str(reason) for reason in fallback_reasons)
        return full(*reasons)
    closures = plan.get("changed_modules")
    if not isinstance(closures, list) or len(closures) != 1:
        return full("multi-module-ambiguity")
    closure_id = str(closures[0])
    candidate = candidate_decisions.get(closure_id)
    if not isinstance(candidate, Mapping) or candidate.get("eligible") is not True:
        return full("closure-not-eligible")
    if candidate.get("evidence_mode") != "qualifying-merged-change" and not allow_simulation:
        return full("simulation-evidence-not-activatable")
    selector = plan.get("selector")
    if not isinstance(selector, Mapping) or selector != candidate.get("selector"):
        return full("selector-drift")
    commands = plan.get("commands")
    expected_command = candidate.get("command")
    if commands != [expected_command] or not isinstance(expected_command, str):
        return full("selector-command-drift")
    return {
        "command": expected_command,
        "selective": True,
        "closure": closure_id,
        "reasons": [],
    }


def build_checked_evidence(root: Path = ROOT) -> dict[str, Any]:
    certification_path = root / CERTIFICATION_PATH.relative_to(ROOT)
    selector_path = root / SELECTOR_PATH.relative_to(ROOT)
    policy_path = root / Path(__file__).resolve().relative_to(ROOT)
    certification = _load_json(certification_path)
    selector_digest = _digest_bytes(selector_path.read_bytes())
    decisions = [
        evaluate_candidate(
            closure,
            (),
            selector_version=SELECTOR_VERSION,
            selector_digest=selector_digest,
        )
        for closure in certification.get("closures", ())
    ]
    eligible = sorted(item["id"] for item in decisions if item["eligible"])
    inputs = {
        "certification_artifact": {
            "path": CERTIFICATION_PATH.relative_to(ROOT).as_posix(),
            "digest": _digest_bytes(certification_path.read_bytes()),
        },
        "selector": {
            "path": SELECTOR_PATH.relative_to(ROOT).as_posix(),
            "version": SELECTOR_VERSION,
            "digest": selector_digest,
        },
        "shadow_policy": {
            "path": policy_path.relative_to(root).as_posix(),
            "version": POLICY_VERSION,
            "digest": _digest_bytes(policy_path.read_bytes()),
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "local-leaf-shadow-policy-only",
        "inputs": inputs,
        "input_digest": _canonical_digest(inputs),
        "policy": {
            "minimum_qualifying_merged_changes": MIN_QUALIFYING_CHANGES,
            "minimum_window_days": MIN_WINDOW_DAYS,
            "required_change_classes": sorted(REQUIRED_CHANGE_CLASSES),
            "zero_selected_green_full_red_escapes": True,
            "maximum_selected_to_full_wall_ratio": MAX_WALL_RATIO,
            "minimum_median_wall_savings_seconds": MIN_MEDIAN_SAVINGS_SECONDS,
            "local_leaf_boundaries": sorted(LOCAL_LEAF_BOUNDARIES),
            "full_only_boundaries": sorted(FULL_ONLY_BOUNDARIES),
            "rollback": [
                "local-activation-disabled",
                "selected-green-full-red-escape",
                "selector-or-input-digest-drift",
                "ownership-or-confidence-degradation",
                "uncertain-or-full-selector-plan",
            ],
        },
        "activation": {"enabled": False, "eligible_closures": eligible},
        "observations": {
            "status": "not-started-no-certified-candidates",
            "samples": [],
            "note": "No real shadow result is claimed; simulation-only controls live in tests.",
        },
        "closure_decisions": decisions,
        "current_gate": {
            "command": FULL_GATE,
            "applies_to": (
                "all check, submit, review, merge, epic, workstream, scheduled, and "
                "release boundaries"
            ),
            "receipt_reuse": (
                "exact-tree full-gate receipts may be reused by existing lifecycle policy"
            ),
        },
    }


def validate_checked_evidence(evidence: Mapping[str, Any], root: Path = ROOT) -> tuple[str, ...]:
    expected = build_checked_evidence(root)
    if evidence != expected:
        return ("checked evidence drifted from current certification, selector, or policy inputs",)
    if evidence.get("activation") != {"enabled": False, "eligible_closures": []}:
        return ("checked evidence must leave real activation disabled",)
    return ()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the checked evidence")
    args = parser.parse_args(argv)
    if args.check:
        errors = validate_checked_evidence(_load_json(EVIDENCE_PATH), ROOT)
        for error in errors:
            print(f"error: {error}")
        if not errors:
            print("shadow policy evidence valid: 23 closures rejected, activation disabled")
        return 1 if errors else 0
    print(json.dumps(build_checked_evidence(ROOT), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
